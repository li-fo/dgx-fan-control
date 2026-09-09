# DGX Fan Controller TUI (MVP)

[English README](README.md)

`dgx-fan`은 Raspberry Pi 4에서 최대 두대의 DCGM exporter의 GPU 정보를 읽고, 두 개의 4선 PWM 팬을 각각 독립적으로 또는 더 높은 demand를 함께 따르는 linked 모드로 제어하는 Python Textual 앱입니다. Dashboard에는 GPU 메모리·사용률·온도와 개별 팬의 상태가 표시됩니다. DCGM에서 DGX Spark 메모리 정보를 읽을 수 없기 때문에, node_exporter를 통해각 DGX의  unified-memory 점유율을 읽을 수 있습니다.

![DGX Fan Control 7inch LCE](./images/dgx-fan-control.webp)

&nbsp;

## 손쉬운 설치 및 실행 (Raspberry Pi)

먼저 [`uv`](https://docs.astral.sh/uv/getting-started/installation/)를 설치한 뒤, 프로젝트를 clone하고 일반 로그인 사용자로 설치 스크립트를 실행합니다.

```bash
git clone <repository-url> dgx-fan
cd dgx-fan
./install.sh
```

처음 실행할 때 `config.toml`이 없으면 설치 스크립트가 파일을 만들고 중지합니다. 이 파일에 DGX URL을 입력하고 `hardware.backend = "raspberry-pi"`로 설정한 뒤 `./install.sh`를 다시 실행하세요. 시스템이 준비되어 있으면 설치 후 터미널 또는 HDMI 디스플레이와 선택적 web monitor를 고르는 메뉴가 열립니다. 최초 설치에서는 재부팅이나 재로그인을 먼저 안내할 수 있습니다. 그 이후에는 다시 설치할 필요 없이 `./dgx-fan-control.sh`로 실행하세요.

제거하려면 `./uninstall.sh`를 실행하고 기본값 No인 확인에 답하세요. 의도적인 비대화형 제거에만 `./uninstall.sh --yes`를 사용합니다. 제거 후에도 clone과 `config.toml`은 유지됩니다. 준비 사항, 직접 실행 명령과 유지되는 시스템 설정은 [자세한 Raspberry Pi 설치 및 디스플레이 안내](#raspberry-pi-설치와-디스플레이)를 참고하세요.

## 개발 및 수동 실행

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

시스템 `/usr/bin/python`은 이 저장소의 `src/` 패키지를 보지 못할 수 있으므로 대체하지 마세요. 설정 탐색 순서는 `--config`, `DGX_FAN_CONFIG`, `./config.toml`입니다. Fan Control 설정 화면에서 지원하는 runtime 항목은 저장 즉시 적용되며, endpoint·배선·web listener 변경은 여전히 재시작해야 합니다. 개발 PC에서는 먼저 `hardware.backend = "fake"`를 사용하세요.

## Raspberry Pi 설치와 디스플레이

편집 가능한 `config.toml`은 clone 폴더에 유지합니다. `install.sh`는 이를 `/etc`로 복사하지 않습니다.

```bash
git clone <repository-url> dgx-fan
cd dgx-fan
cp config.example.toml config.toml
# DGX URL을 수정하고 [hardware] backend = "raspberry-pi"로 설정합니다.
./install.sh --reboot
```

필요하면 먼저 [공식 uv 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)를 따르세요. 설치 스크립트는 `uv sync --locked --extra raspberry-pi --no-dev`를 실행하고 clone-local 설정을 검증한 뒤 2채널 PWM overlay와 Raspberry Pi 4 tty1 콘솔 자동 로그인을 구성합니다. `config.toml`이 없으면 예시 파일을 만들고 중지하므로 편집 후 다시 실행하세요. `--reboot` 없이 `./install.sh`를 실행하면 설치 후 직접 재부팅할 수 있고, `./install.sh --dry-run`은 예정 동작만 확인합니다. 대화형 설치가 성공하고 PWM device와 현재 shell의 `gpio` group이 준비되어 있으면 launcher를 한 번 엽니다. 그렇지 않으면 재부팅 또는 재로그인을 안내합니다. `--no-launch`는 설치만 수행합니다.

앱은 root가 아닌 일반 사용자로 실행합니다. 일반적인 대화형 실행은 다음 명령을 사용합니다.

```bash
./dgx-fan-control.sh
```

현재 터미널 또는 연결된 HDMI 디스플레이를 선택한 뒤, 선택적 브라우저 화면의 실행 여부를 고릅니다. 시작 전에 launcher는 이 clone이 완전한 설치를 소유하는지 확인합니다. 설치가 없거나 일부만 존재하거나 다른 clone을 가리키면 기본값 No인 명시적 확인을 받은 뒤 `install.sh --no-launch`를 실행하고 준비 상태를 다시 확인해 이어갑니다. 기본은 읽기 전용이며 아래 설명처럼 신뢰된 LAN 제어를 명시적으로 활성화할 수 있습니다. 현재 터미널 모드는 foreground로 실행되고 HDMI 모드는 SSH 연결이 끊겨도 tty8 display service가 유지됩니다. 터미널에서 **Ctrl+Q**로 정상 종료하면 launcher가 선택해 시작한 web monitor는 계속 실행됩니다. 반대로 primary terminal 시작이 실패한 경우에는 launcher가 이번 실행에서 시작한 web monitor만 중지합니다.

관리 중인 HDMI display와 선택적 브라우저 모니터를 함께, 질문 없이 중지하려면 다음을 실행합니다.

```bash
./dgx-fan-control.sh stop
```

브라우저 모니터를 먼저 중지하고, 이어서 항상 HDMI display의 clean-stop 경로를 시도합니다. 이 명령은 현재 터미널에서 foreground로 실행 중인 TUI를 중지하지 않습니다. 해당 터미널로 돌아가 **Ctrl+Q**를 사용하세요.

자동화와 문제 해결을 위한 직접 명령도 계속 사용할 수 있습니다. 현재 터미널에서 실행하려면:

```bash
./scripts/start.sh
```

SSH에서 연결된 HDMI 디스플레이로 표시하려면:

```bash
./scripts/display.sh restart
./scripts/display.sh status
sudo journalctl -u dgx-fan-display.service --no-pager
```

transient display 프로세스는 tty8에서 동작하므로 SSH가 끊겨도 유지됩니다. `shutdown_mode = "off"`에서는 **Ctrl+Q**, `./scripts/display.sh stop`, `./scripts/display.sh restart`의 stop 단계가 clean-stop 경로를 사용해 0% duty를 명령합니다. 처리 가능한 비정상 종료에는 full-duty fail-safe가 적용되고, SIGKILL과 전원 손실은 cleanup을 실행할 수 없어 마지막 duty가 유지될 수 있습니다. display가 실행 중일 때 두 번째 `./scripts/start.sh`를 실행하지 마세요. hardware-owner lock이 기존 앱을 멈추지 않고 두 번째 실행을 거부합니다. `uv run dgx-fan --config config.toml`과 `uvx`는 현재 터미널의 foreground 명령입니다.

## 브라우저 모니터와 신뢰된 LAN 제어

선택적 브라우저 화면은 RPM, 그래프, 게이지와 controller 기준 설정을 포함한 같은 Textual `DGX Dashboard`, `Fan Control` 탭을 표시합니다. 기본은 **READ ONLY**입니다. 브라우저 renderer는 어떤 모드에서도 GPIO/PWM 소유권을 얻지 않습니다.

`config.toml`에서 활성화한 뒤, primary controller를 재시작하여 local monitor socket을 생성합니다.

```toml
[web]
enabled = true
allow_control = false # 기본값이며, true로 바꾸기 전에 아래 경고를 읽으세요.
host = "0.0.0.0" # 신뢰된 내부 LAN; local-only면 127.0.0.1을 유지합니다.
port = 8000
```

display 프로세스와 독립적으로 실행합니다.

```bash
./scripts/web.sh start
./scripts/web.sh status
# 브라우저 renderer만 중단하며 물리 controller는 계속 동작합니다.
./scripts/web.sh stop
```

`scripts/web.sh`는 transient `systemd --user` 서비스를 만들며, `install.sh`가 설치 또는 자동 시작하지 않습니다. Raspberry Pi의 console auto-login 사용자 세션이 활성 상태라면 일반적으로 SSH 연결이 끊겨도 계속 실행됩니다. 해당 세션 없이 SSH에서 시작해 지속 실행하려면 먼저 한 번 `sudo loginctl enable-linger "$USER"`를 실행하세요. 기본 `web.host = "127.0.0.1"`은 local-only이며 `ssh -L 8000:127.0.0.1:8000 <pi>` 같은 SSH tunnel과 함께 사용할 수 있습니다.

`web.allow_control = true`이면 **로그인 없이 모든 브라우저 방문자**가 Save and Apply와 명시적인 fan On/Off를 사용할 수 있습니다. 신뢰된 사설 LAN에서만 사용하세요. Controller가 계속 유일한 설정 파일·하드웨어 소유자이며 stale revision과 외부 파일 수정을 거부하고, 요청 전원이 Off여도 safety override를 보존합니다. LAN 접속에는 `web.host = "0.0.0.0"`도 설정하고 primary controller와 browser renderer를 재시작하세요. 이 port를 port-forward하거나 WAN firewall로 열지 말고, 신뢰할 수 없는 접근에는 읽기 전용 모드 또는 별도의 인증된 network boundary를 사용하세요.

clone과 설정을 유지한 채 프로젝트 통합만 제거하려면:

```bash
./uninstall.sh
```

제거 스크립트는 기본값 No인 확인을 요청합니다. 의도적인 비대화형 제거에는 `./uninstall.sh --yes`를 사용하세요. 현재 사용자의 browser service를 먼저 중지하고, 실패하더라도 transient display clean-stop을 이어서 시도합니다. 실제 stop 또는 cleanup이 실패하면 재시도를 위해 모든 관리 통합 파일을 보존합니다. 설정, source, `.venv`, PWM overlay와 콘솔 자동 로그인은 유지됩니다. 필요하면 `sudo raspi-config`로 자동 로그인을 끄고, 정확한 overlay 줄을 제거하거나 backup을 복원하세요.

직접 실행하는 운영 스크립트는 저장소 root에서 `scripts/`로 이동했습니다. 기존 설치는 업데이트 후 `./install.sh`를 다시 실행해 관리 중인 tty/profile 경로를 새 위치로 갱신해야 합니다. 영구적인 root-level 호환 wrapper는 설치하지 않습니다.

## 설정과 안전 동작

`config.example.toml`은 schema v2의 시작점입니다. 이를 `config.toml`로 복사하세요. Fan Control 탭의 **Setting**에서 공통 editor를 열면 Collection, Fan Speed, Fan Control, Hardware, Colors 다섯 탭이 한 section씩 표시되고 Save/Cancel action은 고정된 위치에 유지됩니다. Fan Speed 탭은 네 threshold/speed 쌍을 compact row로 표시합니다. **Save and Apply**를 누르면 전체 설정을 검증하고 comment와 관련 없는 항목을 보존하며 ignored `*.toml.bak`을 만든 뒤 승인된 revision을 적용합니다. 안전한 compare-and-swap 교체에는 Linux kernel과 설정 filesystem의 `renameat2(RENAME_EXCHANGE)` 지원이 필요하며, 지원하지 않는 filesystem에서는 overwrite fallback 없이 save를 거부합니다. **Cancel**은 disk와 runtime을 바꾸지 않습니다. 실패하거나 stale인 save에서는 열린 editor의 draft가 그대로 보입니다. 필요한 값을 복사한 뒤 cancel하고 현재 controller 설정을 다시 여세요. 느리지만 승인된 browser save는 같은 request identity로 재시도합니다. 제한된 client 확인 시간 안에 결과를 확정할 수 없으면 실패라고 단정하지 않고 outcome이 uncertain임을 표시하며, 가능하면 authoritative revision을 새로 읽고 확인을 위해 draft를 유지합니다. 외부에서 파일을 직접 수정한 경우에는 controller를 재시작하기 전까지 UI save가 거부됩니다. Startup enablement는 다음 실행, shutdown mode는 다음 clean exit, startup boost 시간은 이후 boost event부터 적용됩니다. Endpoint URL, 저수준 배선/backend, web listener/access는 파일 수정 후 재시작해야 합니다.

### Schema와 DGX endpoint

| 항목 | 의미와 허용 값 |
| --- | --- |
| `version` | 필수 정수이며 반드시 `2`여야 합니다. |
| `[[dgx]]` | 하나 또는 두 개의 endpoint 테이블입니다. `id`, `name`은 비어 있지 않고 서로 중복되지 않는 문자열이며, `url`은 비어 있지 않은 `http://` 또는 `https://` DCGM `/metrics` URL입니다. |
| `dgx.memory_source` | 선택 사항입니다. `"dcgm"`(기본값)은 DCGM framebuffer 메모리, `"node-exporter"`는 host UMA 메모리를 표시합니다. dashboard 메모리에만 영향을 주며 팬 제어에는 사용하지 않습니다. |
| `dgx.node_exporter_url` | `memory_source = "node-exporter"`일 때 필수인 비어 있지 않은 `http(s)` URL입니다. `"dcgm"`일 때는 설정할 수 없습니다. GPU 온도·사용률은 계속 DCGM이 필수입니다. |

### Dashboard 색상

`[dashboard.colors]`는 선택 사항입니다. `memory`, `utilization`, `temperature`는 각 차트의 전경색을 개별 지정합니다. Colors 탭은 Default와 Rich의 표준 ANSI 16색(`black`~`white`, `bright_black`~`bright_white`)을 색상 sample과 함께 제공합니다. Default를 선택하면 해당 항목을 생략합니다. 기존의 유효한 custom 색 이름이나 `#RRGGBB` 값은 **Current custom**으로 표시되어 명시적으로 바꾸기 전까지 그대로 유지됩니다. 설정 파일에서는 Rich가 지원하는 다른 전경색 이름이나 정확한 `#RRGGBB`도 계속 사용할 수 있지만 저장값 `"default"`는 허용되지 않습니다. 8색 터미널에서는 bright preset이 해당 기본색과 같게 표시될 수 있습니다.

### 브라우저 모니터

| 항목 | 의미와 검증 조건 |
| --- | --- |
| `web.enabled` | 선택 boolean이며 기본값은 `false`입니다. `true`이면 primary controller가 제한된 monitor state를 발행합니다. 변경 후 primary 앱을 재시작하세요. |
| `web.allow_control` | 선택 boolean이며 기본값은 `false`입니다. `true`이면 모든 방문자가 로그인 없이 설정 화면의 전체 항목을 편집하고 명시적 fan On/Off를 요청할 수 있습니다. 신뢰된 사설 LAN 전용이며 변경 후 primary 앱과 browser renderer를 재시작하세요. |
| `web.host` | 선택 숫자 loopback 주소이며 기본값은 `127.0.0.1`입니다. 정확히 `0.0.0.0`만 인증 없는 신뢰된 LAN bind로 추가 허용되며 `http://<현재-Pi-IP>:8000`으로 접속합니다. CIDR 문자열, hostname, IPv6 wildcard, unicast/multicast/link-local/reserved 주소는 거부됩니다. 이 listener를 port-forward하거나 WAN에 열지 마세요. |
| `web.port` | 선택 정수 `1..65535`이며 기본값은 `8000`입니다. |
| `web.socket_path` | 선택 absolute Unix socket 경로입니다. 기본값은 설정 파일 옆의 `.dgx-fan-monitor.sock`입니다. Monitor socket과 sibling control socket은 같은 사용자만 접근하는 mode `0600`이며 primary 앱 종료 시 제거됩니다. Browser process는 hardware에 직접 접근하지 않습니다. |

브라우저 client는 현재 120초 그래프 이력, 적용된 색상·주기, settings revision, 요청 전원이 포함된 완전한 versioned replacement snapshot을 받습니다. Settings와 power는 fresh하고 compatible한 controller frame이 확인된 뒤에만 활성화되며, legacy frame, 연결 끊김, controller identity 변경 또는 publisher stall에서는 다시 비활성화됩니다. 오래된 revision은 무시하며 malformed, publish error, 연결 끊김 또는 publisher stall 데이터는 정상 telemetry로 취급하지 않고 monitor-stream 상태로 표시합니다. Controller 종료 시 새 mutation과 listener를 먼저 닫고 hardware에 fail-safe full duty를 명령한 다음, 이미 승인된 persistence를 제한된 시간 동안만 조정합니다. Kernel-uninterruptible filesystem I/O는 process exit와 최종 clean-`off` release를 지연시킬 수 있지만 그 전에 수행되는 safe-full duty 명령은 지연시키지 않습니다. Browser의 시작·중지·재연결 또는 **Ctrl+Q**는 해당 renderer에만 영향을 주며 primary controller를 중지하지 않습니다. textual-serve의 launch page를 건너뛰려면 `?delay` 없이 served URL을 여세요.

### History

공유 **History** 탭은 active configuration 옆 `data/history.sqlite3`에 최대 8일의 original DCGM 및 설정된 node-exporter response를 보관합니다. Primary controller만 writer이며 local과 browser view는 선택한 endpoint, UTC range, 제한된 chart width만 요청합니다. Browser History는 `web.allow_control = false`여도 read-only로 사용할 수 있고, 별도 collector를 시작하거나 8일 raw data를 live monitor stream으로 받지 않습니다.

Memory, UTIL, temperature, GPU power는 누락 sample을 gap으로 표시합니다. Exporter가 complete physical-GPU value를 제공하지 못하면 GPU power는 **N/A**입니다. Queue, storage, query warning은 History에 표시되며 fan control을 중지하지 않습니다. Database와 SQLite sidecar는 Git에서 무시되고 restart, install, uninstall 후에도 보존됩니다. 저장된 history를 의도적으로 폐기할 때만 직접 제거하세요.

History에서 DGX endpoint를 선택하세요. 기본값은 최근 1시간이며 **+**/**−**로 1분, 10분, 1시간, 6시간, 1일, 8일을 선택하고 drag 또는 arrow key로 pan할 수 있습니다. **Now**를 선택하면 live following으로 돌아갑니다. Raw `DCGM_*`와 설정된 `node_memory_*` response만 collection cadence로 store에 기록됩니다. 각 column에서 Memory는 mean이며 UTIL, temperature, physical-GPU power 합계는 maximum입니다. 정확한 8일 cutoff는 startup과 매 1분마다 유지되며 cleanup은 chunk 단위이므로 앱이 중간에 종료되면 다음 start에서 이어집니다. Database 크기는 exporter response cardinality와 gap에 따라 달라집니다.

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
| `control.fan_mode` | 선택값 `"independent"`(기본값) 또는 `"linked"`입니다. independent는 각 매핑 endpoint의 hysteresis 적용, normal cap 제한 곡선 demand를 사용합니다. linked는 두 demand 중 큰 값을 두 팬에 적용하며 온도를 평균내지 않습니다. startup/tach boost도 더 높은 최종 duty로 두 출력을 함께 올립니다. |
| `control.enabled_at_startup` | 필수 boolean입니다. `true`이면 자동 제어로 시작하며, `false`이면 safety override가 없는 한 사용자 Off 상태로 시작합니다. |
| `control.max_speed_percent` | 필수 정수 `1..100`입니다. 정상 stage duty만 제한합니다. |
| `control.fallback_speed_percent` | 선택 정수 `0..100`이며 생략 시 `100`입니다. 첫 유효 sample 전과 safety override 동안 두 팬에 적용되며 `max_speed_percent`와 독립적입니다. |
| `control.hysteresis_celsius` | 필수 유한 수 `>= 0`입니다. 온도 변동 시 팬이 즉시 낮은 stage로 내려가는 것을 방지합니다. |
| `control.emergency_temperature_celsius` | 필수 유한 수 `>= 0`입니다. 유효 GPU 온도 하나라도 이 값 이상이면 두 팬이 fallback으로 전환됩니다. |
| `control.recovery_seconds` | 필수 유한 수 `>= 0`입니다. 아래에 설명한 safety recovery dwell 시간입니다. |

**Setting > Fan Control**에서 **Independent** 또는 **Linked (higher demand)**를 고르고 **Save and Apply**를 누르면 저장 및 즉시 적용됩니다. 이 mode만 바꾸면 endpoint mapping, endpoint별 stage/hysteresis, safety/boost 상태는 유지되고 다음 evaluation에서 출력 coordination만 바뀝니다. `config.toml`을 직접 편집하면 controller 재시작이 필요합니다. normal cap은 linked demand를 비교하기 전에 계속 적용되고, safety fallback은 이 cap과 독립적입니다. 두 editor가 같은 setting을 보이도록 updated controller와 display/web monitor 코드를 함께 실행하세요.

#### Safety override

Safety override는 normal control과 UI Off보다 우선합니다. independent와 linked mode 모두에서 `max_speed_percent`를 적용하지 않고 **두** 팬을 `fallback_speed_percent`(기본값 `100`)로 구동합니다. 화면에 표시되는 reason은 다음 중 하나입니다.

- `endpoint unavailable`: 설정된 DCGM endpoint에 prior sample이 없거나(첫 sample 전 포함), stale 상태이거나, terminal collection error가 있습니다. retry 중에는 fresh prior sample을 계속 사용할 수 있으므로 모든 일시적인 request failure가 즉시 fallback을 작동시키지는 않지만, terminal error는 cache가 fresh여도 fallback을 작동시킵니다.
- `no valid GPU temperature`: 어느 한 팬의 mapped endpoint에 유효한 GPU temperature가 없습니다.
- `fan stalled`: 이전 duty가 양수인 fan에 tach가 없어 한 번의 tach-retry startup boost를 사용한 뒤, 다음 `stall_timeout_seconds` 동안에도 tach가 없습니다. 이 latch는 앱을 재시작할 때까지 unsafe 상태이며 Off 또는 settings 저장으로 해제되지 않습니다. 재시작하기 전에 fan과 wiring을 점검하세요.
- `emergency temperature`: unmapped endpoint를 포함해 수집된 유효 GPU temperature 하나라도 `emergency_temperature_celsius` 이상입니다.

node exporter failure는 선택적인 memory display에만 영향을 주며 이 override를 작동시키지 않습니다. `STARTUP BOOST`는 safety reason이 아닌 별도의 normal-control state입니다. recovery 중에는 temperature boundary를 충족할 때까지 `safety recovery temperature`가 표시되고, timer가 동작하는 동안에는 `safety recovery dwell`이 표시됩니다. 모든 safety 조건이 해제된 후에도 유효 GPU temperature의 최고값이 `emergency_temperature_celsius - hysteresis_celsius`보다 **엄격히 낮은** 상태를 `recovery_seconds` 동안 연속 유지해야 하며, 중간에 조건이 깨지면 dwell이 다시 시작됩니다. 기본 emergency threshold 75°C, hysteresis 2°C, recovery 10초에서는 73°C **미만**을 10초 동안 유지해야 합니다. 이 dwell은 일반 stage 전환에는 적용되지 않습니다. `fallback_speed_percent`를 낮추면 emergency와 stalled-fan output도 함께 낮아지며, low-level PWM/GPIO failure는 별도의 full-speed recovery 동작을 유지합니다.

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

영구 restart daemon, endpoint/배선 editor, meatball 메뉴, GPU process table, 물리 console software keyboard, `uvx` 릴리스 패키지, 실제 하드웨어 검증은 아직 없습니다. 선택적 tty1 통합은 transient systemd unit을 사용하며, boot, PWM 파형, RPM, fan fail-safe, 물리 디스플레이 동작은 대상 Pi에서 검증해야 합니다.
