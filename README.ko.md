# DGX Fan Controller TUI (MVP)

[English README](README.md)

`dgx-fan`은 Raspberry Pi 4에서 최대 두 DCGM exporter의 GPU 정보를 읽고, 두 개의 4선 PWM 팬을 각각 독립적으로 제어하는 Python Textual 앱입니다. Dashboard에는 GPU 메모리·사용률·온도와 팬별 매핑 DGX, duty, RPM, 상태가 표시됩니다. DGX Spark endpoint는 선택적으로 node_exporter에서 host unified-memory 점유율을 읽을 수 있습니다.

![DGX Fan Control 7inch LCE](./images/dgx-fan-control.webp)

&nbsp;

## 로컬 실행

Python 3.11+와 개발 환경을 준비합니다.

```bash
uv venv
uv pip install -e '.[dev]'
cp config.example.toml config.toml
```

가상환경을 활성화한 뒤 Python 또는 console entry point로 실행합니다.

```bash
source .venv/bin/activate
python -m dgx_fan --config config.toml
# 또는: dgx-fan --config config.toml
```

활성화 없이 uv를 통해 실행할 수도 있습니다.

```bash
uv run dgx-fan --config config.toml
```

시스템 `/usr/bin/python`은 이 저장소의 `src/` 패키지를 보지 못할 수 있으므로 대체하지 마세요. 설정 탐색 순서는 `--config`, `DGX_FAN_CONFIG`, `./config.toml`이며, 설정을 수정하면 재시작해야 합니다. 설정 UI와 hot reload는 없습니다. 개발 PC에서는 먼저 `hardware.backend = "fake"`를 사용하세요.

## Raspberry Pi 설치와 디스플레이

편집 가능한 `config.toml`은 clone 폴더에 유지합니다. `install.sh`는 이를 `/etc`로 복사하지 않습니다.

```bash
git clone <repository-url> dgx-fan
cd dgx-fan
cp config.example.toml config.toml
# DGX URL을 수정하고 [hardware] backend = "raspberry-pi"로 설정합니다.
./install.sh --reboot
```

필요하면 먼저 [공식 uv 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)를 따르세요. 설치 스크립트는 `uv sync --locked --extra raspberry-pi --no-dev`를 실행하고 clone-local 설정을 검증한 뒤 2채널 PWM overlay와 Raspberry Pi 4 tty1 콘솔 자동 로그인을 구성합니다. `config.toml`이 없으면 예시 파일을 만들고 중지하므로 편집 후 다시 실행하세요. `--reboot` 없이 `./install.sh`를 실행하면 설치 후 직접 재부팅할 수 있고, `./install.sh --dry-run`은 예정 동작만 확인합니다.

앱은 root가 아닌 일반 사용자로 실행합니다. 설치 후 현재 터미널에서 실행하려면:

```bash
./start.sh
```

SSH에서 연결된 HDMI 디스플레이로 표시하려면:

```bash
./display.sh restart
./display.sh status
sudo journalctl -u dgx-fan-display.service --no-pager
```

transient display 프로세스는 tty8에서 동작하므로 SSH가 끊겨도 유지됩니다. `shutdown_mode = "off"`에서는 **Ctrl+Q**, `./display.sh stop`, `./display.sh restart`의 stop 단계가 clean-stop 경로를 사용해 0% duty를 명령합니다. 처리 가능한 비정상 종료에는 full-duty fail-safe가 적용되고, SIGKILL과 전원 손실은 cleanup을 실행할 수 없어 마지막 duty가 유지될 수 있습니다. display가 실행 중일 때 두 번째 `./start.sh`를 실행하지 마세요. hardware-owner lock이 기존 앱을 멈추지 않고 두 번째 실행을 거부합니다. `uv run dgx-fan --config config.toml`과 `uvx`는 현재 터미널의 foreground 명령입니다.

## 읽기 전용 브라우저 모니터

선택적 브라우저 화면은 RPM, 그래프, 게이지를 포함한 같은 Textual `DGX Dashboard`, `Fan Control` 탭을 표시합니다. 이 화면은 의도적으로 **READ ONLY**입니다. 브라우저 방문자는 팬을 토글하거나 설정을 수정할 수 없고, GPIO/PWM 소유권이나 fallback 동작에도 영향을 줄 수 없습니다.

`config.toml`에서 활성화한 뒤, primary controller를 재시작하여 local monitor socket을 생성합니다.

```toml
[web]
enabled = true
host = "0.0.0.0" # 신뢰된 내부 LAN; local-only면 127.0.0.1을 유지합니다.
port = 8000
```

display 프로세스와 독립적으로 실행합니다.

```bash
./web.sh start
./web.sh status
# 브라우저 renderer만 중단하며 물리 controller는 계속 동작합니다.
./web.sh stop
```

`web.sh`는 transient `systemd --user` 서비스를 만들며, `install.sh`가 설치 또는 자동 시작하지 않습니다. Raspberry Pi의 console auto-login 사용자 세션이 활성 상태라면 일반적으로 SSH 연결이 끊겨도 계속 실행됩니다. 해당 세션 없이 SSH에서 시작해 지속 실행하려면 먼저 한 번 `sudo loginctl enable-linger "$USER"`를 실행하세요. 기본 `web.host = "127.0.0.1"`은 local-only이며 `ssh -L 8000:127.0.0.1:8000 <pi>` 같은 SSH tunnel과 함께 사용할 수 있습니다. 신뢰된 내부 LAN에서 직접 접속하려면 `web.host = "0.0.0.0"`으로 명시한 뒤 primary controller를 재시작하고 `http://<현재-Pi-IP>:8000`(예: `http://192.168.1.7:8000`)으로 여세요. wildcard bind는 DHCP 및 `192.168.0.0/24`/`192.168.1.0/24` subnet 변경에도 그대로 동작하지만, 사용자 인증이나 client IP 필터를 제공하지 않습니다. 이 port를 port-forward하거나 WAN firewall로 열지 마세요. 신뢰할 수 없는 네트워크나 인터넷 접근에는 loopback+SSH tunnel 또는 인증된 reverse proxy를 사용하세요.

clone과 설정을 유지한 채 프로젝트 통합만 제거하려면:

```bash
./uninstall.sh --yes
```

제거는 transient display unit을 중지한 뒤 관리된 helper를 지우지만 PWM overlay와 콘솔 자동 로그인은 의도적으로 유지합니다. 필요하면 `sudo raspi-config`로 자동 로그인을 끄고, 정확한 overlay 줄을 제거하거나 backup을 복원하세요.

## 설정과 안전 동작

`config.example.toml`은 schema v2의 시작점입니다. 이를 `config.toml`로 복사해 편집하고, 수정 후에는 반드시 앱을 재시작하세요. 아래 참조에는 예시 파일에 없는 고급 하드웨어 경로 두 항목까지 포함한 전체 지원 설정을 정리했습니다.

### Schema와 DGX endpoint

| 항목 | 의미와 허용 값 |
| --- | --- |
| `version` | 필수 정수이며 반드시 `2`여야 합니다. |
| `[[dgx]]` | 하나 또는 두 개의 endpoint 테이블입니다. `id`, `name`은 비어 있지 않고 서로 중복되지 않는 문자열이며, `url`은 비어 있지 않은 `http://` 또는 `https://` DCGM `/metrics` URL입니다. |
| `dgx.memory_source` | 선택 사항입니다. `"dcgm"`(기본값)은 DCGM framebuffer 메모리, `"node-exporter"`는 host UMA 메모리를 표시합니다. dashboard 메모리에만 영향을 주며 팬 제어에는 사용하지 않습니다. |
| `dgx.node_exporter_url` | `memory_source = "node-exporter"`일 때 필수인 비어 있지 않은 `http(s)` URL입니다. `"dcgm"`일 때는 설정할 수 없습니다. GPU 온도·사용률은 계속 DCGM이 필수입니다. |

### Dashboard 색상

`[dashboard.colors]`는 선택 사항입니다. `memory`, `utilization`, `temperature`는 각 차트의 전경색을 개별 지정합니다. Rich가 지원하는 색 이름(예: 8색 터미널의 `"ansi_yellow"`) 또는 정확한 `#RRGGBB`를 사용하세요. 항목을 생략하면 터미널 기본색을 사용하며, `"default"` 값은 허용되지 않습니다.

### 브라우저 모니터

| 항목 | 의미와 검증 조건 |
| --- | --- |
| `web.enabled` | 선택 boolean이며 기본값은 `false`입니다. `true`이면 primary controller가 제한된 읽기 전용 monitor state를 발행합니다. 변경 후 primary 앱을 재시작하세요. |
| `web.host` | 선택 숫자 loopback 주소이며 기본값은 `127.0.0.1`입니다. 정확히 `0.0.0.0`만 인증 없는 신뢰된 LAN bind로 추가 허용되며 `http://<현재-Pi-IP>:8000`으로 접속합니다. CIDR 문자열, hostname, IPv6 wildcard, unicast/multicast/link-local/reserved 주소는 거부됩니다. 이 listener를 port-forward하거나 WAN에 열지 마세요. |
| `web.port` | 선택 정수 `1..65535`이며 기본값은 `8000`입니다. |
| `web.socket_path` | 선택 absolute Unix socket 경로입니다. 기본값은 설정 파일 옆의 `.dgx-fan-monitor.sock`입니다. socket은 같은 사용자만 접근할 수 있도록 mode `0600`이며, command를 받지 않고 primary 앱 종료 시 제거됩니다. |

브라우저 client는 현재 120초 그래프 이력이 포함된 완전한 versioned replacement snapshot을 받습니다. 오래된 revision은 무시하며 malformed, publish error, 연결 끊김 또는 publisher stall 데이터는 정상 telemetry로 취급하지 않고 monitor-stream 상태로 표시합니다. browser client의 시작·중지·재연결은 fan duty나 primary 앱의 수명에 영향을 주지 않습니다. textual-serve의 launch page를 건너뛰려면 `?delay` 없이 served URL을 여세요. monitor stream이 시작되면 자동으로 연결되어 화면이 바뀝니다.

### Collection

| 항목 | 의미와 검증 조건 |
| --- | --- |
| `collection.interval_seconds` | 수집 주기(초)입니다. 유한한 수이며 `>= 0.1`이어야 합니다. |
| `collection.timeout_seconds` | DCGM 요청 하나의 timeout(초)입니다. 유한한 수이며 `>= 0.1`이어야 합니다. |
| `collection.stale_after_seconds` | 캐시 telemetry를 허용하는 최대 시간(초)입니다. 유한한 수이고 `>= 0.1` 및 `interval_seconds` 이상이어야 합니다. 이를 넘은 데이터는 팬 제어에 안전하지 않습니다. |
| `collection.retry_count` | 최초 요청 후 추가 DCGM 요청 횟수입니다. 정수 `>= 0`이며 생략 시 `0`입니다. transport 오류와 HTTP 408, 429, 5xx는 재시도할 수 있습니다. |
| `collection.retry_delay_seconds` | 재시도 간 대기 시간(초)입니다. 유한한 수 `>= 0`이며 생략 시 `10.0`입니다. |

각 DGX는 독립적으로 수집됩니다. 새 캐시 sample도 `stale_after_seconds`까지만 사용되며, stale 또는 재시도가 소진된 DCGM 온도 telemetry는 fan fallback을 작동시킵니다. node_exporter 장애는 선택적인 메모리 표시에는 영향을 주지만 DCGM endpoint 상태나 팬 safety에는 영향을 주지 않습니다.

### Fan control

| 항목 | 의미와 검증 조건 |
| --- | --- |
| `control.fan_endpoint_ids` | 필수 두 항목 배열이며 **Fan 1, Fan 2** 순서입니다. 각 값은 설정된 `[[dgx]].id`를 참조해야 합니다. 두 팬이 같은 DGX를 냉각하면 같은 ID를 반복하세요. 각 팬은 매핑된 endpoint의 유효 GPU 온도 중 최고값을 사용합니다. |
| `control.enabled_at_startup` | 필수 boolean입니다. `true`이면 자동 제어로 시작하며, `false`이면 safety override가 없는 한 사용자 Off 상태로 시작합니다. |
| `control.max_speed_percent` | 필수 정수 `1..100`입니다. 정상 stage duty만 제한합니다. |
| `control.fallback_speed_percent` | 선택 정수 `0..100`이며 생략 시 `100`입니다. 첫 유효 sample 전과 safety override 동안 두 팬에 적용되며 `max_speed_percent`와 독립적입니다. |
| `control.hysteresis_celsius` | 필수 유한 수 `>= 0`입니다. 온도 변동 시 팬이 즉시 낮은 stage로 내려가는 것을 방지합니다. |
| `control.emergency_temperature_celsius` | 필수 유한 수 `>= 0`입니다. 유효 GPU 온도 하나라도 이 값 이상이면 두 팬이 fallback으로 전환됩니다. |
| `control.recovery_seconds` | 필수 유한 수 `>= 0`입니다. 아래에 설명한 safety recovery dwell 시간입니다. |

DCGM endpoint unavailable/stale, 매핑된 GPU 온도 누락, fan stall, 비상 온도에서는 두 팬 모두 safety override가 적용됩니다. 이 상태는 UI Off보다 우선합니다. 복구 가능한 safety 조건이 해제된 뒤에도 모든 유효 GPU의 최고 온도가 `emergency_temperature_celsius - hysteresis_celsius`보다 낮은 상태를 `recovery_seconds` 동안 연속 유지해야 자동 제어로 복귀합니다. 이 dwell은 일반 stage 전환에는 적용되지 않습니다. stall된 팬은 앱을 재시작할 때까지 unsafe 상태로 유지됩니다.

#### `[[control.stages]]`: 4단계 온도 곡선

정확히 네 개의 stage 테이블이 필요합니다. 처음 세 개는 `max_temperature_celsius`가 필요하며(유한 수 `>= 0`, 엄격히 오름차순), 네 번째는 이 항목을 반드시 생략하는 open-ended stage입니다. 모든 `speed_percent`는 정수 `0..100`이고 stage가 올라갈수록 같거나 커야 합니다. stage의 상한은 포함됩니다. 예를 들어 `max_temperature_celsius = 50`이면 50.0°C는 해당 stage이고, 50보다 큰 값은 다음 해당 stage를 선택합니다.

온도가 올라갈 때는 다음 수집/제어 갱신에서 즉시 높은 stage로 전환됩니다. 온도가 내려갈 때는, 새로 선택될 낮은 stage의 상한에서 `hysteresis_celsius`를 뺀 값 이하가 될 때까지 현재 높은 stage를 유지합니다.

예를 들어 `<= 50°C: 20%`, `<= 55°C: 60%`, `hysteresis_celsius = 2`이면, 이미 60% stage에 진입한 팬은 49°C에서도 60%를 유지합니다. **48°C 이하**가 되어야 20% stage로 내려갑니다. 정상 온도 곡선에는 시간 지연이 없으며 `recovery_seconds`는 safety override 이후에만 적용됩니다.

### Hardware

| 항목 | 의미와 검증 조건 |
| --- | --- |
| `hardware.backend` | 필수입니다. 개발용 `"fake"` 또는 Linux PWM/GPIO 하드웨어용 `"raspberry-pi"`를 사용합니다. |
| `hardware.pwm_gpio_bcm` | 필수 두 개의 서로 다른 BCM GPIO 정수이며 **Fan 1, Fan 2** 순서입니다. 하나는 PWM0(`12` 또는 `18`), 다른 하나는 PWM1(`13` 또는 `19`)이어야 하며 tach GPIO와 겹치면 안 됩니다. |
| `hardware.pwm_frequency_hz` | 필수 정수 `>= 1`이며 PWM 주파수(Hz)입니다. Noctua 안내는 25,000 Hz를 사용합니다. |
| `hardware.pwm_inverted` | 필수 boolean입니다. Noctua NF-A6x25 5V PWM 직결은 `false`입니다. |
| `hardware.tach_gpio_bcm` | 필수 두 개의 서로 다른 BCM GPIO 정수이며 **Fan 1, Fan 2** 순서입니다. PWM GPIO와 겹치면 안 됩니다. |
| `hardware.pulses_per_revolution` | 필수 두 항목 정수 배열이며 **Fan 1, Fan 2** 순서입니다. 각 값은 `>= 1`입니다. |
| `hardware.startup_boost_seconds` | 필수 유한 수 `>= 0`입니다. 팬이 0%에서 양수 목표로 바뀔 때와 tach 재시도 때 이 시간 동안 100%로 구동합니다. |
| `hardware.stall_timeout_seconds` | 필수 유한 수 `>= 0.1`입니다. 이전 duty가 양수인데 tach가 없으면 이 시간 후 한 번 startup boost를 시도하고, 다시 같은 시간이 지나도 없으면 stalled로 표시합니다. |
| `hardware.shutdown_mode` | 선택 사항입니다. `"full"`(기본값) 또는 `"off"`입니다. 정상 종료에서만 선택한 `"off"`가 적용되고, 처리 가능한 비정상 종료는 full duty를 사용합니다. SIGKILL·전원 손실은 cleanup을 할 수 없습니다. |
| `hardware.pwm_chip_path` | 선택 고급 항목입니다. PWM chip의 절대 경로이며 기본값은 `/sys/class/pwm/pwmchip0`입니다. |
| `hardware.gpio_chip_path` | 선택 고급 항목입니다. GPIO chip의 절대 경로이며 기본값은 `/dev/gpiochip0`입니다. |

`startup_boost_seconds`, `stall_timeout_seconds`는 tach/startup 시간이며, collection 재시도 시간 및 safety `recovery_seconds`와 다릅니다. `fallback_speed_percent`를 낮추면 비상·stall 냉각도 함께 낮아지므로 무인 운용 전 실기 검증이 필요합니다. 단, low-level PWM/GPIO 오류는 계속 full speed로 복구합니다.

`hardware.backend = "raspberry-pi"`를 선택하기 전 전원을 끄고 상세 [Raspberry Pi 배선 안내](docs/raspberry-pi-wiring.ko.md)를 따르세요. 이 직결 안내는 **Noctua NF-A6x25 5V PWM**에만 적용됩니다. 다른 4선 팬에는 해당 모델의 데이터시트와 실기 검증 없이 적용하지 마세요.

## MVP 한계

영구 restart daemon, 설정 편집기, meatball 메뉴, GPU process table, native touch 지원, `uvx` 릴리스 패키지, 실제 하드웨어 검증은 아직 없습니다. 선택적 tty1 통합은 transient systemd unit을 사용하며, boot, PWM 파형, RPM, fan fail-safe, 물리 디스플레이 동작은 대상 Pi에서 검증해야 합니다.
