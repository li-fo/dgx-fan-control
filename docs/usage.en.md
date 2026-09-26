# Installation, operation, and display guide

[English README](../README.en.md) · [한국어 사용 안내](usage.md) · [Configuration guide](configuration.en.md) · [Raspberry Pi wiring guide](raspberry-pi-wiring.en.md)

Power off and check wiring before connecting real PWM fans. On a development PC, first test with `hardware.backend = "fake"`. Run this repository's install and launch scripts as the regular Raspberry Pi login user; do not run the app directly as root.

## Installation and first launch

If `uv` is not installed, follow its [official installation guide](https://docs.astral.sh/uv/getting-started/installation/). Edit `config.toml` in the clone; the installer does not copy it to `/etc`.

```bash
git clone <repository-url> dgx-fan
cd dgx-fan
cp config.example.toml config.toml
# Check the DGX URL and fan mapping; set backend to "raspberry-pi" for real wiring
./install.sh --no-launch
```

If `config.toml` is missing, the installer creates a copy of the example and stops, so edit it and run the installer again. Installation prepares the clone's Python environment and sets up the Raspberry Pi 4 two-channel PWM overlay and tty1 console autologin integration. After an interactive installation, `./install.sh` may also open the launcher when prerequisites are ready. `--no-launch` only installs, `--dry-run` previews planned actions, and `--reboot` requests a reboot after installation. A first installation may require a reboot or a new login for `gpio` group membership.

The normal post-installation start command takes **no arguments**. `start` is not a supported argument.

```bash
./dgx-fan-control.sh
```

The launcher checks this clone's installation state. If it is absent, incomplete, or owned by another clone, it asks for default-**No** confirmation before installing or repairing. Canceling the prompt leaves the installation unchanged.

| Choice | Display |
| --- | --- |
| `1` | Foreground in the current terminal, which owns the app. |
| `2` | labwc and fullscreen LXTerminal on HDMI tty8. |
| `3` | Linux console fallback on HDMI tty8. |
| `h` | Show menu help and choose again. |
| `q` | Cancel launch. |

The launcher then asks `Start the optional web monitor? [y/N]`. No is the default; the chosen physical display and web monitor are separate processes. A web-start failure does not stop the chosen physical display from starting. Current-terminal mode may end with its SSH session, while both managed HDMI modes persist after disconnection.

Quit a current-terminal app normally with **Ctrl+Q**. A web monitor started alongside it keeps running after the TUI exits. To stop managed web and HDMI displays together, use the following command; it does not terminate a foreground terminal app for you.

```bash
./dgx-fan-control.sh stop
```

## Direct launch and display troubleshooting

Use `./scripts/start.sh` to run the installed app directly in the current terminal. To manage HDMI directly, use the commands below: `restart` selects graphical mode and `restart-console` selects Linux console mode. `start` and `start-console` do not replace a running display, while restart intentionally switches it.

```bash
./scripts/start.sh
./scripts/display.sh restart
./scripts/display.sh restart-console
./scripts/display.sh status
./scripts/display.sh stop
```

| Script | Supported arguments |
| --- | --- |
| `./scripts/start.sh` | No arguments for foreground launch, or `--dry-run` to check. |
| `./scripts/display.sh` | `start`, `restart`, `start-console`, `restart-console`, `stop`, `status`. |
| `./scripts/web.sh` | `start`, `restart`, `stop`, `status`. |

`scripts/graphical_session.py` is an internal component of the managed graphical session, not an operator command to launch directly.

If graphical HDMI fails to start, check that `labwc` and `lxterminal` are installed on the Pi and inspect `sudo journalctl -u dgx-fan-graphical.service --no-pager`. For console-mode logs, use `sudo journalctl -u dgx-fan-display.service --no-pager`. Verify actual HDMI font, color, touch, PAM seat access, and performance on the target Pi. If a managed helper changed after an update, rerun `./install.sh --no-launch` and restart the selected display mode.

If managed tty8 cleanup reports busy, first update the helper for this clone, inspect the result of `./scripts/display.sh stop`, and try `./scripts/display.sh start-console` if needed. Cleanup targets only the owned VT; it does not indiscriminately terminate other tty holders. If a failure remains, inspect the logs and active tty rather than forcibly deleting console processes.

Console mode uses the managed tty8 font and falls back to ordinary text values when Graph #2's large numerals cannot be drawn. Graphical mode uses a separate terminal font. On Linux VT, the fan gauge retains a simple ring; graphical and browser displays show a denser ring and larger PWM numerals when space permits. This display choice is independent of Graph #1/#2 and visits to History; a narrow screen may use a smaller gauge. Switching display modes alone does not change fan settings.

Run only one hardware-owning app at a time. If Display is running, the ownership lock rejects a second `scripts/start.sh` invocation. For direct development execution, prepare and activate a virtual environment:

```bash
uv venv
uv pip install -e '.[dev]'
source .venv/bin/activate
python -m dgx_fan --config config.toml
# Or: dgx-fan --config config.toml
```

You can also use `uv run dgx-fan --config config.toml` without activation. All of these run in the current terminal's foreground. `/usr/bin/python` outside the environment may not find this project's `src/` package.

With `hardware.shutdown_mode = "off"`, a **normal shutdown** commands 0% PWM. The default `"full"` uses full duty even on normal shutdown. Handled abnormal shutdown uses full-duty fail-safe, but SIGKILL and power loss cannot run cleanup and may leave the last duty. Unlike the default, `config.example.toml` sets `shutdown_mode = "off"`; see [Hardware in the configuration guide](configuration.en.md#hardware) for the distinction.

## Web monitor

The web view is optional and does not directly own fan hardware. Set `web.enabled = true` in `config.toml`, restart the primary controller, then start its separate renderer with the command below. `allow_control` is a separate option and defaults to `false`, making the view read-only.

```bash
./scripts/web.sh start
./scripts/web.sh status
./scripts/web.sh stop
```

The default `web.host = "127.0.0.1"` and `web.port = 8000` allow access only from the Pi. To view it from another device, use an SSH tunnel or set `host = "0.0.0.0"` only on a trusted LAN. Do not expose the unauthenticated listener to the WAN or forward its port. With `web.allow_control = true`, visitors can request settings saves and fan On/Off, so do not use it on a network without a separate authentication boundary. Restart both the primary and web renderer after changing listener or access settings.

Settings and power actions are disabled if the web connection is lost or stale state arrives. Starting, stopping, or reconnecting the web renderer, or pressing Ctrl+Q in that view, does not stop the primary controller. If the web service must survive an SSH session, check that the user `systemd` session persists. If needed, make a separate operational decision to run `sudo loginctl enable-linger "$USER"`.

## Fan Control and History

In Fan Control, the large percentage and ring represent **commanded PWM duty**; RPM is measured separately from the tach. `N/A` means a measurement is unavailable, not 0 RPM. S1–S4 and gauge colors represent the applied temperature-curve stage, not a calculation from PWM percentage. Linked mode shares the higher demand between both fans, and safety override takes priority over user Off. Consult the [configuration and safety guide](configuration.en.md#fan-control-and-temperature-curve) before changing wiring or safety settings.

The History tab queries up to eight days of collection records stored in `data/history.sqlite3` next to the configuration file. It does **not** store entire raw responses: only `DCGM_*` rows from DCGM responses and `node_memory_*` rows from the configured node_exporter response are retained in their original sample syntax. Only the primary controller records data; local and web views request only the needed endpoint, time range, and chart width. Web History is available in read-only mode and does not start a separate collector. The database and SQLite sidecars survive restart, install, and uninstall.

After selecting a DGX in History, use `+`/`−` to choose a range of 1 minute, 10 minutes, 1 hour, 6 hours, 1 day, or 8 days (default: the latest 1 hour). Reopening the tab or changing scale follows the latest range. Drag the Timeline or use left/right arrow keys to inspect a fixed past range. Missing samples remain blank. Per-column Memory uses an average; UTIL, temperature, and summed physical-GPU power use maxima. The Power chart caps its display at 240W without changing stored values. Storage or query warnings appear in History and do not stop fan control.

## Uninstall and limitations

To remove only managed integrations, run `./uninstall.sh` and answer its default-No confirmation. Use `./uninstall.sh --yes` only for intentional non-interactive removal. `--no-launch` is an **installer** option, not an uninstall option. Uninstall tries to stop the web service first, then cleanly stops HDMI. It preserves managed files if actual stopping or cleanup fails, so resolve the cause and retry. The clone, `config.toml`, `.venv`, PWM overlay, and console autologin remain.

This project was developed for a particular device setup. Verify boot, PWM waveform, RPM, fail-safe behavior, and the physical display on the target Raspberry Pi. Do not apply the [Noctua wiring guide](raspberry-pi-wiring.en.md) to another four-wire fan without validation.
