# DGX Fan Controller TUI (MVP)

[한국어 README](README.ko.md)

`dgx-fan` is a Python Textual application for a Raspberry Pi 4. It reads GPU data from up to two DCGM exporter endpoints and independently drives two 4-wire PWM fans. The dashboard shows GPU memory, utilisation, and temperature, plus each fan's mapped DGX, duty, RPM, and state. DGX Spark endpoints can optionally use node_exporter for host unified-memory occupancy.

![DGX Fan Control 7inch LCE](./images/dgx-fan-control.webp)

&nbsp;

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

Install `uv` first using the [official instructions](https://docs.astral.sh/uv/getting-started/installation/) if needed. The installer runs `uv sync --locked --extra raspberry-pi --no-dev`, validates the clone-local configuration, adds the dual-PWM overlay, and configures Raspberry Pi 4 tty1 console auto-login. If `config.toml` is absent, it creates a copy and stops so you can edit it. Run `./install.sh` without `--reboot` to install first and reboot manually afterward, or use `./install.sh --dry-run` to inspect its action.

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

The transient display process runs on tty8 and continues after SSH disconnects. With `shutdown_mode = "off"`, **Ctrl+Q**, `./display.sh stop`, and the stop phase of `./display.sh restart` use the clean-stop path and command 0% duty. Handled abnormal termination uses the full-duty fail-safe; SIGKILL and power loss cannot run cleanup and may preserve the last duty. Do not start a second foreground `./start.sh` while the display is active; the hardware-owner lock rejects it without stopping the running app. `uv run dgx-fan --config config.toml` and `uvx` remain foreground terminal commands.

## Read-only browser monitor

The optional browser view renders the same Textual `DGX Dashboard` and `Fan Control` tabs, including RPM, charts, and gauges. It is deliberately **READ ONLY**: browser visitors cannot toggle fans, change configuration, acquire GPIO/PWM ownership, or alter fallback behavior.

Enable it in `config.toml`, then restart the primary controller so it creates the local monitor socket:

```toml
[web]
enabled = true
host = "127.0.0.1"
port = 8000
```

Start it independently from the display process:

```bash
./web.sh start
./web.sh status
# Stop only the browser renderer; the physical controller keeps running.
./web.sh stop
```

`web.sh` creates a transient `systemd --user` service, so it is not installed or auto-started by `install.sh`. It normally persists after an SSH disconnect while the Raspberry Pi's console auto-login user session remains active. If you operate without that session, enable user lingering once (`sudo loginctl enable-linger "$USER"`) before relying on an SSH-started browser service. Use `http://127.0.0.1:8000` locally, or an SSH tunnel such as `ssh -L 8000:127.0.0.1:8000 <pi>`. Do not expose this unauthenticated service directly to the Internet; use a VPN or authenticated TLS reverse proxy if remote network access is required.

Remove only this project's integration while retaining the clone and configuration:

```bash
./uninstall.sh --yes
```

Uninstall stops its transient display unit before removing managed helpers, but intentionally keeps the PWM overlay and console auto-login. Disable auto-login with `sudo raspi-config` and remove the exact overlay (or restore its backup) only when required.

## Configuration and safe operation

`config.example.toml` is a schema-v2 starting point. Copy it to `config.toml`, then edit it; all changes require an application restart. The reference below covers the complete supported configuration surface, including the two optional advanced hardware paths that are not shown in the example.

### Schema and DGX endpoints

| Field | Meaning and accepted value |
| --- | --- |
| `version` | Required integer. Must be `2`. |
| `[[dgx]]` | One or two endpoint tables. `id` and `name` are non-empty, unique strings; `url` is a non-empty `http://` or `https://` DCGM `/metrics` URL. |
| `dgx.memory_source` | Optional: `"dcgm"` (default) for DCGM framebuffer memory, or `"node-exporter"` for host UMA memory. It changes dashboard memory only, never fan control. |
| `dgx.node_exporter_url` | Required non-empty `http(s)` URL when `memory_source = "node-exporter"`; forbidden with `"dcgm"`. DCGM GPU temperature and utilisation remain required. |

### Dashboard colours

`[dashboard.colors]` is optional. `memory`, `utilization`, and `temperature` independently set the foreground colour of their charts. Each may be a valid Rich foreground colour name (for example `"ansi_yellow"` on an 8-colour terminal) or an exact `#RRGGBB` value. Omit a key to use the terminal default; `"default"` is not accepted.

### Browser monitor

| Field | Meaning and validation |
| --- | --- |
| `web.enabled` | Optional boolean; defaults to `false`. When true, the primary controller publishes its bounded read-only monitor state. Restart the primary app after changing it. |
| `web.host` | Optional numeric IPv4 or IPv6 bind address; defaults to `127.0.0.1`. `localhost` names are intentionally rejected so launcher input cannot be interpreted as shell syntax. Bind a private-LAN address only after arranging network access controls. |
| `web.port` | Optional integer `1..65535`; defaults to `8000`. |
| `web.socket_path` | Optional absolute Unix-socket path. Defaults to `.dgx-fan-monitor.sock` beside the configuration file. The socket is same-user mode `0600`, command-free, and removed when the primary app exits. |

Browser clients receive a complete versioned replacement snapshot containing the current 120-second chart history. Older revisions are ignored, and malformed or disconnected monitor data is shown as a monitor-stream state rather than being treated as healthy telemetry. Starting, stopping, or reconnecting browser clients never changes fan duty or the primary app's lifetime.

### Collection

| Field | Meaning and validation |
| --- | --- |
| `collection.interval_seconds` | Poll interval in seconds; finite number `>= 0.1`. |
| `collection.timeout_seconds` | Per-request DCGM timeout in seconds; finite number `>= 0.1`. |
| `collection.stale_after_seconds` | Maximum age of cached telemetry in seconds; finite number `>= 0.1` and at least `interval_seconds`. Older data is unsafe for fan control. |
| `collection.retry_count` | Extra DCGM requests after the initial request; integer `>= 0` (defaults to `0` if omitted). Transport errors and HTTP 408, 429, and 5xx can retry. |
| `collection.retry_delay_seconds` | Delay between retry attempts in seconds; finite number `>= 0` (defaults to `10.0` if omitted). |

Each DGX is collected independently. A fresh cached sample can be used only until `stale_after_seconds`; stale or exhausted DCGM temperature telemetry activates the fan fallback. Node exporter failure affects the optional memory display, not DCGM endpoint health or fan safety.

### Fan control

| Field | Meaning and validation |
| --- | --- |
| `control.fan_endpoint_ids` | Required two-item array in **Fan 1, Fan 2** order. Each item must reference a configured `[[dgx]].id`. Repeat one ID when both fans cool the same DGX. Each fan uses the highest valid GPU temperature at its mapped endpoint. |
| `control.enabled_at_startup` | Required boolean. `true` starts normal automatic control; `false` starts in user-Off state unless safety override is active. |
| `control.max_speed_percent` | Required integer `1..100`. Caps normal stage duty only. |
| `control.fallback_speed_percent` | Optional integer `0..100`, default `100`. Duty for both fans before the first valid sample and during safety override; it is independent of `max_speed_percent`. |
| `control.hysteresis_celsius` | Required finite number `>= 0`; prevents a fan from immediately dropping to a lower stage when temperature fluctuates. |
| `control.emergency_temperature_celsius` | Required finite number `>= 0`. Any valid GPU temperature at or above it activates fallback for both fans. |
| `control.recovery_seconds` | Required finite number `>= 0`. Safety-recovery dwell, described below. |

Safety override applies to both fans for unavailable/stale DCGM endpoints, missing mapped GPU temperatures, a stalled fan, or emergency temperature. It overrides the UI Off control. After a recoverable safety condition clears, the maximum valid GPU temperature must remain below `emergency_temperature_celsius - hysteresis_celsius` continuously for `recovery_seconds` before automatic control resumes. This dwell is **not** used for ordinary stage changes. A stalled fan remains unsafe until the app is restarted.

#### `[[control.stages]]`: four-stage temperature curve

Exactly four stage tables are required. The first three require `max_temperature_celsius` (finite numbers `>= 0`, strictly ascending); the fourth must omit it and is open-ended. Every `speed_percent` is an integer `0..100`, and stage speeds must be non-decreasing. A stage upper boundary is inclusive: with `max_temperature_celsius = 50`, 50.0°C selects that stage, while any value above 50 selects the next applicable stage.

Stage changes upward happen immediately on the next collection/control update. When temperature falls, the controller holds the current higher stage until it is at or below the newly selected stage's upper boundary minus `hysteresis_celsius`.

For example, with stages `<= 50°C: 20%`, `<= 55°C: 60%`, and `hysteresis_celsius = 2`, a fan that reached the 60% stage remains at 60% at 49°C. It falls to the 20% stage only at **48°C or below**. There is no normal-curve time delay; `recovery_seconds` only applies after a safety override.

### Hardware

| Field | Meaning and validation |
| --- | --- |
| `hardware.backend` | Required: `"fake"` for development or `"raspberry-pi"` for Linux PWM/GPIO hardware. |
| `hardware.pwm_gpio_bcm` | Required two distinct BCM GPIO integers in **Fan 1, Fan 2** order. One must be PWM0 (`12` or `18`) and the other PWM1 (`13` or `19`); neither may also be a tach GPIO. |
| `hardware.pwm_frequency_hz` | Required integer `>= 1`; PWM frequency in Hz. The Noctua guide uses 25,000 Hz. |
| `hardware.pwm_inverted` | Required boolean. `false` for direct Noctua NF-A6x25 5V PWM wiring. |
| `hardware.tach_gpio_bcm` | Required two distinct BCM GPIO integers in **Fan 1, Fan 2** order; must not overlap PWM GPIOs. |
| `hardware.pulses_per_revolution` | Required two-item integer array in **Fan 1, Fan 2** order; each value is `>= 1`. |
| `hardware.startup_boost_seconds` | Required finite number `>= 0`. When a fan changes from 0% to a positive target, and for a tach restart attempt, it is driven at 100% for this duration. |
| `hardware.stall_timeout_seconds` | Required finite number `>= 0.1`. Missing tach while the previous duty is positive waits this long, attempts one startup boost, then a further timeout marks the fan stalled. |
| `hardware.shutdown_mode` | Optional: `"full"` (default) or `"off"`. Clean exit uses `"off"` only when selected; catchable abnormal exits use full duty. SIGKILL and power loss cannot clean up. |
| `hardware.pwm_chip_path` | Optional advanced absolute path to the PWM chip; defaults to `/sys/class/pwm/pwmchip0`. |
| `hardware.gpio_chip_path` | Optional advanced absolute path to the GPIO chip; defaults to `/dev/gpiochip0`. |

`startup_boost_seconds` and `stall_timeout_seconds` are tach/startup timings, distinct from collection retry timing and safety `recovery_seconds`. Lowering `fallback_speed_percent` also lowers emergency and stall cooling, so validate it physically before unattended operation; low-level PWM/GPIO failures still recover to full speed.

Before selecting `hardware.backend = "raspberry-pi"`, power off and follow the detailed [Raspberry Pi wiring guide](docs/raspberry-pi-wiring.md). It applies only to the **Noctua NF-A6x25 5V PWM** described there; do not use its direct-wiring advice for another four-wire fan without that model's data sheet and physical validation.

## MVP limitations

There is no persistent restart daemon, configuration editor, meatball menu, GPU process table, native touch support, `uvx` release package, or live hardware verification. The optional tty1 integration uses a transient systemd unit, and all boot, PWM waveform, RPM, fan fail-safe, and physical-display behavior must be validated on the target Pi.
