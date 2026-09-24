# 설정 안내

[한국어 README](../README.ko.md) · [사용 안내](usage.ko.md) · [Raspberry Pi 배선 안내](raspberry-pi-wiring.ko.md)

`config.example.toml`을 `config.toml`로 복사한 뒤 실제 DGX 주소와 배선에 맞게 수정하세요. 설정은 schema v2이며 잘못된 설정값은 거부됩니다. 알 수 없는 키를 명시적으로 거부하는 표는 `[dashboard]`, `[dashboard.colors]`, `[web]`이며 다른 표의 추가 키에는 같은 규칙이 일괄 적용되지 않습니다. 아래의 **기본값**은 키를 생략했을 때의 동작이고, 예제 파일에 적힌 값과 다를 수 있습니다. 앱은 `--config`, `DGX_FAN_CONFIG`, 현재 디렉터리의 `config.toml` 순서로 파일을 찾습니다.

```bash
cp config.example.toml config.toml
```

## DGX와 Dashboard

| 항목 | 값과 의미 |
| --- | --- |
| `version` | 필수이며 `2`입니다. |
| `[[dgx]]` | 1~2개입니다. 각 `id`와 `name`은 비어 있지 않고 서로 중복되지 않아야 합니다. `url`은 DCGM exporter의 `http(s)` 주소입니다. |
| `dgx.memory_source` | 생략 시 `"dcgm"` framebuffer 메모리입니다. DGX Spark의 host UMA 메모리를 표시하려면 `"node-exporter"`를 사용합니다. |
| `dgx.node_exporter_url` | `memory_source = "node-exporter"`일 때 필수 `http(s)` 주소입니다. `"dcgm"`에는 지정할 수 없습니다. 메모리 표시용이며 GPU 온도·사용률과 팬 제어에는 계속 DCGM이 필요합니다. |
| `dashboard.graph_view` | 생략 시 `"graph-1"`; `"graph-2"`는 두 DGX 열의 compact 화면입니다. Setting → UI에서도 선택할 수 있습니다. |
| `dashboard.colors.memory`, `utilization`, `temperature`, `power` | 선택 전경색입니다. Rich 색 이름 또는 정확한 `#RRGGBB`를 사용합니다. 키를 생략하면 MEM·UTIL·TEMP는 터미널 기본색, POWER는 `ansi_green`입니다. 문자열 `"default"`는 유효한 설정값이 아닙니다. |

Graph #2 요약과 차트는 **UTIL, MEM, TEMP, POWER** 순서입니다. 각 차트의 시간축은 최근 120초이며, UTIL·MEM은 0–100%, TEMP는 0–100°C 또는 더 높은 비상 온도까지, POWER는 0–240W를 표시합니다. 밝기는 값에 따라 다섯 단계이고 높이는 실제 값에 따라 변합니다. 이전 관측값은 수집 간격의 최대 1.5배까지만 잠깐 표시하며, 더 긴 공백과 실제 0은 구별됩니다. POWER는 모든 physical GPU의 DCGM power 값이 유효할 때만 합계를 표시합니다. 설정한 `graph_view`와 색상은 웹 화면에도 전달됩니다.

Colors 편집기는 Default와 표준 ANSI 색을 제공합니다. 기존의 유효한 custom 이름이나 `#RRGGBB`는 명시적으로 바꾸기 전까지 유지됩니다. 실제 HDMI의 글꼴·색은 화면에서 확인하세요.

## 수집과 웹 모니터

| 항목 | 값과 의미 |
| --- | --- |
| `collection.interval_seconds` | 필수 유한 수 `>= 0.1`. 수집 완료 뒤 이 간격을 기다리므로 실제 완료 간격에는 요청 시간도 더해집니다. |
| `collection.timeout_seconds` | 필수 유한 수 `>= 0.1`. DCGM 요청 timeout입니다. |
| `collection.stale_after_seconds` | 필수 유한 수 `>= interval_seconds`. 이보다 오래된 DCGM telemetry는 팬 제어에 사용하지 않습니다. |
| `collection.retry_count` | 선택 정수 `>= 0`; 생략 시 **0**, 예제 파일에는 **3**입니다. 최초 요청 뒤 추가 재시도 횟수입니다. transport 오류와 HTTP 408·429·5xx가 대상입니다. |
| `collection.retry_delay_seconds` | 선택 유한 수 `>= 0`; 생략 시·예제 모두 **10.0초**입니다. |
| `web.enabled` | 선택 boolean, 기본 `false`. Primary controller가 로컬 monitor 상태를 발행할지 결정합니다. 이것만으로 웹 화면이 시작되지는 않습니다. |
| `web.allow_control` | 선택 boolean, 기본 `false`. `enabled`와 **별개**입니다. `true`이면 로그인 없이 방문자가 설정 저장과 명시적인 팬 On/Off를 요청할 수 있습니다. 신뢰된 사설 LAN에서만 사용하세요. |
| `web.host` / `web.port` | 기본 `127.0.0.1:8000`; port는 `1..65535`. 숫자 loopback 주소 또는 신뢰된 LAN용 정확한 `0.0.0.0`만 허용합니다. WAN·port forwarding에 공개하지 마세요. |
| `web.socket_path` | 선택 절대 경로. 생략 시 설정 파일 옆 `.dgx-fan-monitor.sock`입니다. |

예제의 `retry_count = 3`은 생략 시 기본값 0을 바꾸며, 요청 실패가 반드시 같은 시각에 즉시 safety override를 뜻하지는 않습니다. 신선한 이전 sample은 재시도 중 유지될 수 있지만 terminal failure나 stale DCGM 데이터는 fallback을 일으킵니다. node_exporter 장애는 선택적 메모리 표시 문제이며 DCGM 안전 판단을 바꾸지 않습니다.

웹 모니터를 사용하려면 `web.enabled = true`로 바꾼 뒤 primary controller를 재시작하고 `./scripts/web.sh start`를 실행하세요. 기본은 읽기 전용입니다. `allow_control = true`를 사용할 때는 접속 가능 범위와 인증 부재를 먼저 확인하세요. 자세한 명령은 [사용 안내](usage.ko.md#웹-모니터)를 참고하세요.

## 팬 제어와 온도 곡선

| 항목 | 값과 의미 |
| --- | --- |
| `control.fan_endpoint_ids` | 필수 2개 ID, Fan 1·Fan 2 순서. 각 ID는 설정된 `[[dgx]].id`를 참조해야 합니다. DGX가 하나면 같은 ID를 두 번 적습니다. |
| `control.fan_mode` | 생략 시 `"independent"`. `"linked"`는 각 팬의 정상 목표 duty에 hysteresis와 normal cap을 적용한 뒤 더 높은 demand를 두 팬에 적용합니다. 온도를 평균내지 않습니다. |
| `control.enabled_at_startup` | 필수 boolean. `false`여도 safety override는 사용자 Off보다 우선합니다. |
| `control.max_speed_percent` | 필수 정수 `1..100`. 정상 stage duty만 제한합니다. |
| `control.fallback_speed_percent` | 선택 정수 `0..100`, 기본 `100`. 예제도 `100`입니다. 첫 유효 sample 전·safety override에 사용하며 normal cap과 독립적입니다. |
| `control.hysteresis_celsius` | 필수 유한 수 `>= 0`. 낮은 stage로 내려갈 때의 온도 여유입니다. |
| `control.emergency_temperature_celsius` | 필수 유한 수 `>= 0`. 어떤 DGX의 유효 GPU 온도라도 이 값 이상이면 두 팬이 fallback으로 전환됩니다. |
| `control.recovery_seconds` | 필수 유한 수 `>= 0`. safety 조건이 해제된 뒤 recovery 온도를 유지해야 하는 시간입니다. |
| `[[control.stages]]` | 정확히 4개. 처음 3개의 `max_temperature_celsius`는 유한한 `>= 0` 값으로 엄격히 증가해야 하고, 마지막은 이 키를 생략합니다. `speed_percent`는 정수 `0..100`이며 단계에 따라 감소할 수 없습니다. |

각 팬은 매핑된 DGX의 유효 GPU 온도 중 최고값으로 단계를 정합니다. 경계 온도는 해당 단계에 포함됩니다. 온도가 오르면 높은 단계로 즉시 전환하고, 내려갈 때는 낮아질 단계의 상한에서 hysteresis를 뺀 값 **이하**가 되어야 전환합니다. 예를 들어 첫 단계 상한 50°C, 다음 단계 55°C, hysteresis 2°C이면 60% 단계에 올라간 팬은 49°C에서도 유지하고 48°C 이하에서 첫 단계로 내려갑니다. `recovery_seconds`는 이 일반 전환이 아닌 safety 복귀에만 적용됩니다.

Fan Control의 S1~S4 색은 적용된 단계(파랑·노랑·주황·빨강)이며 표시 PWM 비율을 역산하지 않습니다. Linked 모드에서는 공통 최고 단계를 표시하되 내부 팬별 hysteresis는 유지합니다. Startup boost의 실제 PWM은 100%여도 기저 단계는 그대로 표시합니다. Off와 safety 중 단계는 `S-`입니다.

### Safety override

Safety는 normal curve와 사용자 Off보다 우선합니다. 두 팬 모두 normal cap 없이 `fallback_speed_percent`로 구동합니다. 화면의 주요 원인은 `endpoint unavailable`(첫 sample 전·stale·terminal error), `no valid GPU temperature`, `fan stalled`, `emergency temperature`입니다. 정지한 tach는 한 번의 재시도 boost 뒤에도 회복되지 않으면 latch되며 앱을 재시작하기 전까지 해제되지 않습니다. 배선과 팬을 확인하세요.

복귀 중 최고 GPU 온도는 `emergency_temperature_celsius - hysteresis_celsius`보다 **엄격히 낮아야** 하며, 모든 safety 조건이 해제된 채 `recovery_seconds` 동안 유지되어야 합니다. 예제의 75°C·2°C·10초에서는 **73°C 미만**을 10초 유지합니다. 안전 출력을 낮추면 비상·stall 냉각도 줄어드니 실기 검증 없이 무인 운용에 적용하지 마세요.

## 하드웨어

| 항목 | 값과 의미 |
| --- | --- |
| `hardware.backend` | 필수 `"fake"` 또는 `"raspberry-pi"`. 예제는 개발용 `"fake"`입니다. |
| `hardware.pwm_gpio_bcm` | 필수 서로 다른 GPIO 2개, Fan 1·Fan 2 순서. PWM0(`12`/`18`)와 PWM1(`13`/`19`)에서 각각 하나이며 tach GPIO와 겹치면 안 됩니다. |
| `hardware.pwm_frequency_hz` | 필수 정수 `>= 1`. 예제는 25,000Hz입니다. |
| `hardware.pwm_inverted` | 필수 boolean. 예제의 Noctua 직결은 `false`입니다. |
| `hardware.tach_gpio_bcm` / `pulses_per_revolution` | 각각 필수 2개 배열. Tach GPIO는 서로 달라야 하고 pulses 값은 각각 정수 `>= 1`입니다. |
| `hardware.startup_boost_seconds` | 필수 유한 수 `>= 0`. 0%에서 양수 목표로 바뀌거나 tach 재시도 시 100%로 구동할 시간입니다. |
| `hardware.stall_timeout_seconds` | 필수 유한 수 `>= 0.1`. Tach 미검출의 재시도·stall 판단에 사용합니다. |
| `hardware.shutdown_mode` | 생략 시 안전 우선 `"full"`; 예제는 **`"off"`**입니다. `"off"`는 정상 종료에서만 0%를 명령합니다. 처리 가능한 비정상 종료는 full duty이며 SIGKILL·전원 손실에는 cleanup을 보장할 수 없습니다. |
| `hardware.pwm_chip_path` / `gpio_chip_path` | 선택 절대 경로. 기본값은 각각 `/sys/class/pwm/pwmchip0`, `/dev/gpiochip0`입니다. |

실제 배선을 바꾸기 전에는 전원을 끄고 [Noctua NF-A6x25 5V PWM 배선 안내](raspberry-pi-wiring.ko.md)를 확인하세요. 다른 4선 팬에 동일한 직결 방식을 가정하지 마세요.

## 화면에서 설정 저장

Fan Control → **Setting**에는 Collection, Fan Speed, Fan Control, UI, Hardware, Colors 탭이 있습니다. **Save and Apply**는 전체 설정을 검증하고 주석·관련 없는 필드를 보존하며 `*.toml.bak` 백업을 만든 뒤 적용합니다. **Cancel**은 디스크와 실행 상태를 바꾸지 않습니다. 외부에서 파일을 직접 수정한 뒤에는 충돌을 막기 위해 UI 저장이 거부될 수 있으므로 controller를 다시 시작하세요. 저장 실패·stale revision에서는 draft가 열린 편집기에 남으므로 필요한 값을 복사한 뒤 현재 설정을 다시 여세요.

지원되는 UI 항목은 저장 직후 적용되지만 startup enablement는 다음 시작, shutdown mode는 다음 정상 종료, boost 시간은 이후 boost부터 영향을 줍니다. Endpoint URL, 저수준 배선/backend, 웹 listener/access 변경은 파일을 편집하고 관련 프로세스를 재시작해야 합니다. 설정 파일의 안전한 교체에는 Linux `renameat2(RENAME_EXCHANGE)` 지원이 필요하며 지원하지 않는 filesystem에서는 덮어쓰기 대신 저장이 거부됩니다.
