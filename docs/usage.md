# 설치·실행·화면 사용 안내

[한국어 README](../README.md) · [English usage guide](usage.en.md) · [설정 안내](configuration.md) · [Raspberry Pi 배선 안내](raspberry-pi-wiring.md)

실제 PWM 팬을 연결하기 전에는 전원을 끄고 배선을 확인하세요. 개발 PC에서는 먼저 `hardware.backend = "fake"`로 시험합니다. 이 저장소의 설치·실행 스크립트는 Raspberry Pi의 일반 로그인 사용자로 실행하며, 앱을 root로 직접 실행하지 않습니다.

## 설치와 첫 실행

`uv`가 없다면 [공식 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)를 따르세요. Clone 안의 `config.toml`을 편집하며 설치 스크립트는 이를 `/etc`로 복사하지 않습니다.

```bash
git clone <repository-url> dgx-fan
cd dgx-fan
cp config.example.toml config.toml
# DGX URL·팬 매핑을 확인하고 실기 배선이면 backend를 "raspberry-pi"로 변경
./install.sh --no-launch
```

`config.toml`이 없으면 설치 스크립트가 예제 복사본을 만들고 중지하므로 편집 후 다시 실행합니다. 설치는 clone의 Python 환경을 준비하고 Raspberry Pi 4의 2채널 PWM overlay와 tty1 콘솔 자동 로그인 통합을 설정합니다. `./install.sh`는 조건이 준비된 대화형 설치 후 launcher도 열 수 있습니다. `--no-launch`는 설치만, `--dry-run`은 예정 동작 확인, `--reboot`는 설치 후 재부팅 요청입니다. 최초 설치 뒤에는 재부팅이나 `gpio` 그룹 적용을 위한 재로그인이 필요할 수 있습니다.

설치 후 일반적인 시작 명령은 **인수 없는** 다음 명령입니다. `start` 인수는 지원하지 않습니다.

```bash
./dgx-fan-control.sh
```

Launcher는 현재 clone의 설치 상태를 확인합니다. 설치가 없거나 불완전하거나 다른 clone 소유이면 기본값 **No**인 확인을 받고 설치·복구한 뒤 진행합니다. 질문을 취소하면 설치를 바꾸지 않습니다.

| 선택 | 화면 |
| --- | --- |
| `1` | 현재 터미널에서 foreground로 실행. 이 터미널이 앱을 소유합니다. |
| `2` | HDMI tty8에서 labwc와 전체 화면 LXTerminal을 사용합니다. |
| `3` | HDMI tty8의 Linux 콘솔 fallback을 사용합니다. |
| `h` | 메뉴 도움말을 보고 다시 선택합니다. |
| `q` | 실행을 취소합니다. |

이어서 `Start the optional web monitor? [y/N]`를 묻습니다. 기본 No이며 선택한 물리 화면과 웹 모니터는 별도 프로세스입니다. 웹 시작이 실패해도 선택한 물리 화면은 계속 시작합니다. 현재 터미널 모드는 SSH 연결과 함께 종료될 수 있지만 두 관리형 HDMI 모드는 연결이 끊겨도 유지됩니다.

현재 터미널 앱은 **Ctrl+Q**로 정상 종료합니다. Launcher가 함께 시작한 웹 모니터는 정상 TUI 종료 뒤에도 계속 실행됩니다. 관리형 웹·HDMI를 함께 중지하려면 다음 명령을 사용합니다. 이 명령은 foreground 터미널 앱을 대신 종료하지 않습니다.

```bash
./dgx-fan-control.sh stop
```

## 직접 실행과 디스플레이 문제 해결

설치된 앱을 현재 터미널에서 직접 실행하려면 `./scripts/start.sh`를, HDMI를 직접 관리하려면 아래 명령을 사용합니다. `restart`는 그래픽 모드, `restart-console`은 Linux 콘솔 모드입니다. `start`·`start-console`은 이미 실행 중인 화면을 교체하지 않지만 restart는 의도적으로 전환합니다.

```bash
./scripts/start.sh
./scripts/display.sh restart
./scripts/display.sh restart-console
./scripts/display.sh status
./scripts/display.sh stop
```

| 스크립트 | 지원 인수 |
| --- | --- |
| `./scripts/start.sh` | 인수 없이 foreground 실행 또는 `--dry-run` 검사. |
| `./scripts/display.sh` | `start`, `restart`, `start-console`, `restart-console`, `stop`, `status`. |
| `./scripts/web.sh` | `start`, `restart`, `stop`, `status`. |

`scripts/graphical_session.py`는 관리형 그래픽 세션의 내부 구성요소이므로 운영자가 직접 시작하는 명령이 아닙니다.

그래픽 HDMI가 시작되지 않으면 Pi에 `labwc`와 `lxterminal`이 설치됐는지 확인하고 `sudo journalctl -u dgx-fan-graphical.service --no-pager`를 살펴보세요. 콘솔 모드 로그는 `sudo journalctl -u dgx-fan-display.service --no-pager`입니다. 실제 HDMI 글꼴·색·터치·PAM seat 접근·성능은 대상 Pi에서 확인해야 합니다. 업데이트 후 관리 helper가 바뀌었다면 `./install.sh --no-launch`를 다시 실행하고 선택한 화면 모드를 재시작하세요.

관리형 tty8 정리가 busy 상태라면 먼저 현재 clone의 helper를 갱신한 뒤 `./scripts/display.sh stop` 결과를 확인하고 필요하면 `./scripts/display.sh start-console`을 시도하세요. 정리 도구는 소유한 VT만 대상으로 하며 다른 tty holder를 임의로 종료하지 않습니다. 실패 메시지가 남으면 강제로 콘솔 프로세스를 지우지 말고 로그와 활성 tty를 확인하세요.

콘솔 모드는 관리된 tty8 글꼴을 사용하며 Graph #2의 큰 숫자가 불가능하면 일반 텍스트 값으로 대체합니다. 그래픽 모드는 별도 터미널 글꼴을 사용합니다. Linux VT의 팬 게이지는 단순 링을 유지하고, 그래픽·브라우저는 공간이 충분하면 촘촘한 링과 큰 PWM 숫자를 표시합니다. 화면 모드 전환만으로 팬 설정이 바뀌지는 않습니다.

한 번에 하나의 하드웨어 소유 앱만 실행하세요. Display가 작동 중일 때 다른 `scripts/start.sh`를 실행하면 소유권 잠금이 두 번째 실행을 거부합니다. 개발 환경에서 직접 실행하려면 다음처럼 가상환경을 준비·활성화하세요.

```bash
uv venv
uv pip install -e '.[dev]'
source .venv/bin/activate
python -m dgx_fan --config config.toml
# 또는: dgx-fan --config config.toml
```

활성화 없이 `uv run dgx-fan --config config.toml`을 사용할 수도 있습니다. 모두 현재 터미널의 foreground 실행입니다. 저장소 밖 `/usr/bin/python`은 이 프로젝트의 `src/` 패키지를 찾지 못할 수 있습니다.

`hardware.shutdown_mode = "off"`라면 **정상 종료** 시 0% PWM을 명령합니다. 기본 `"full"`은 정상 종료도 full duty입니다. 처리 가능한 비정상 종료에는 full-duty fail-safe가 적용되지만 SIGKILL·전원 손실은 cleanup을 실행할 수 없으므로 마지막 duty가 남을 수 있습니다. `config.example.toml`에는 기본값과 달리 `shutdown_mode = "off"`가 적혀 있으니 [설정 안내](configuration.md#하드웨어)에서 차이를 확인하세요.

## 웹 모니터

웹 화면은 선택 사항이고 팬 하드웨어를 직접 소유하지 않습니다. `config.toml`에서 `web.enabled = true`로 설정하고 primary controller를 재시작한 뒤 다음 명령으로 별도 renderer를 시작합니다. `allow_control`은 별개의 옵션이며 기본 `false`이므로 읽기 전용입니다.

```bash
./scripts/web.sh start
./scripts/web.sh status
./scripts/web.sh stop
```

기본 `web.host = "127.0.0.1"`, `web.port = 8000`은 Pi 내부에서만 접속할 수 있습니다. 다른 기기에서 보려면 SSH tunnel을 쓰거나 신뢰된 LAN에서만 `host = "0.0.0.0"`으로 설정하세요. 로그인 없이 접근 가능한 listener를 WAN·port forwarding에 노출하지 마세요. `web.allow_control = true`라면 방문자가 설정 저장과 팬 On/Off를 요청할 수 있으므로 별도 인증 경계가 없는 네트워크에는 사용하지 마세요. Listener와 접근 설정을 바꾼 뒤에는 primary와 웹 renderer를 모두 재시작합니다.

웹 연결이 끊기거나 오래된 상태가 도착하면 설정·전원 조작이 비활성화됩니다. 웹 renderer의 시작·중지·재연결과 해당 화면의 Ctrl+Q는 primary controller를 중지하지 않습니다. SSH 세션 뒤에도 웹 서비스를 유지해야 하는 환경에서는 사용자 `systemd` 세션이 살아 있는지 확인하세요. 필요하면 별도 운영 판단으로 `sudo loginctl enable-linger "$USER"`를 설정합니다.

## Fan Control과 History

Fan Control에서 큰 퍼센트와 링은 **명령한 PWM duty**이며 RPM은 별도로 측정한 tach 값입니다. `N/A`는 0 RPM이 아니라 측정값이 없다는 뜻입니다. S1~S4와 게이지 색은 온도 곡선의 적용 단계이며 PWM 비율에서 계산하지 않습니다. Linked 모드의 두 팬은 높은 demand를 공유하고, safety override는 사용자 Off보다 우선합니다. 배선과 안전 설정을 바꾸기 전에는 [설정·안전 설명](configuration.md#팬-제어와-온도-곡선)을 확인하세요.

History 탭은 설정 파일 옆 `data/history.sqlite3`에 저장된 최대 8일의 수집 기록을 조회합니다. 원본 응답 전체가 아니라 DCGM 응답의 `DCGM_*` 행과 설정된 node_exporter 응답의 `node_memory_*` 행만 원래 sample 구문으로 보관합니다. Primary controller만 기록하고, 로컬과 웹 화면은 필요한 endpoint·시간 범위·차트 폭만 요청합니다. 웹 History는 읽기 전용 모드에서도 볼 수 있으며 별도의 collector를 시작하지 않습니다. 데이터베이스와 SQLite sidecar는 restart·install·uninstall 뒤에도 보존됩니다.

History의 DGX를 고른 뒤 `+`/`−`로 1분·10분·1시간·6시간·1일·8일 범위를 선택합니다(기본 최근 1시간). 탭을 다시 열거나 배율을 바꾸면 최신 구간을 따라갑니다. Timeline을 드래그하거나 좌우 화살표 키로 과거로 이동하면 고정 구간을 살펴볼 수 있습니다. 누락 sample은 공백이며, 각 열의 Memory는 평균, UTIL·온도·physical GPU power 합계는 최대값으로 요약됩니다. Power 차트는 화면에서 240W 상한을 쓰지만 저장값은 변경하지 않습니다. 저장·조회 경고는 History에 표시되며 팬 제어를 멈추지 않습니다.

## 제거와 한계

관리 중인 통합만 제거하려면 `./uninstall.sh`를 실행하고 기본값 No인 확인에 답하세요. 의도적인 비대화형 제거에는 `./uninstall.sh --yes`를 사용합니다. `--no-launch`는 **설치** 옵션이며 제거 옵션이 아닙니다. 제거는 웹 stop을 먼저 시도하고 HDMI clean-stop을 이어서 시도합니다. 실제 중지·정리 실패 시 관리 파일을 보존하므로 원인을 해결한 뒤 재시도하세요. Clone, `config.toml`, `.venv`, PWM overlay, 콘솔 자동 로그인은 남습니다.

이 프로젝트는 특정 장치 구성에서 개발됐습니다. 부팅, PWM 파형, RPM, fail-safe와 물리 디스플레이는 대상 Raspberry Pi에서 확인해야 합니다. 다른 4선 팬에는 [Noctua 배선 안내](raspberry-pi-wiring.md)를 검증 없이 적용하지 마세요.
