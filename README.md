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

This app drives **two independent PWM signals** and reads **two separate tach signals**. The exact fan data sheet always wins. The connector positions below are the usual four-wire PWM convention only: fan connector order, wire colours, voltage, and tach type can vary. Disconnect power before wiring or changing a connection.

| Function | Typical fan connector position* | Fan 1 | Fan 2 | Raspberry Pi 4 |
| --- | --- | --- | --- | --- |
| Ground | Pin 1 | GND | GND | Any Pi GND, for example physical pin 6 or 9; all supplies share this ground |
| Fan power | Pin 2 | +5 V | +5 V | Physical pin 2 or 4 **only when its supply budget is safe**; otherwise use a separate 5 V fan supply |
| Tach / RPM | Pin 3 | Tach 1 | Tach 2 | Fan 1: BCM23 / physical pin 16; Fan 2: BCM24 / physical pin 18. Each has its own external 4.7 kΩ–10 kΩ pull-up to Pi 3.3 V |
| PWM control | Pin 4 | PWM 1 | PWM 2 | Fan 1: an isolated open-collector/open-drain driver from BCM18 / physical pin 12 (PWM0); Fan 2: a separate driver from BCM19 / physical pin 35 (PWM1) |

\* Never trust a generic wire colour or connector number over the exact fan data sheet. Never connect the fan PWM line (which may have an internal 5 V pull-up), or a 5 V tach signal, directly to a Pi GPIO.

The configuration must use one GPIO from PWM0 (`BCM12` or `BCM18`) and one from PWM1 (`BCM13` or `BCM19`); the supported default is Fan 1 `BCM18` / physical 12 and Fan 2 `BCM19` / physical 35. Do not use two pins from the same PWM channel, and do not share a fan PWM line or its transistor driver between the fans.

### PWM level shifting

The Pi GPIO is 3.3 V logic. Drive each fan PWM input with its own open-collector/open-drain circuit, not directly from a Pi GPIO. Use two suitable NPN circuits such as 2N3904/2N2222:

```text
Fan 1: Pi BCM18 / physical pin 12 -- 2.2 kΩ–4.7 kΩ -- Base (NPN #1)
                                            o  base node
                                            |
                                     10 kΩ (recommended pull-down)
                                            |
                                           GND

Fan 1 PWM (typical pin 4) -------- Collector (NPN #1); emitter --- common GND
Fan 2: Pi BCM19 / physical pin 35 -- 2.2 kΩ–4.7 kΩ -- Base (NPN #2)
Fan 2 PWM (typical pin 4) -------- Collector (NPN #2); emitter --- common GND
```

A 3.3 V logic-level N-channel MOSFET can be used instead for each fan: gate from its own BCM18/BCM19 GPIO through about 100 Ω–1 kΩ, a 10 kΩ gate-to-ground pull-down, source to common ground, and drain to that fan's PWM input. Select a component specified to turn on with a 3.3 V gate drive. Do not add a Pi-side pull-up to a fan PWM line; use the fan's documented input circuit.

The supplied `pwm_inverted = true` compensates for this low-side NPN/N-MOSFET inversion: a higher requested duty creates the corresponding active-low duty at the fan input. Keep it unless a changed physical circuit has been measured and its polarity verified.

### Tach, power, and grounding

Do not join tach wires. Typical open-collector/open-drain tach wiring is:

```text
Pi 3.3 V ---- 4.7 kΩ–10 kΩ ----+---- BCM23 / physical pin 16
                                |
Fan 1 tach ---------------------+

Pi 3.3 V ---- 4.7 kΩ–10 kΩ ----+---- BCM24 / physical pin 18
                                |
Fan 2 tach ---------------------+
```

Confirm the tach output type in the fan data sheet. A 5 V push-pull tach needs a level shifter; the Pi internal pull-up is not a substitute and BCM23/24 must never receive 5 V. Set `pulses_per_revolution` to the fan's documented value (often 2, but not always).

Physical pins 2 and 4 are one Pi 5 V rail, not separate supplies. Add both fans' normal and startup current to the Pi/USB load and verify that the supply, cabling, and connector are rated for the total before using header power. If uncertain, use a separately rated 5 V fan supply. Its ground must connect to Pi ground for PWM/tach reference, but do **not** connect that supply's +5 V back to Pi physical pin 2/4 while the Pi has another supply: that can backfeed the Pi.

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
pwm_inverted = true
tach_gpio_bcm = [23, 24]
```

Bring up one fan at a time: first inspect powered-off wiring against the fan data sheet, then verify voltage/common ground with PWM and tach disconnected. Add the driver and first fan. The app commands full speed before receiving DCGM data, so expect a full-speed fail-safe output; power off if this is not expected. Confirm approximately 25 kHz PWM, active-low polarity, stage duty changes, and minimum stable duty. Then validate each tach/RPM separately, add the second fan, and exercise endpoint loss, a disconnected tach/stall, and app exit. Safety states should force full speed; PWM release should return the external driver/fan input to its documented pull-up/default full-speed state.

Stop and remove power if any GPIO, cable, transistor, connector, or supply becomes hot; a Pi GPIO is above 3.3 V; polarity is uncertain; a fan will not start; or measured current exceeds a supply/cable rating. This project cannot validate an unknown fan, assembled circuit, or live DGX data.

### Electrical references

- [Raspberry Pi GPIO and 40-pin header documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#gpio-and-the-40-pin-header)
- [Raspberry Pi 4 Model B datasheet and pinout](https://datasheets.raspberrypi.com/rpi4/raspberry-pi-4-datasheet.pdf)
- [Official Raspberry Pi firmware overlay README](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README)
- [pigpio `gpioHardwarePWM` implementation (sets GPIO mode and PWM clock)](https://github.com/joan2937/pigpio/blob/master/pigpio.c#L12319-L12410)
- [Intel four-wire fan electrical guidance (reference topology)](https://www.intel.com.tr/content/dam/www/public/us/en/documents/design-guides/celeron-400-guide.pdf)

The Intel guide describes a common four-wire PWM electrical topology. It does not certify an unknown 5 V fan's connector order, voltage, current, or behaviour.

On startup the app sets a safe full-speed output before reading the network. On normal exit or error it releases PWM; the external open-collector circuit should leave the fan's control input at its pull-up/default full-speed behavior.

## MVP limitations

There is no systemd daemon, Unix socket, persistent `/etc` configuration, configuration editor, meatball menu, GPU process table, native touch support, `uvx` release package, or live hardware verification yet. Those are intentionally deferred until the direct-Python MVP is proven on a target Pi and DGX.
