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

## 설정과 안전 동작

`config.example.toml`은 schema v2를 설명합니다. `hardware.pwm_gpio_bcm`과 `control.fan_endpoint_ids`는 각각 Fan 1/Fan 2 순서의 정확히 두 항목이어야 합니다. endpoint ID는 설정된 `[[dgx]]` ID를 가리켜야 하며, DGX 한 대면 같은 ID를 두 번 씁니다. 정상 곡선, hysteresis, startup boost, tach 기대값, duty, TUI 상태는 팬별로 독립적이고 곡선/최대 속도 제한만 공통입니다. 모든 설정 endpoint가 최신·정상이 아니거나, 매핑된 온도가 없거나, **어느** 설정 GPU든 비상 온도이거나, 팬 stall이면 **두** PWM 채널 모두 100%가 됩니다. UI Off는 그 안전 상태를 해제하지 않습니다. 기존 v1의 `pwm_gpio_bcm = 18`은 시작 시 거부됩니다. `version = 2`, `[18, 19]`, `fan_endpoint_ids`로 마이그레이션하세요.

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

이 backend는 pigpiod를 사용하지 않습니다. 커널 Linux PWM sysfs와 libgpiod falling-edge tach 입력(내부 pull-up)을 사용합니다. 현재 Raspberry Pi OS에서는 `/boot/firmware/config.txt`에 다음 overlay를 추가하고 재부팅하세요. PWM channel은 공유 자원이므로 analogue audio나 다른 PWM 소비자를 동시에 실행하지 마세요.

```bash
pinout
uv pip install -e '.[raspberry-pi]'
sudoedit /boot/firmware/config.txt
sudo reboot
ls -l /sys/class/pwm/pwmchip0 /dev/gpiochip0
sudo .venv/bin/dgx-fan --config /absolute/path/config.toml
```

재부팅 전에 `/boot/firmware/config.txt`에 다음 주석 없는 줄을 추가하세요.

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

프로세스는 PWM sysfs 쓰기와 `/dev/gpiochip0` 접근 권한이 필요합니다. 적절한 udev/device-access 정책을 구성하거나 첫 가동은 `sudo`로 하세요. 팬은 한 개씩 가동하세요. 전원 없는 배선과 5 V/공통 GND를 확인한 뒤 Fan 1을 연결하고 약 25 kHz non-inverted duty, RPM, 곡선 반응을 실측한 다음 Fan 2를 추가하세요. endpoint 손실, tach 분리/stall, 정상/오류 종료를 모두 시험하세요. 기본 release는 full duty를 명령하고 PWM enabled 상태를 유지합니다. 명시적 `shutdown_mode = "off"`에서는 TUI 정상 종료가 0% duty를 명령하고 enabled 상태를 유지하지만, PWM/tach/GPIO 정리 중 오류는 full duty로 되돌립니다. 이 저장소는 pinmux·파형·강제 종료·부팅·물리 속도를 증명하지 않으므로 무인 운용 전 실기 측정이 필요합니다. 롤백은 `shutdown_mode = "off"`를 제거하거나 `"full"`로 바꿔 재시작하거나, 해당 커밋을 되돌리는 방법이며 배선 변경은 필요하지 않습니다.


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

systemd daemon, Unix socket, 영구 `/etc` 설정, 설정 편집기, meatball 메뉴, GPU process table, native touch 지원, `uvx` 릴리스 패키지, 실제 하드웨어 검증은 아직 없습니다. 대상 Pi와 DGX에서 이 직접 실행형 Python MVP를 검증한 뒤에 추가할 항목입니다.
