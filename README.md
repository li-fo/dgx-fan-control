# DGX Fan Controller TUI (MVP)

[한국어 README](README.ko.md)

`dgx-fan` is a direct-Python Textual application for a Raspberry Pi 4 that reads GPU data from up to two DCGM exporter endpoints and independently drives two 4-wire PWM fans. It shows GPU memory, utilisation, temperature, and each fan's mapped DGX, duty, RPM, and state; its runtime On/Off control remains global.

## Run

Use Python 3.11+ and install the project with its development dependencies:

```bash
uv venv
uv pip install -e '.[dev]'
cp config.example.toml config.toml
```

Activate that virtual environment before using its Python or console entry point:

```bash
source .venv/bin/activate
python -m dgx_fan --config config.toml
# or: dgx-fan --config config.toml
```

Alternatively, run without activation through uv's managed project environment:

```bash
uv run dgx-fan --config config.toml
```

Do not substitute the system `/usr/bin/python` for the activated environment: it will not see this repository's `src/`-layout package unless `dgx-fan` has been installed into that Python environment. Configuration lookup is `--config`, then `DGX_FAN_CONFIG`, then `./config.toml`. Edit the file and restart the application; the MVP intentionally has no settings UI or hot reload.

Start with `hardware.backend = "fake"` on a development machine. Its default tach simulation reports plausible running RPM while PWM is nonzero; tests can explicitly inject `NO TACH` readings for stall probes. For a Pi install `.[raspberry-pi]`, enable the kernel PWM overlay, and change the backend to `raspberry-pi` only after wiring has been checked.

## Raspberry Pi clone-local installation and console start

For a Raspberry Pi OS console installation, clone the repository, then keep the editable
configuration **in that clone**. `install.sh` never copies `config.toml` to `/etc`.

```bash
git clone <repository-url> dgx-fan
cd dgx-fan
cp config.example.toml config.toml
# Edit DGX URLs and set [hardware] backend = "raspberry-pi" before continuing.
./install.sh --reboot
```

`install.sh` runs `uv sync --locked --extra raspberry-pi --no-dev`, validates the clone's
`config.toml`, adds the required dual-PWM overlay, and configures Raspberry Pi OS console
auto-login on tty1. If `config.toml` is absent, it creates it from `config.example.toml` and
stops; edit it and run the installer again. It requires `uv`; install uv with the
[official uv instructions](https://docs.astral.sh/uv/getting-started/installation/) first if it
is not already available. Use `./install.sh` without `--reboot` to install first and reboot
manually, or `./install.sh --dry-run` to inspect the intended action without changing the Pi.

No systemd unit or cron `@reboot` entry is installed. A root-owned tty1 profile hook calls
the clone's `start.sh` once for the configured local login user, never for SSH, and does not
`exec` the application: a normal TUI Quit returns to the console shell. `start.sh` uses
passwordless sudo only for a fixed, root-owned no-argument helper that grants the `gpio` group
access to PWM0, PWM1, and `/dev/gpiochip0`; it then launches
`.venv/bin/dgx-fan --config <clone>/config.toml` as the regular user. The Python project is
never run as root.

For a manual launch after installation, use:

```bash
./start.sh
```

To remove only this project's hook, sudoers policy, and hardware helper, while keeping the
clone and its configuration:

```bash
./uninstall.sh --yes
```

Uninstall intentionally leaves the PWM overlay and console auto-login in place. Disable
console auto-login with `sudo raspi-config`, and remove only the exact
`dtoverlay=pwm-2chan,pin=18,pin2=19,func=2,func2=2` line (or restore the backup created beside
the boot configuration) if rollback requires it. The local auto-login grants physical-console
access to the account and this design has no crash-restart supervisor. A tty1 console is also
less suitable for terminal mouse/touch reporting than a terminal emulator; keyboard control is
the supported boot-console interface. Validate boot behavior, PWM waveform, RPM, and fan
fail-safe behavior on the real Pi before unattended use.

## Configuration and safety

`config.example.toml` documents schema v2. `hardware.pwm_gpio_bcm` and `control.fan_endpoint_ids` each contain exactly two entries in Fan 1/Fan 2 order. Both endpoint IDs must name configured `[[dgx]]` IDs; for one DGX, repeat its ID. Normal curve, hysteresis, startup boost, tach expectation, duty, and TUI status are independent per fan. The curve and max-speed cap are intentionally shared. All configured endpoints must be fresh and healthy; any endpoint failure, missing mapped temperature, emergency temperature on **any** configured GPU, or a reported fan stall forces **both** PWM channels to 100%. A runtime UI Off is ignored by those safety states. Normal recovery has a dwell period. Version-1 scalar `pwm_gpio_bcm = 18` files are rejected: change to `version = 2`, use `[18, 19]`, and add `fan_endpoint_ids` before starting the app.

`hardware.shutdown_mode` is optional and defaults to `"full"`: every release, including startup/setup failure and an application error, commands both fans to full duty. Set `shutdown_mode = "off"` only for a verified fan such as the direct-wired Noctua NF-A6x25 5V PWM, whose documented 0% PWM speed is 0 RPM. It stops both fans only after Textual returns with exit code 0; it keeps PWM channels enabled at 0% duty. A non-zero/internal Textual failure, raised exception, failed stop write, tach join timeout, or GPIO release failure instead returns both channels to full duty. This is not a boot-time, SIGKILL, terminal-kill, or power-loss guarantee; use a hardware power switch if default-off under those conditions is required.

For two DGX endpoints, pin the physical airflow mapping explicitly:

```toml
[control]
fan_endpoint_ids = ["dgx-1", "dgx-2"] # Fan 1 -> DGX-1, Fan 2 -> DGX-2

[hardware]
pwm_gpio_bcm = [18, 19] # Fan 1 -> BCM18, Fan 2 -> BCM19
```

## Raspberry Pi 4 wiring

This direct-wiring guide is for **Noctua NF-A6x25 5V PWM only**. Do not apply it to another fan merely because it has four wires: confirm that model's connector, logic, tach, voltage, and current requirements in its own data sheet. Disconnect power before wiring.

| Function | NF-A6x25 wire / pin* | Fan 1 | Fan 2 | Raspberry Pi 4 |
| --- | --- | --- | --- | --- |
| Ground | black / pin 1 | black | black | Pi GND (for example physical pin 6 or 9); all supplies share ground |
| Fan power | yellow / pin 2 | yellow +5 V | yellow +5 V | Pi physical pin 2/4 only with proven supply headroom, otherwise a regulated external 5 V supply |
| Tach / RPM | green / pin 3 | green → BCM23 / physical 16 | green → BCM24 / physical 18 | Keep green tach wires separate |
| PWM control | blue / pin 4 | blue → BCM18 / physical 12 (PWM0) | blue → BCM19 / physical 35 (PWM1) | Direct, independent GPIO connections |

\* Colour/pin mapping is specific to this exact Noctua model.

Connect each blue PWM line directly to its GPIO. For this model, do **not** add an NPN/MOSFET, level shifter, or external PWM pull-up; do not join the blue wires. The Noctua input accepts 3.3 V CMOS logic. Use 25 kHz non-inverted PWM and set `pwm_inverted = false` for this direct connection.

### Tach, power, and grounding

Do not join tach wires. The NF-A6x25 green tach output is open collector:

```text
Fan 1 green tach ---------------- BCM23 / physical pin 16
Fan 2 green tach ---------------- BCM24 / physical pin 18
```

The app enables the Pi's internal 3.3 V pull-up (`PUD_UP`) on these open-collector tach inputs; this is the baseline direct connection, not a floating input. Set `pulses_per_revolution = [2, 2]`. For noisy/long wiring, Noctua documents an optional 1 kΩ pull-up to **3.3 V** and an optional 1 µF non-polar capacitor; never pull tach up to 5 V.

Each fan is 0.187 A typical and 0.26 A maximum; two are 0.374 A typical and 0.52 A maximum. Physical pins 2/4 are one Pi 5 V rail: include this load with the Pi and USB load, and verify supply/cable/connector headroom. With an external 5 V fan supply, connect ground to Pi ground but never connect its +5 V back to Pi pins 2/4 while the Pi has another supply (backfeed risk).

### Linux PWM and libgpiod first bring-up

This backend does not use pigpiod. It writes kernel Linux PWM sysfs channels and uses libgpiod falling-edge events with an internal pull-up for tach. The clone-local installer adds the overlay to the active boot configuration (`/boot/firmware/config.txt` on current Raspberry Pi OS images), installs the access helper, and reboots:

```bash
pinout
./install.sh --reboot
# After the Pi has rebooted:
ls -l /sys/class/pwm/pwmchip0 /dev/gpiochip0
./start.sh
```

The installer writes this uncommented line. Add it manually only when intentionally not using the installer:

```ini
dtoverlay=pwm-2chan,pin=18,pin2=19,func=2,func2=2
```

```toml
[hardware]
backend = "raspberry-pi"
pwm_gpio_bcm = [18, 19]
pwm_frequency_hz = 25000
pwm_inverted = false
tach_gpio_bcm = [23, 24]
shutdown_mode = "off" # optional; clean exit only, default is fail-safe "full"
```

The installer adds the login user to `gpio` and installs a narrow root-owned helper to grant access to PWM sysfs and `/dev/gpiochip0`; use `./start.sh`, not `sudo .venv/bin/dgx-fan`, after installation. Do not run analogue audio or another PWM consumer at the same time: PWM channels are shared hardware. Bring up one fan at a time: inspect unpowered wiring, verify 5 V/common ground, connect Fan 1, then measure approximately 25 kHz non-inverted duty, RPM, and curve response before adding Fan 2. Exercise endpoint loss, disconnected tach/stall, and both clean and fault exits. The default release commands full duty and keeps PWM enabled. With the explicit `shutdown_mode = "off"` option, a clean TUI exit commands 0% duty and keeps PWM enabled; any PWM/tach/GPIO cleanup fault falls back to full duty. This repository cannot prove pinmux, waveform, process-kill, boot, or physical speed behavior; measure it before unattended use. To roll back, remove `shutdown_mode = "off"` (or set `"full"`) and restart, or revert the containing commit; no rewiring is required.


Stop and remove power if any GPIO, cable, connector, or supply becomes hot; a Pi GPIO is above 3.3 V; polarity is uncertain; a fan will not start; or measured current exceeds a supply/cable rating. This project cannot validate an unknown fan, assembled circuit, or live DGX data.

### Electrical references

- [Noctua microcontroller PWM and RPM guide](https://www.noctua.at/en/support/faqs/microcontroller-guide-pwm-setup-and-rpm-monitoring)
- [Noctua NF-A6x25 5V PWM specifications](https://www.noctua.at/en/products/nf-a6x25-5v-pwm/specifications)
- [Noctua PWM specifications white paper](https://cdn.noctua.at/media/Noctua_PWM_specifications_white_paper.pdf)
- [Raspberry Pi 4 Model B datasheet and pinout](https://datasheets.raspberrypi.com/rpi4/raspberry-pi-4-datasheet.pdf)
- [Official Raspberry Pi firmware overlay README](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README)
- [Linux PWM sysfs documentation](https://docs.kernel.org/driver-api/pwm.html)
- [libgpiod Python bindings](https://libgpiod.readthedocs.io/)
The primary sources above support only the stated Noctua model. They do not verify this assembled circuit or the current release behavior.

## MVP limitations

There is no systemd daemon, Unix socket, persistent `/etc` configuration, configuration editor, meatball menu, GPU process table, native touch support, `uvx` release package, crash-restart supervisor, or live hardware verification yet. The optional tty1 console integration is deliberately simple and remains subject to target-Pi validation.
