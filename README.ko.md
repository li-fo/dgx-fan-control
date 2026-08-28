# DGX Fan Controller TUI (MVP)

[English README](README.md)

`dgx-fan`은 Raspberry Pi 4에서 최대 두 DCGM exporter의 GPU 정보를 읽고, 두 개의 4선 PWM 팬을 각각 독립적으로 제어하는 직접 실행형 Python Textual 앱입니다. GPU 메모리, 사용률, 온도와 팬별 매핑 DGX, duty, RPM/상태를 보여 주며 실행 중 On/Off 제어는 전역으로 유지됩니다.

## 실행

Python 3.11+와 개발 의존성을 설치합니다.

```bash
uv venv
uv pip install -e '.[dev]'
cp config.example.toml config.toml
```

가상환경을 활성화한 뒤 실행합니다.

```bash
source .venv/bin/activate
python -m dgx_fan --config config.toml
# 또는: dgx-fan --config config.toml
```

활성화하지 않고 uv의 프로젝트 환경으로 실행할 수도 있습니다.

```bash
uv run dgx-fan --config config.toml
```

시스템 `/usr/bin/python`을 사용하면 `src/` 레이아웃 패키지를 보지 못할 수 있습니다. 설정 탐색 순서는 `--config`, `DGX_FAN_CONFIG`, `./config.toml`입니다. 설정을 수정하면 앱을 재시작하세요. MVP에는 설정 UI와 hot reload가 없습니다.

개발 PC에서는 `hardware.backend = "fake"`로 시작하세요. Pi에서는 `.[raspberry-pi]`를 설치하고 Linux PWM overlay를 활성화한 뒤, 배선을 확인한 다음에만 `raspberry-pi` backend로 전환하세요.

## Raspberry Pi 클론 로컬 설치와 콘솔 자동 시작

Raspberry Pi OS 콘솔 설치에서는 저장소를 clone한 폴더에 편집 가능한 설정을 둡니다.
`install.sh`는 애플리케이션 `config.toml`을 `/etc`로 복사하거나 이동하지 않지만,
boot 통합에 필요한 좁은 범위의 파일은 `/etc`에 설치합니다.

```bash
git clone <repository-url> dgx-fan
cd dgx-fan
cp config.example.toml config.toml
# DGX URL을 수정하고 계속하기 전에 [hardware] backend = "raspberry-pi"로 설정합니다.
./install.sh --reboot
```

`install.sh`는 `uv sync --locked --extra raspberry-pi --no-dev`를 실행하고, 클론의
`config.toml`을 검증한 다음, 2채널 PWM overlay와 Raspberry Pi 4의 Raspberry Pi OS tty1 콘솔 자동 로그인을
구성합니다. `config.toml`이 없으면 `config.example.toml`에서 만들고 즉시 중지하므로, 파일을
편집한 뒤 설치 스크립트를 다시 실행하세요. `uv`가 필요합니다. 설치되어 있지 않다면
[공식 uv 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)를 먼저 따르세요.
`--reboot` 없이 `./install.sh`를 실행하면 설치 후 수동 재부팅할 수 있고,
`./install.sh --dry-run`은 Pi를 바꾸지 않고 예정된 동작만 보여 줍니다.

영구 systemd unit이나 cron `@reboot`는 설치하지 않습니다. root 소유 tty1 profile hook은 지정된
로컬 로그인 사용자에서 한 번만 SSH를 제외하고 같은 transient 물리 디스플레이 경로를 시작합니다.
앱이 실행되는 동안에만 `dgx-fan-display.service`가 만들어지고 tty8에서 동작하며, 충돌 후 자동 재시작은 하지 않습니다.
`start.sh`는 PWM0, PWM1, `/dev/gpiochip0`, 공유 runtime lock 접근만 준비하는 root 소유·고정 helper에만 passwordless sudo를
사용하고, 이후 `.venv/bin/dgx-fan --config <clone>/config.toml`을 일반 사용자 권한으로 실행합니다.
Python 프로젝트 전체를 root로 실행하지 않습니다.

설치 후 수동 실행은 다음과 같습니다.

```bash
./start.sh
```

### SSH에서 HDMI 물리 디스플레이 실행

설치 뒤 SSH 터미널에서 clone 폴더의 launcher를 실행합니다.

```bash
./display.sh restart
./display.sh status
# 시작 실패 진단:
sudo journalctl -u dgx-fan-display.service --no-pager
```

transient 프로세스는 `openvt -c 8 -s -w`로 tty8을 열므로 SSH 연결을 종료해도 1024x600 HDMI 콘솔의
모니터링은 유지됩니다. Pi에 연결한 키보드에서는 **Ctrl+Q**로 정상 종료하며 이전 VT로 돌아갑니다.
`./display.sh stop` 또는 system manager의 stop은 비정상 종료이므로, 선택적인 clean `shutdown_mode = "off"`
경로 대신 기존 fan fail-safe release(기본 full duty)가 적용됩니다. display가 동작하는 동안 두 번째
`./start.sh`를 실행하지 마세요. 공유 non-blocking lock이 두 번째 소유자를 거부하며, 첫 프로세스를 종료하지 않습니다.
`uv run dgx-fan --config config.toml`과 `uvx`는 기존처럼 현재 터미널의 foreground 명령입니다.

클론과 설정을 유지하면서 이 프로젝트가 만든 profile hook, sudoers 정책, 고정 display helper와 hardware helper만
제거하려면 다음을 사용합니다.

```bash
./uninstall.sh --yes
```

제거 스크립트는 존재하면 이 프로젝트의 transient unit을 중지한 뒤 PWM overlay와 콘솔 자동 로그인을 의도적으로 유지합니다. 필요하면
`sudo raspi-config`로 콘솔 자동 로그인을 끄고, boot 설정에서 정확한
`dtoverlay=pwm-2chan,pin=18,pin2=19,func=2,func2=2` 줄만 지우거나 boot 설정 옆에 만든
backup을 복원하세요. 로컬 자동 로그인은 물리 콘솔에서 해당 계정에 접근할 수 있게 하며,
이 방식에는 앱 충돌 후 재시작 감독 기능이 없습니다. tty1 콘솔은 터미널 에뮬레이터보다
마우스/터치 보고에 적합하지 않으므로 부팅 콘솔에서는 키보드 조작을 지원 대상으로 봅니다.
무인 운용 전에 실제 Pi에서 boot, PWM 파형, RPM, 팬 fail-safe 동작을 측정하세요.

## 설정과 안전 동작

`config.example.toml`은 schema v2를 설명합니다. `hardware.pwm_gpio_bcm`과 `control.fan_endpoint_ids`는 각각 Fan 1/Fan 2 순서의 정확히 두 항목이어야 합니다. endpoint ID는 설정된 `[[dgx]]` ID를 가리켜야 하며, DGX 한 대면 같은 ID를 두 번 씁니다. 정상 곡선, hysteresis, startup boost, tach 기대값, duty, TUI 상태는 팬별로 독립적이고 곡선/최대 속도 제한만 공통입니다.

`control.fallback_speed_percent`는 선택 항목이며 `0`부터 `100`까지의 정수만 허용하고, 기본값은 `100`입니다. `max_speed_percent`와는 독립적입니다. 첫 telemetry 읽기 전과 모든 `SAFETY OVERRIDE`(endpoint 실패, 매핑된 온도 없음, **어느** 설정 GPU든 비상 온도, fan stall, safety recovery temperature/dwell)에서 두 팬에 함께 적용할 duty를 정합니다. UI Off로도 이 안전 상태는 해제되지 않습니다. 이 값을 낮추면 비상·stall fallback 속도도 함께 낮아지므로 무인 운용 전 냉각과 tach 동작을 확인하세요. 하드웨어 setup/write/release 오류 시의 low-level full-speed recovery에는 영향을 주지 않습니다. 설정 파일 수정 뒤에는 앱을 재시작해야 하며 hot reload는 지원하지 않습니다. 기존 v1의 `pwm_gpio_bcm = 18`은 시작 시 거부됩니다. `version = 2`, `[18, 19]`, `fan_endpoint_ids`로 마이그레이션하세요.

### DCGM 네트워크 재시도

전역 선택 설정인 `[collection]`으로 잠깐의 네트워크 단절에서 최신 DCGM 샘플을 재시도하는 동안 유지할 수 있습니다.

```toml
[collection]
interval_seconds = 2.0
timeout_seconds = 1.5
stale_after_seconds = 6.0
retry_count = 3          # 최초 요청 뒤의 추가 시도 횟수
retry_delay_seconds = 10.0
```

기존 v2 파일에서 두 키를 생략하면 호환성을 유지하며 `retry_count = 0`, `retry_delay_seconds = 10.0`이 적용됩니다. 한 주기는 최초 요청 1회와 `retry_count`만큼의 추가 시도로 구성되고, 시도 사이에는 고정 대기 시간이 있으며 각 요청에는 `timeout_seconds`가 각각 적용됩니다. DNS/연결/timeout 등을 포함한 전송 오류와 HTTP 408, 429, 5xx만 재시도합니다. 그 밖의 4xx, 잘못된 metrics, 사용 가능한 GPU 데이터가 없는 응답은 즉시 실패합니다.

각 DGX는 독립적으로 수집하므로 한 endpoint의 재시도가 정상 endpoint 수집을 지연시키지 않습니다. 재시도 중에는 캐시 샘플도 `stale_after_seconds` 안에서만 사용할 수 있습니다. 샘플이 stale해지거나 모든 시도가 끝나면 그 endpoint는 즉시 비정상이 되고, 두 팬은 `control.fallback_speed_percent`(기본 100%)로 동작합니다. 다음 수집 주기에서 네트워크가 복구되면 정상 복구할 수 있습니다. Dashboard 배너는 `WAITING`, `RETRYING n/N`, `RETRYING · STALE`, `FAILED after N attempts`, 정상 상태를 구분하며, 재시도만으로 그래프 샘플이 중복 추가되지는 않습니다.

`hardware.shutdown_mode`은 선택 항목이며 기본값은 `"full"`입니다. 시작/초기화 실패나 앱 오류를 포함한 모든 release에서 두 팬에 full duty를 명령합니다. 직결 Noctua NF-A6x25 5V PWM처럼 제조사 사양상 0% PWM에서 0 RPM임을 확인한 팬에서만 `shutdown_mode = "off"`를 설정하세요. Textual이 exit code 0으로 정상 반환한 경우에만 두 팬을 0% duty로 멈추고 PWM channel은 enabled 상태로 유지합니다. Textual 내부/비정상 종료, 예외, 0% 쓰기 실패, tach join timeout, GPIO release 실패는 두 channel을 full duty로 되돌립니다. 이는 부팅 시, SIGKILL·터미널 강제 종료, 전원 손실 시 OFF를 보장하지 않습니다. 그런 기본 OFF가 필요하면 별도 하드웨어 전원 스위치가 필요합니다.

DGX 두 대는 물리 공기 흐름 매핑을 명시적으로 고정합니다.

```toml
[control]
fan_endpoint_ids = ["dgx-1", "dgx-2"] # Fan 1 -> DGX-1, Fan 2 -> DGX-2

[hardware]
pwm_gpio_bcm = [18, 19] # Fan 1 -> BCM18, Fan 2 -> BCM19
```

## Raspberry Pi 4 배선

이 직결 안내는 **Noctua NF-A6x25 5V PWM 정확한 모델에만** 적용됩니다. 4선이라는 이유만으로 다른 팬에 적용하지 말고 해당 모델 데이터시트의 커넥터, 논리, tach, 전압, 전류를 별도로 확인하세요. 배선 전에는 전원을 제거하세요.

| 기능 | NF-A6x25 선 / 핀* | Fan 1 | Fan 2 | Raspberry Pi 4 |
| --- | --- | --- | --- | --- |
| 접지 | black / pin 1 | black | black | Pi GND(예: 물리 6/9), 모든 전원 공통 GND |
| 팬 전원 | yellow / pin 2 | yellow +5 V | yellow +5 V | 전원 여유가 확인된 Pi 물리 2/4 또는 정격 외부 5 V |
| Tach / RPM | green / pin 3 | green → BCM23 / 물리 16 | green → BCM24 / 물리 18 | green tach 선은 분리 |
| PWM 제어 | blue / pin 4 | blue → BCM18 / 물리 12(PWM0) | blue → BCM19 / 물리 35(PWM1) | 직접·독립 GPIO 연결 |

\* 색/핀 매핑은 이 정확한 Noctua 모델에만 해당합니다.

각 blue PWM 선을 GPIO에 직접 연결합니다. 이 모델에는 NPN/MOSFET, 레벨 시프터, 외부 PWM pull-up을 추가하지 말고 blue 선도 묶지 마세요. Noctua 입력은 3.3 V CMOS 논리를 허용합니다. 25 kHz non-inverted PWM 및 `pwm_inverted = false`를 사용하세요.

### Tach, 전원, 공통 접지

tach 선은 서로 합치지 마세요. NF-A6x25 green tach 출력은 open collector입니다:

```text
Fan 1 green tach ---------------- BCM23 / 물리 16
Fan 2 green tach ---------------- BCM24 / 물리 18
```

앱은 open-collector tach 입력에 Pi 내부 3.3 V pull-up(`PUD_UP`)을 설정합니다. 즉 floating input이 아닌 기본 직결입니다. `pulses_per_revolution = [2, 2]`를 사용하세요. 긴/노이즈 배선에는 Noctua가 제시한 선택적 3.3 V 1 kΩ pull-up과 선택적 1 µF non-polar capacitor를 추가할 수 있습니다. tach를 5 V로 pull-up하면 안 됩니다.

팬 한 개는 typical 0.187 A, maximum 0.26 A이고 두 개는 typical 0.374 A, maximum 0.52 A입니다. 물리 2/4는 하나의 Pi 5 V rail이므로 Pi/USB 부하와 합산해 전원·케이블·커넥터 여유를 확인하세요. 외부 5 V 팬 전원은 GND를 Pi와 공통으로 하되, Pi가 다른 전원으로 켜진 상태에서 +5 V를 Pi 물리 2/4에 되먹이지 마세요.

### Linux PWM/libgpiod 소유권과 첫 가동

이 backend는 pigpiod를 사용하지 않습니다. 커널 Linux PWM sysfs와 libgpiod falling-edge tach 입력(내부 pull-up)을 사용합니다. 클론 로컬 설치 스크립트는 Raspberry Pi 4만 지원하며, 현재 Raspberry Pi OS의 활성 boot 설정(대개 `/boot/firmware/config.txt`)에 overlay를 추가하고, 접근 helper를 설치한 뒤 재부팅합니다. PWM channel은 공유 자원이므로 analogue audio나 다른 PWM 소비자를 동시에 실행하지 마세요.

```bash
pinout
./install.sh --reboot
# Pi가 재부팅된 후:
ls -l /sys/class/pwm/pwmchip0 /dev/gpiochip0
./start.sh
```

설치 스크립트가 이 주석 없는 줄을 기록합니다. 설치 스크립트를 의도적으로 사용하지 않을 때만 수동으로 추가하세요.

```ini
dtoverlay=pwm-2chan,pin=18,pin2=19,func=2,func2=2
```

```toml
[hardware]
backend = "raspberry-pi"
pwm_gpio_bcm = [18, 19]
pwm_frequency_hz = 25000
pwm_inverted = false
tach_gpio_bcm = [23, 24]
shutdown_mode = "off" # 선택: 정상 종료에서만, 기본값은 fail-safe "full"
```

설치 스크립트는 로그인 사용자를 `gpio` 그룹에 추가하고 PWM sysfs와 `/dev/gpiochip0` 접근만 허용하는 좁은 root 소유 helper를 설치합니다. 설치 후에는 `sudo .venv/bin/dgx-fan` 대신 `./start.sh`를 사용하세요. 팬은 한 개씩 가동하세요. 전원 없는 배선과 5 V/공통 GND를 확인한 뒤 Fan 1을 연결하고 약 25 kHz non-inverted duty, RPM, 곡선 반응을 실측한 다음 Fan 2를 추가하세요. endpoint 손실, tach 분리/stall, 정상/오류 종료를 모두 시험하세요. 기본 release는 full duty를 명령하고 PWM enabled 상태를 유지합니다. 명시적 `shutdown_mode = "off"`에서는 TUI 정상 종료가 0% duty를 명령하고 enabled 상태를 유지하지만, PWM/tach/GPIO 정리 중 오류는 full duty로 되돌립니다. 이 저장소는 pinmux·파형·강제 종료·부팅·물리 속도를 증명하지 않으므로 무인 운용 전 실기 측정이 필요합니다. 롤백은 `shutdown_mode = "off"`를 제거하거나 `"full"`로 바꿔 재시작하거나, 해당 커밋을 되돌리는 방법이며 배선 변경은 필요하지 않습니다.


GPIO, 케이블, 커넥터, 전원이 뜨거워지거나, GPIO가 3.3 V보다 높은 전압을 보거나, 극성이 불확실하거나, 팬이 시작하지 않거나, 측정 전류가 전원/케이블 정격을 넘으면 즉시 전원을 제거하고 중지하세요. 이 프로젝트는 알 수 없는 팬, 조립된 회로, 실시간 DGX 데이터를 검증할 수 없습니다.

### 전기 참고 자료

- [Noctua microcontroller PWM/RPM 가이드](https://www.noctua.at/en/support/faqs/microcontroller-guide-pwm-setup-and-rpm-monitoring)
- [Noctua NF-A6x25 5V PWM 사양](https://www.noctua.at/en/products/nf-a6x25-5v-pwm/specifications)
- [Noctua PWM specifications white paper](https://cdn.noctua.at/media/Noctua_PWM_specifications_white_paper.pdf)
- [Raspberry Pi 4 Model B 데이터시트 및 핀아웃](https://datasheets.raspberrypi.com/rpi4/raspberry-pi-4-datasheet.pdf)
- [공식 Raspberry Pi firmware overlay README](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README)
- [Linux PWM sysfs 문서](https://docs.kernel.org/driver-api/pwm.html)
- [libgpiod Python bindings](https://libgpiod.readthedocs.io/)
위 1차 자료는 명시된 Noctua 모델만 뒷받침하며, 조립 회로나 현재 release 동작을 실기 검증하지는 않습니다.

## MVP 한계

systemd daemon, Unix socket, 영구 `/etc` 설정, 설정 편집기, meatball 메뉴, GPU process table, native touch 지원, `uvx` 릴리스 패키지, 충돌 후 재시작 감독, 실제 하드웨어 검증은 아직 없습니다. 선택 가능한 tty1 콘솔 통합은 단순한 방식이며 대상 Pi에서 별도 검증이 필요합니다.
