# Configuration guide

[English README](../README.en.md) · [한국어 설정 안내](configuration.md) · [Usage guide](usage.en.md) · [Raspberry Pi wiring guide](raspberry-pi-wiring.en.md)

Copy `config.example.toml` to `config.toml`, then edit it for your actual DGX addresses and wiring. The configuration uses schema v2 and rejects invalid setting values. Unknown keys are explicitly rejected in `[dashboard]`, `[dashboard.colors]`, `[dashboard.ranges]`, each `[dashboard.ranges.<metric>]`, and `[web]`; the same blanket rule does not apply to extra keys in other tables. The **defaults** below apply when a key is omitted and may differ from values written in the example file. The app looks for a file in this order: `--config`, `DGX_FAN_CONFIG`, then `config.toml` in the current directory.

```bash
cp config.example.toml config.toml
```

## DGX and dashboard

| Setting | Value and meaning |
| --- | --- |
| `version` | Required; must be `2`. |
| `[[dgx]]` | One or two entries. Each `id` and `name` must be nonempty and unique. `url` is the DCGM exporter's `http(s)` address. |
| `dgx.memory_source` | Defaults to `"dcgm"` framebuffer memory. Use `"node-exporter"` to display DGX Spark host UMA memory. |
| `dgx.node_exporter_url` | Required `http(s)` address when `memory_source = "node-exporter"`; cannot be set for `"dcgm"`. It supplies the memory display only. GPU temperature, utilization, and fan control still require DCGM. |
| `dashboard.graph_view` | Defaults to `"graph-2"`, the compact two-DGX-column view. Explicitly set `"graph-1"` for the classic view or select it under Setting → UI. |
| `dashboard.colors.memory`, `utilization`, `temperature`, `power` | Optional foreground colors. Use a Rich color name or exact `#RRGGBB`. If omitted, MEM, UTIL, and TEMP use the terminal default; POWER uses `ansi_green`. The string `"default"` is not a valid configured value. |
| `dashboard.ranges.utilization`, `memory`, `temperature`, `power` | Optional Graph #2 display ranges. Each table needs finite numeric `min` and `max` with `min < max`. UTIL/MEM must stay within 0–100%, POWER's minimum must be nonnegative, and TEMP may have a negative minimum. Unknown range names or keys and overflowing spans are rejected. |

Graph #2 summaries and charts appear in **UTIL, MEM, TEMP, POWER** order. Each chart shows the last 120 seconds. By default, UTIL and MEM range from 0–100%, TEMP from 0–100°C or a higher emergency temperature, and POWER from 0–240W. Brightness has five value-based levels, while height follows the actual value. A previous observation is displayed briefly for at most 1.5 times the collection interval; longer gaps remain blank and are distinct from a real zero. POWER is shown only when DCGM power values are valid for all physical GPUs. The configured `graph_view` and colors are also sent to the web view.

Omitted ranges keep those defaults. To zoom the POWER chart to 0–120W, add this to `config.toml`. The MEM chart and its current value remain **percentages**, while the upper memory summary remains in GiB. Ranges apply to both DGX columns and web Graph #2; they do not change Graph #1, History, fan control, or stored values. Height and the five brightness bands use 20%-of-range intervals of `(value - min) / (max - min)`; only the drawing is clamped, not the actual value shown in the label. Restart the primary controller and web renderer after editing the file. The Setting UI has no range editor, but saving other settings preserves these tables.

```toml
[dashboard.ranges.power]
min = 0
max = 120
```

The Colors editor offers Default and standard ANSI colors. An existing valid custom name or `#RRGGBB` is retained until explicitly changed. Check the actual font and colors on the HDMI display.

## Collection and web monitor

| Setting | Value and meaning |
| --- | --- |
| `collection.interval_seconds` | Required finite number `>= 0.1`. Collection waits this long after completion, so request time also contributes to the actual completion-to-completion interval. |
| `collection.timeout_seconds` | Required finite number `>= 0.1`; DCGM request timeout. |
| `collection.stale_after_seconds` | Required finite number `>= interval_seconds`. Older DCGM telemetry is not used for fan control. |
| `collection.retry_count` | Optional integer `>= 0`; omitted default **0**, example value **3**. Number of extra retries after the initial request. Transport failures and HTTP 408, 429, and 5xx qualify. |
| `collection.retry_delay_seconds` | Optional finite number `>= 0`; omitted default and example value are both **10.0 seconds**. |
| `web.enabled` | Optional boolean, default `false`. Determines whether the primary controller publishes local monitor state; does not by itself start the web view. |
| `web.allow_control` | Optional boolean, default `false`, **independent** of `enabled`. If `true`, visitors without login can request settings saves and explicit fan On/Off. Use only on a trusted private LAN. |
| `web.host` / `web.port` | Default `127.0.0.1:8000`; port range `1..65535`. Only a numeric loopback address or exactly `0.0.0.0` for a trusted LAN is allowed. Do not expose it to the WAN or forward its port. |
| `web.socket_path` | Optional absolute path; defaults to `.dgx-fan-monitor.sock` next to the configuration file. |

The example's `retry_count = 3` overrides the omitted default of 0. A request failure does not necessarily mean an immediate safety override at that moment: a fresh prior sample can remain usable during retries, but a terminal failure or stale DCGM data triggers fallback. A node_exporter failure affects optional memory display and does not change DCGM-based safety decisions.

To use the web monitor, set `web.enabled = true`, restart the primary controller, then run `./scripts/web.sh start`. It is read-only by default. Before setting `allow_control = true`, consider who can connect and the lack of authentication. See [Web monitor in the usage guide](usage.en.md#web-monitor) for commands.

## Fan control and temperature curve

| Setting | Value and meaning |
| --- | --- |
| `control.fan_endpoint_ids` | Required list of two IDs, in Fan 1 and Fan 2 order. Each must reference a configured `[[dgx]].id`. Repeat the same ID for a single DGX. |
| `control.fan_mode` | Defaults to `"independent"`. `"linked"` applies hysteresis and the normal cap to each fan's normal target duty, then applies the higher demand to both. It does not average temperatures. |
| `control.enabled_at_startup` | Required boolean. Safety override takes priority over user Off even when this is `false`. |
| `control.max_speed_percent` | Required integer `1..100`; caps normal stage duty only. |
| `control.fallback_speed_percent` | Optional integer `0..100`, default `100`; the example also uses `100`. Used before the first valid sample and during safety override, independently of the normal cap. |
| `control.hysteresis_celsius` | Required finite number `>= 0`; temperature margin for stepping down. |
| `control.emergency_temperature_celsius` | Required finite number `>= 0`. A valid GPU temperature on any DGX at or above this threshold sends both fans to fallback. |
| `control.recovery_seconds` | Required finite number `>= 0`; time the recovery temperature must hold after safety conditions clear. |
| `[[control.stages]]` | Exactly four entries. The first three `max_temperature_celsius` values must be finite, `>= 0`, and strictly increasing; omit this key on the last stage. `speed_percent` is an integer `0..100` and cannot decrease across stages. |

Each fan chooses its stage from the highest valid GPU temperature on its mapped DGX. A threshold temperature belongs to that stage. Rising temperature moves to a higher stage immediately. To step down, temperature must be **at or below** the lower stage's upper threshold minus hysteresis. For example, with stage bounds of 50°C and 55°C and hysteresis of 2°C, a fan that has moved to the 60% stage remains there at 49°C and returns to the first stage only at 48°C or below. `recovery_seconds` applies to safety recovery, not this ordinary transition.

Fan Control's S1–S4 colors reflect the applied stage (blue, yellow, orange, red), not a stage inferred from displayed PWM. Linked mode displays the shared highest stage while retaining each fan's internal hysteresis. Startup boost may drive actual PWM to 100% while displaying the underlying stage. Off and safety display `S-`.

### Safety override

Safety takes priority over the normal curve and user Off. Both fans run at `fallback_speed_percent` without the normal cap. Main on-screen reasons include `endpoint unavailable` (before the first sample, stale data, or terminal error), `no valid GPU temperature`, `fan stalled`, and `emergency temperature`. A stalled tach latches if it does not recover after one retry boost; the latch remains until the app restarts. Check wiring and the fan.

During recovery, the highest GPU temperature must be **strictly below** `emergency_temperature_celsius - hysteresis_celsius`, with every safety condition cleared for `recovery_seconds`. With the example's 75°C, 2°C, and 10 seconds, this means **below 73°C** for 10 seconds. Lowering the safety output also reduces emergency and stall cooling; do not use it unattended on real hardware without validation.

## Hardware

| Setting | Value and meaning |
| --- | --- |
| `hardware.backend` | Required `"fake"` or `"raspberry-pi"`. The example uses development-only `"fake"`. |
| `hardware.pwm_gpio_bcm` | Required two distinct GPIOs in Fan 1 and Fan 2 order: one from PWM0 (`12`/`18`) and one from PWM1 (`13`/`19`), neither overlapping a tach GPIO. |
| `hardware.pwm_frequency_hz` | Required integer `>= 1`; example 25,000Hz. |
| `hardware.pwm_inverted` | Required boolean; `false` for the example's directly connected Noctua fans. |
| `hardware.tach_gpio_bcm` / `pulses_per_revolution` | Both required two-element arrays. Tach GPIOs must differ; each pulses value is an integer `>= 1`. |
| `hardware.startup_boost_seconds` | Required finite number `>= 0`; time to drive 100% after a transition from 0% to a positive target or during a tach retry. |
| `hardware.stall_timeout_seconds` | Required finite number `>= 0.1`; used for tach retry and stall detection. |
| `hardware.shutdown_mode` | Omitted safe-first default `"full"`; the example uses **`"off"`**. `"off"` commands 0% only on normal shutdown. Handled abnormal shutdown uses full duty; SIGKILL and power loss cannot guarantee cleanup. |
| `hardware.pwm_chip_path` / `gpio_chip_path` | Optional absolute paths; defaults `/sys/class/pwm/pwmchip0` and `/dev/gpiochip0`, respectively. |

Power off before changing physical wiring and consult the [Noctua NF-A6x25 5V PWM wiring guide](raspberry-pi-wiring.en.md). Do not assume the same direct connection works for other four-wire fans.

## Saving settings in the UI

Fan Control → **Setting** has Collection, Fan Speed, Fan Control, UI, Hardware, and Colors tabs. **Save and Apply** validates the entire configuration, preserves comments and unrelated fields, creates a `*.toml.bak` backup, and applies it. **Cancel** changes neither disk nor running state. If the file was edited externally, the UI may reject saving to avoid a conflict; restart the controller. On save failure or a stale revision, the draft remains in the open editor, so copy any needed values before reopening current settings.

Supported UI settings apply immediately after saving, but startup enablement takes effect at the next start, shutdown mode at the next normal shutdown, and boost duration on subsequent boosts. Endpoint URLs, low-level wiring/backend, and web listener/access changes require editing the file and restarting the relevant processes. Safe replacement of the settings file requires Linux `renameat2(RENAME_EXCHANGE)` support; on a filesystem without it, saving is rejected instead of overwriting the file.
