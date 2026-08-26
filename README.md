# DGX Fan Controller TUI (MVP)

`dgx-fan` is a direct-Python Textual application for a Raspberry Pi 4 that reads GPU data from up to two DCGM exporter endpoints and sets one shared PWM target for two 4-wire fans. It shows GPU memory, utilisation, temperature, fan RPM/status, and has a runtime-only On/Off control.

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

`config.example.toml` documents schema v1: one or two DCGM URLs, four ascending control stages (the fourth is unbounded), maximum fan speed, emergency threshold, and GPIO configuration. The optional `[dashboard.colors]` table controls the foreground color of the `memory`, `utilization`, and `temperature` chart boxes independently. Each key may use a Rich color name such as `yellow`, `cyan`, or `red`, or an exact six-digit hexadecimal value such as `#38bdf8`; omit the table or individual keys to retain the terminal-default color. Backgrounds, style expressions, whitespace-padded values, and shorthand hex are rejected with a field-path startup error. Edit the configuration and restart the app to apply chart-color changes. All numeric values must be finite; `nan`, `+inf`, and `-inf` are rejected before hardware construction. All endpoints must be fresh and healthy for normal curve control. Endpoint failures, stale/no-valid temperature data, emergency temperature, or a reported fan stall force 100% PWM. A runtime UI Off is ignored by those safety states. Normal recovery has a dwell period; restarting from zero applies a short full-speed boost. DCGM polls use the configured interval while a separate 250 ms control tick continues tach/stall checks from cached snapshots.

## Raspberry Pi 4 wiring

- Enable hardware PWM with `dtoverlay=pwm-2chan` in `/boot/firmware/config.txt` (or the OS-equivalent boot configuration), then reboot.
- BCM18 is the shared 25 kHz PWM output. Connect it through an NPN/N-MOSFET open-collector circuit; do **not** connect a 5 V fan PWM input directly to a Pi GPIO. `pwm_inverted` accounts for that circuit's inversion.
- BCM23 and BCM24 are independent tach inputs with 3.3 V pull-ups. Confirm the fan's pulses-per-revolution value before use.
- Check both fans' normal and startup current against the Pi 5 V supply budget before using the header for fan power. Use a separate supply if the budget is uncertain, with a common ground where required by the circuit.
- Validate PWM frequency, duty polarity, minimum stable duty, tach/RPM, endpoint loss, and shutdown behavior on the physical target. This repository cannot validate wiring or live DGX data.

On startup the app sets a safe full-speed output before reading the network. On normal exit or error it releases PWM; the external open-collector circuit should leave the fan's control input at its pull-up/default full-speed behavior.

## MVP limitations

There is no systemd daemon, Unix socket, persistent `/etc` configuration, configuration editor, meatball menu, GPU process table, native touch support, `uvx` release package, or live hardware verification yet. Those are intentionally deferred until the direct-Python MVP is proven on a target Pi and DGX.
