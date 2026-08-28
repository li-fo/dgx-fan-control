# Raspberry Pi 4 팬 배선

[English wiring guide](raspberry-pi-wiring.md) · [README로 돌아가기](../README.ko.md)

이 직결 안내는 **Noctua NF-A6x25 5V PWM 정확한 모델에만** 적용됩니다. 4선이라는 이유만으로 다른 팬에 적용하지 말고 해당 모델 데이터시트의 커넥터, 논리, tach, 전압, 전류 조건을 별도로 확인하세요. 배선 전에는 전원을 제거하세요.

## 연결

| 기능 | NF-A6x25 선 / 핀* | Fan 1 | Fan 2 | Raspberry Pi 4 |
| --- | --- | --- | --- | --- |
| 접지 | black / pin 1 | black | black | Pi GND(예: 물리 6/9), 모든 전원 공통 GND |
| 팬 전원 | yellow / pin 2 | yellow +5 V | yellow +5 V | 전원 여유가 확인된 Pi 물리 2/4 또는 정격 외부 5 V |
| Tach / RPM | green / pin 3 | green → BCM23 / 물리 16 | green → BCM24 / 물리 18 | green tach 선은 분리 |
| PWM 제어 | blue / pin 4 | blue → BCM18 / 물리 12(PWM0) | blue → BCM19 / 물리 35(PWM1) | 직접·독립 GPIO 연결 |

\* 색/핀 매핑은 이 정확한 Noctua 모델에만 해당합니다.

각 blue PWM 선을 GPIO에 직접 연결합니다. 이 모델에는 NPN/MOSFET, 레벨 시프터, 외부 PWM pull-up을 추가하지 말고 blue 선도 묶지 마세요. Noctua 입력은 3.3 V CMOS 논리를 허용합니다. 25 kHz non-inverted PWM 및 `pwm_inverted = false`를 사용하세요.

## Tach, 전원, 공통 접지

tach 선은 서로 합치지 마세요. NF-A6x25 green tach 출력은 open collector입니다.

```text
Fan 1 green tach ---------------- BCM23 / 물리 16
Fan 2 green tach ---------------- BCM24 / 물리 18
```

앱은 open-collector tach 입력에 Pi 내부 3.3 V pull-up(`PUD_UP`)을 설정합니다. 즉 floating input이 아닌 기본 직결입니다. `pulses_per_revolution = [2, 2]`를 사용하세요. 긴/노이즈 배선에는 Noctua가 제시한 선택적 3.3 V 1 kΩ pull-up과 선택적 1 µF non-polar capacitor를 추가할 수 있습니다. tach를 5 V로 pull-up하면 안 됩니다.

팬 한 개는 typical 0.187 A, maximum 0.26 A이고 두 개는 typical 0.374 A, maximum 0.52 A입니다. 물리 2/4는 하나의 Pi 5 V rail이므로 Pi/USB 부하와 합산해 전원·케이블·커넥터 여유를 확인하세요. 외부 5 V 팬 전원은 GND를 Pi와 공통으로 하되, Pi가 다른 전원으로 켜진 상태에서 +5 V를 Pi 물리 2/4에 되먹이지 마세요.

## Linux PWM과 첫 가동

이 backend는 pigpiod를 사용하지 않습니다. 커널 Linux PWM sysfs와 libgpiod falling-edge tach 입력(내부 pull-up)을 사용합니다. clone-local 설치 스크립트는 Raspberry Pi 4만 지원하며, 현재 Raspberry Pi OS의 활성 boot 설정(대개 `/boot/firmware/config.txt`)에 overlay를 추가하고 접근 helper를 설치한 뒤 재부팅합니다.

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

설치 스크립트는 로그인 사용자를 `gpio` 그룹에 추가하고 PWM sysfs와 `/dev/gpiochip0` 접근만 허용하는 좁은 root 소유 helper를 설치합니다. 설치 후에는 `sudo .venv/bin/dgx-fan` 대신 `./start.sh`를 사용하세요. PWM channel은 공유 자원이므로 analogue audio나 다른 PWM 소비자를 동시에 실행하지 마세요.

팬은 한 개씩 가동하세요. 전원 없는 배선과 5 V/공통 GND를 확인한 뒤 Fan 1을 연결하고 약 25 kHz non-inverted duty, RPM, 곡선 반응을 실측한 다음 Fan 2를 추가하세요. endpoint 손실, tach 분리/stall, 정상/오류 종료를 모두 시험하세요. 기본 release는 full duty를 명령하고 PWM enabled 상태를 유지합니다. 명시적 `shutdown_mode = "off"`에서는 TUI 정상 종료가 0% duty를 명령하고 enabled 상태를 유지하지만, PWM/tach/GPIO 정리 중 오류는 full duty로 되돌립니다. 이 저장소는 pinmux·파형·강제 종료·부팅·물리 속도를 증명하지 않으므로 무인 운용 전 실기 측정이 필요합니다. 롤백은 `shutdown_mode = "off"`를 제거하거나 `"full"`로 바꿔 재시작하거나, 해당 커밋을 되돌리는 방법이며 배선 변경은 필요하지 않습니다.

## 즉시 중지할 조건

GPIO, 케이블, 커넥터, 전원이 뜨거워지거나, Pi GPIO가 3.3 V보다 높은 전압을 보거나, 극성이 불확실하거나, 팬이 시작하지 않거나, 측정 전류가 전원/케이블 정격을 넘으면 즉시 전원을 제거하고 중지하세요. 이 프로젝트는 알 수 없는 팬, 조립된 회로, 실시간 DGX 데이터를 검증할 수 없습니다.

## 전기 참고 자료

- [Noctua microcontroller PWM/RPM 가이드](https://www.noctua.at/en/support/faqs/microcontroller-guide-pwm-setup-and-rpm-monitoring)
- [Noctua NF-A6x25 5V PWM 사양](https://www.noctua.at/en/products/nf-a6x25-5v-pwm/specifications)
- [Noctua PWM specifications white paper](https://cdn.noctua.at/media/Noctua_PWM_specifications_white_paper.pdf)
- [Raspberry Pi 4 Model B 데이터시트 및 핀아웃](https://datasheets.raspberrypi.com/rpi4/raspberry-pi-4-datasheet.pdf)
- [공식 Raspberry Pi firmware overlay README](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README)
- [Linux PWM sysfs 문서](https://docs.kernel.org/driver-api/pwm.html)
- [libgpiod Python bindings](https://libgpiod.readthedocs.io/)

위 1차 자료는 명시된 Noctua 모델만 뒷받침하며, 조립 회로나 현재 release 동작을 실기 검증하지는 않습니다.
