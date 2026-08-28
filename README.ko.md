# DGX Fan Controller TUI (MVP)

[English README](README.md)

`dgx-fan`은 Raspberry Pi 4에서 최대 두 DCGM exporter의 GPU 정보를 읽고, 두 개의 4선 PWM 팬을 각각 독립적으로 제어하는 Python Textual 앱입니다. Dashboard에는 GPU 메모리·사용률·온도와 팬별 매핑 DGX, duty, RPM, 상태가 표시됩니다. DGX Spark endpoint는 선택적으로 node_exporter에서 host unified-memory 점유율을 읽을 수 있습니다.

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

clone과 설정을 유지한 채 프로젝트 통합만 제거하려면:

```bash
./uninstall.sh --yes
```

제거는 transient display unit을 중지한 뒤 관리된 helper를 지우지만 PWM overlay와 콘솔 자동 로그인은 의도적으로 유지합니다. 필요하면 `sudo raspi-config`로 자동 로그인을 끄고, 정확한 overlay 줄을 제거하거나 backup을 복원하세요.

## 설정과 안전 동작

`config.example.toml`은 schema v2의 기준입니다. `[[dgx]]`는 최대 두 개까지 설정할 수 있습니다. `control.fan_endpoint_ids`와 `hardware.pwm_gpio_bcm`는 Fan 1/Fan 2 순서의 정확히 두 항목이며, 한 DGX에 두 팬을 쓸 때는 endpoint ID를 반복합니다. 정상 곡선, tach 상태, duty, dashboard 상태는 팬별로 독립적이고 곡선과 최대 속도 제한만 공통입니다.

`control.fallback_speed_percent`의 기본값은 `100`이며 `max_speed_percent`와 독립적입니다. 첫 telemetry sample 전과 `SAFETY OVERRIDE`에서 두 팬에 함께 적용됩니다. 여기에는 실패하거나 stale한 온도 telemetry, 설정된 GPU 중 어느 하나의 비상 온도, fan stall, recovery dwell이 포함됩니다. UI Off는 safety를 해제하지 않습니다. 값을 낮추면 비상·stall 냉각도 낮아지므로 무인 운용 전 실기 검증이 필요하며, low-level PWM/GPIO 오류는 계속 full speed로 복구합니다.

`[collection]`에서 짧은 DCGM 연결 장애의 재시도를 설정할 수 있습니다.

```toml
[collection]
interval_seconds = 2.0
timeout_seconds = 1.5
stale_after_seconds = 6.0
retry_count = 3
retry_delay_seconds = 10.0
```

각 DGX는 독립적으로 수집됩니다. 캐시 telemetry는 `stale_after_seconds`까지만 사용할 수 있고 stale 또는 재시도 소진 DCGM 온도 데이터는 fallback을 작동시킵니다. DGX Spark는 `memory_source = "node-exporter"` 및 `node_exporter_url`로 endpoint 전체 `UMA MEM`을 표시할 수 있습니다. GPU 온도·사용률은 계속 DCGM이 필수이며 fan safety에도 DCGM telemetry만 사용됩니다. 주석 처리된 Spark 예시는 `config.example.toml`을 참조하세요.

`hardware.shutdown_mode`의 기본값은 `"full"`입니다. 처리 가능한 비정상 종료는 full duty를 명령합니다. 0% PWM에서 멈추는 것을 실기로 확인한 팬에서만 `"off"`를 사용하세요. 이는 TUI가 정상 종료할 때만 적용됩니다. SIGKILL과 전원 손실은 cleanup을 실행할 수 없어 마지막 duty가 유지될 수 있습니다.

`hardware.backend = "raspberry-pi"`를 선택하기 전 전원을 끄고 상세 [Raspberry Pi 배선 안내](docs/raspberry-pi-wiring.ko.md)를 따르세요. 이 직결 안내는 **Noctua NF-A6x25 5V PWM**에만 적용됩니다. 다른 4선 팬에는 해당 모델의 데이터시트와 실기 검증 없이 적용하지 마세요.

## MVP 한계

영구 restart daemon, 설정 편집기, meatball 메뉴, GPU process table, native touch 지원, `uvx` 릴리스 패키지, 실제 하드웨어 검증은 아직 없습니다. 선택적 tty1 통합은 transient systemd unit을 사용하며, boot, PWM 파형, RPM, fan fail-safe, 물리 디스플레이 동작은 대상 Pi에서 검증해야 합니다.
