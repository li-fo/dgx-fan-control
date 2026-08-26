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

Start with `hardware.backend = "fake"` on a development machine. Its default tach simulation reports plausible running RPM while PWM is nonzero; tests can explicitly inject `NO TACH` readings for stall probes. For a Pi install `.[raspberry-pi]`, run `pigpiod`, and change the backend to `raspberry-pi` only after wiring has been checked.

## Configuration and safety

`config.example.toml` documents schema v2. `hardware.pwm_gpio_bcm` and `control.fan_endpoint_ids` each contain exactly two entries in Fan 1/Fan 2 order. Both endpoint IDs must name configured `[[dgx]]` IDs; for one DGX, repeat its ID. Normal curve, hysteresis, startup boost, tach expectation, duty, and TUI status are independent per fan. The curve and max-speed cap are intentionally shared. All configured endpoints must be fresh and healthy; any endpoint failure, missing mapped temperature, emergency temperature on **any** configured GPU, or a reported fan stall forces **both** PWM channels to 100%. A runtime UI Off is ignored by those safety states. Normal recovery has a dwell period. Version-1 scalar `pwm_gpio_bcm = 18` files are rejected: change to `version = 2`, use `[18, 19]`, and add `fan_endpoint_ids` before starting the app.

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

### pigpio ownership, daemon, and first bring-up

The code calls pigpio `hardware_PWM` on BCM18 (PWM0) and BCM19 (PWM1) at the configured 25 kHz. For this pigpio backend, do **not** add `dtoverlay=pwm` or `dtoverlay=pwm-2chan`. If either PWM overlay is inherited from an image or another project, remove/disable it before live use unless an operator has independently established exclusive ownership and compatibility. Do not run analogue audio or another PWM consumer concurrently: the PWM peripheral/channels are shared hardware resources. The Raspberry Pi overlay documentation is linked below for pin/channel/resource facts, not as a setup requirement for this backend.

Verify the physical mapping, install the Pi extra, start `pigpiod`, and check the configuration before selecting the real backend:

```bash
pinout
uv pip install -e '.[raspberry-pi]'
sudo systemctl enable --now pigpiod
uv run dgx-fan --config config.toml
```

```toml
[hardware]
backend = "raspberry-pi"
pwm_gpio_bcm = [18, 19]
pwm_frequency_hz = 25000
pwm_inverted = false
tach_gpio_bcm = [23, 24]
```

Bring up one fan at a time: inspect unpowered wiring, verify 5 V/common ground, connect Fan 1, then confirm approximately 25 kHz non-inverted duty, RPM, and curve response before adding Fan 2. Exercise endpoint loss, disconnected tach/stall, and app exit. The software commands 100% duty for its documented safety states. Current release calls `hardware_PWM(gpio, 0, 0)` and does **not** prove a GPIO input/Hi-Z transition or full-speed-after-quit on live hardware; measure that behavior before unattended use.

Stop and remove power if any GPIO, cable, connector, or supply becomes hot; a Pi GPIO is above 3.3 V; polarity is uncertain; a fan will not start; or measured current exceeds a supply/cable rating. This project cannot validate an unknown fan, assembled circuit, or live DGX data.

### Electrical references

- [Noctua microcontroller PWM and RPM guide](https://www.noctua.at/en/support/faqs/microcontroller-guide-pwm-setup-and-rpm-monitoring)
- [Noctua NF-A6x25 5V PWM specifications](https://www.noctua.at/en/products/nf-a6x25-5v-pwm/specifications)
- [Noctua PWM specifications white paper](https://cdn.noctua.at/media/Noctua_PWM_specifications_white_paper.pdf)
- [Raspberry Pi 4 Model B datasheet and pinout](https://datasheets.raspberrypi.com/rpi4/raspberry-pi-4-datasheet.pdf)
- [Official Raspberry Pi firmware overlay README](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README)
- [pigpio `gpioHardwarePWM` implementation (sets GPIO mode and PWM clock)](https://github.com/joan2937/pigpio/blob/master/pigpio.c#L12319-L12410)
The primary sources above support only the stated Noctua model. They do not verify this assembled circuit or the current release behavior.

## MVP limitations

There is no systemd daemon, Unix socket, persistent `/etc` configuration, configuration editor, meatball menu, GPU process table, native touch support, `uvx` release package, or live hardware verification yet. Those are intentionally deferred until the direct-Python MVP is proven on a target Pi and DGX.
