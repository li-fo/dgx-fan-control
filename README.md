# DGX Fan Controller TUI (MVP)

[한국어 README](README.ko.md)

`dgx-fan` is a Python Textual application for a Raspberry Pi 4. It reads GPU data from up to two DCGM exporter endpoints and independently drives two 4-wire PWM fans. The dashboard shows GPU memory, utilisation, and temperature, plus each fan's mapped DGX, duty, RPM, and state. DGX Spark endpoints can optionally use node_exporter for host unified-memory occupancy.

## Run locally

Use Python 3.11+ and install the development environment:

```bash
uv venv
uv pip install -e '.[dev]'
cp config.example.toml config.toml
```

Activate the environment to use its Python or console entry point:

```bash
source .venv/bin/activate
python -m dgx_fan --config config.toml
# or: dgx-fan --config config.toml
```

Alternatively, run through uv without activation:

```bash
uv run dgx-fan --config config.toml
```

Do not substitute `/usr/bin/python`: it does not see this repository's `src/` package unless the project is installed there. Configuration lookup is `--config`, then `DGX_FAN_CONFIG`, then `./config.toml`; restart after edits because there is no settings UI or hot reload. Begin on a development machine with `hardware.backend = "fake"`.

## Raspberry Pi installation and display

Keep the editable `config.toml` in the clone; `install.sh` does not copy it to `/etc`.

```bash
git clone <repository-url> dgx-fan
cd dgx-fan
cp config.example.toml config.toml
# Edit DGX URLs and set [hardware] backend = "raspberry-pi".
./install.sh --reboot
```

Install `uv` first using the [official instructions](https://docs.astral.sh/uv/getting-started/installation/) if needed. The installer runs `uv sync --locked --extra raspberry-pi --no-dev`, validates the clone-local configuration, adds the dual-PWM overlay, and configures Raspberry Pi 4 tty1 console auto-login. If `config.toml` is absent, it creates a copy and stops so you can edit it. Use `./install.sh` to reboot manually or `./install.sh --dry-run` to inspect its action.

The app itself runs as the regular user, never as root. After installation, launch it in the current terminal with:

```bash
./start.sh
```

To place the TUI on the connected HDMI display from SSH:

```bash
./display.sh restart
./display.sh status
sudo journalctl -u dgx-fan-display.service --no-pager
```

The transient display process runs on tty8 and continues after SSH disconnects. A keyboard attached to the Pi can exit cleanly with **Ctrl+Q**. Do not start a second foreground `./start.sh` while the display is active; the hardware-owner lock rejects it without stopping the running app. `uv run dgx-fan --config config.toml` and `uvx` remain foreground terminal commands.

Remove only this project's integration while retaining the clone and configuration:

```bash
./uninstall.sh --yes
```

Uninstall stops its transient display unit before removing managed helpers, but intentionally keeps the PWM overlay and console auto-login. Disable auto-login with `sudo raspi-config` and remove the exact overlay (or restore its backup) only when required.

## Configuration and safe operation

`config.example.toml` is the schema-v2 reference. It supports at most two `[[dgx]]` entries. `control.fan_endpoint_ids` and `hardware.pwm_gpio_bcm` each contain exactly two Fan 1/Fan 2 entries; repeat an endpoint ID when both fans serve one DGX. The normal curve, tach state, duty, and dashboard status are per-fan, while the curve and maximum-speed cap are shared.

`control.fallback_speed_percent` defaults to `100` and is independent of `max_speed_percent`. It controls both fans before the first telemetry sample and during a `SAFETY OVERRIDE`: failed or stale temperature telemetry, an emergency temperature on any configured GPU, fan stall, or recovery dwell. UI Off does not override safety. Lowering the fallback also lowers emergency and stall cooling, so validate it physically before unattended operation; low-level PWM/GPIO failures still recover to full speed.

`[collection]` can retry short DCGM disruptions:

```toml
[collection]
interval_seconds = 2.0
timeout_seconds = 1.5
stale_after_seconds = 6.0
retry_count = 3
retry_delay_seconds = 10.0
```

Each DGX is collected independently. Cached telemetry remains usable only until `stale_after_seconds`; stale or exhausted DCGM temperature data triggers the fallback. DGX Spark may set `memory_source = "node-exporter"` and `node_exporter_url` to display endpoint-wide `UMA MEM`; DCGM temperature and utilisation remain mandatory and are the only telemetry affecting fan safety. See the commented Spark example in `config.example.toml`.

`hardware.shutdown_mode` defaults to `"full"`: catchable abnormal exits command full duty. Set `"off"` only for a physically verified fan that stops at 0% PWM; it applies only after a clean TUI exit. SIGKILL and power loss cannot run cleanup and may preserve the last duty.

Before selecting `hardware.backend = "raspberry-pi"`, power off and follow the detailed [Raspberry Pi wiring guide](docs/raspberry-pi-wiring.md). It applies only to the **Noctua NF-A6x25 5V PWM** described there; do not use its direct-wiring advice for another four-wire fan without that model's data sheet and physical validation.

## MVP limitations

There is no persistent restart daemon, configuration editor, meatball menu, GPU process table, native touch support, `uvx` release package, or live hardware verification. The optional tty1 integration uses a transient systemd unit, and all boot, PWM waveform, RPM, fan fail-safe, and physical-display behavior must be validated on the target Pi.
