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

개발 PC에서는 `hardware.backend = "fake"`로 시작하세요. Pi에서는 `.[raspberry-pi]`를 설치하고 `pigpiod`를 실행한 뒤, 배선을 확인한 다음에만 `raspberry-pi` backend로 전환하세요.

## 설정과 안전 동작

`config.example.toml`은 schema v2를 설명합니다. `hardware.pwm_gpio_bcm`과 `control.fan_endpoint_ids`는 각각 Fan 1/Fan 2 순서의 정확히 두 항목이어야 합니다. endpoint ID는 설정된 `[[dgx]]` ID를 가리켜야 하며, DGX 한 대면 같은 ID를 두 번 씁니다. 정상 곡선, hysteresis, startup boost, tach 기대값, duty, TUI 상태는 팬별로 독립적이고 곡선/최대 속도 제한만 공통입니다. 모든 설정 endpoint가 최신·정상이 아니거나, 매핑된 온도가 없거나, **어느** 설정 GPU든 비상 온도이거나, 팬 stall이면 **두** PWM 채널 모두 100%가 됩니다. UI Off는 그 안전 상태를 해제하지 않습니다. 기존 v1의 `pwm_gpio_bcm = 18`은 시작 시 거부됩니다. `version = 2`, `[18, 19]`, `fan_endpoint_ids`로 마이그레이션하세요.

DGX 두 대는 물리 공기 흐름 매핑을 명시적으로 고정합니다.

```toml
[control]
fan_endpoint_ids = ["dgx-1", "dgx-2"] # Fan 1 -> DGX-1, Fan 2 -> DGX-2

[hardware]
pwm_gpio_bcm = [18, 19] # Fan 1 -> BCM18, Fan 2 -> BCM19
```

## Raspberry Pi 4 배선

이 앱은 **PWM 신호 두 개와 tach 신호 두 개를 각각 독립적으로** 제어/읽습니다. 정확한 팬 데이터시트가 항상 우선입니다. 아래 커넥터 위치는 일반적인 4선 PWM 팬 관례일 뿐이고, 커넥터 순서, 선 색, 전압, tach 출력 방식은 팬마다 다를 수 있습니다. 배선·변경 전에는 반드시 전원을 제거하세요.

| 기능 | 일반적인 팬 커넥터 위치* | Fan 1 | Fan 2 | Raspberry Pi 4 |
| --- | --- | --- | --- | --- |
| 접지 | Pin 1 | GND | GND | Pi GND 아무 곳(예: 물리 핀 6 또는 9), 모든 전원이 이 접지를 공유 |
| 팬 전원 | Pin 2 | +5 V | +5 V | 전원 예산이 안전할 때만 물리 핀 2 또는 4, 아니면 별도 5 V 팬 전원 |
| Tach / RPM | Pin 3 | Tach 1 | Tach 2 | Fan 1: BCM23 / 물리 핀 16; Fan 2: BCM24 / 물리 핀 18. 각각 Pi 3.3 V로 향하는 독립 외부 4.7 kΩ–10 kΩ pull-up 필요 |
| PWM 제어 | Pin 4 | PWM 1 | PWM 2 | Fan 1: BCM18 / 물리 12(PWM0)의 독립 open-collector/open-drain 드라이버; Fan 2: BCM19 / 물리 35(PWM1)의 별도 드라이버 |

\* 일반 배선도의 선 색/핀 번호보다 정확한 팬 데이터시트를 우선하세요. 팬 PWM선(내부 5 V pull-up이 있을 수 있음) 또는 5 V tach 신호를 Pi GPIO에 직접 연결하면 안 됩니다.

설정은 PWM0(`BCM12` 또는 `BCM18`)에서 하나, PWM1(`BCM13` 또는 `BCM19`)에서 하나를 사용해야 합니다. 지원 기본값은 Fan 1 `BCM18` / 물리 12, Fan 2 `BCM19` / 물리 35입니다. 같은 PWM 채널의 두 핀을 쓰거나 두 팬의 PWM선/트랜지스터 드라이버를 공유하지 마세요.

### PWM 레벨 시프터

Pi GPIO는 3.3 V 로직입니다. 각 팬 PWM 입력에 각자의 open-collector/open-drain 회로를 사용하고 Pi GPIO에 직접 연결하지 마세요. 2N3904 또는 2N2222 NPN 회로 두 개를 사용합니다.

```text
Fan 1: Pi BCM18 / 물리 핀 12 -- 2.2 kΩ–4.7 kΩ -- Base (NPN #1)
                                            o  base node
                                            |
                                     10 kΩ (권장 pull-down)
                                            |
                                           GND

Fan 1 PWM (일반적으로 pin 4) -------- Collector (NPN #1); emitter --- 공통 GND
Fan 2: Pi BCM19 / 물리 핀 35 -- 2.2 kΩ–4.7 kΩ -- Base (NPN #2)
Fan 2 PWM (일반적으로 pin 4) -------- Collector (NPN #2); emitter --- 공통 GND
```

대신 각 팬마다 3.3 V gate drive에서 켜지도록 명시된 N-channel logic-level MOSFET을 쓸 수 있습니다. Gate는 각 BCM18/BCM19에서 약 100 Ω–1 kΩ 직렬 저항을 거쳐 연결하고, gate-to-ground 10 kΩ pull-down을 둡니다. Source는 공통 GND, drain은 해당 팬 PWM 입력에 연결합니다. PWM선에 Pi 측 pull-up을 추가하지 말고 해당 팬의 데이터시트상 입력 회로를 사용하세요.

제공되는 `pwm_inverted = true`는 NPN/N-MOSFET low-side 회로의 반전을 보정합니다. 요청 duty가 팬 입력의 active-low duty에 맞게 적용됩니다. 실제 회로를 변경하고 극성을 계측으로 확인하기 전에는 유지하세요.

### Tach, 전원, 공통 접지

tach 선은 서로 합치지 마세요. 일반적인 open-collector/open-drain tach 배선은 다음과 같습니다.

```text
Pi 3.3 V ---- 4.7 kΩ–10 kΩ ----+---- BCM23 / 물리 핀 16
                                |
Fan 1 tach ---------------------+

Pi 3.3 V ---- 4.7 kΩ–10 kΩ ----+---- BCM24 / 물리 핀 18
                                |
Fan 2 tach ---------------------+
```

팬 데이터시트에서 tach 출력 타입을 확인하세요. 5 V push-pull tach에는 level shifter가 필요합니다. Pi 내부 pull-up으로 대체할 수 없고 BCM23/24에 5 V를 인가하면 안 됩니다. `pulses_per_revolution`은 팬 데이터시트 값으로 설정하세요(흔히 2지만 항상 그렇지 않음).

물리 핀 2와 4는 서로 다른 전원이 아니라 같은 Pi 5 V rail입니다. 두 팬의 정상/기동 전류를 Pi/USB 부하에 더하고, 사용 중인 전원, 케이블, 커넥터가 총 전류 정격을 만족하는지 확인한 뒤에만 헤더 전원을 사용하세요. 확실하지 않으면 정격이 확인된 별도 5 V 팬 전원을 사용합니다. PWM/tach 기준을 위해 그 전원의 GND는 Pi GND와 공통이어야 하지만, Pi가 다른 전원으로 켜진 상태에서 별도 팬 전원의 +5 V를 Pi 물리 핀 2/4에 연결하면 역급전될 수 있으므로 하지 마세요.

### pigpio 소유권, daemon, 첫 가동

코드는 설정한 25 kHz로 BCM18(PWM0)과 BCM19(PWM1)에 pigpio `hardware_PWM`을 호출합니다. 이 pigpio backend에는 `dtoverlay=pwm` 또는 `dtoverlay=pwm-2chan`을 추가하지 마세요. OS 이미지나 다른 프로젝트에서 PWM overlay를 물려받았다면, 운영자가 배타적 소유권과 호환성을 별도로 확인한 경우가 아니면 실제 가동 전에 제거/비활성화하세요. PWM peripheral/channel은 공유 하드웨어 자원이므로 analogue audio나 다른 PWM 소비자를 동시에 실행하지 마세요. 아래 Raspberry Pi overlay 문서는 이 backend의 설정 요구 사항이 아니라 핀/channel/resource 사실을 확인하기 위한 참고 자료입니다.

실제 backend 전환 전 물리 매핑, Pi extra, `pigpiod`, 설정을 확인하세요.

```bash
pinout
uv pip install -e '.[raspberry-pi]'
sudo systemctl enable --now pigpiod
uv run dgx-fan --config config.toml
```

```toml
[hardware]
backend = "raspberry-pi"
pwm_gpio_bcm = [18, 19]
pwm_frequency_hz = 25000
pwm_inverted = true
tach_gpio_bcm = [23, 24]
```

팬은 한 개씩 가동하세요. 전원이 꺼진 상태에서 데이터시트와 대조하고, PWM/tach를 빼고 전압/공통 GND를 확인한 뒤 드라이버와 첫 팬을 연결합니다. 앱은 DCGM 데이터를 받기 전에 full speed를 명령하므로 full-speed fail-safe 출력이 나타나는 것이 정상입니다. 예상과 다르면 전원을 끄세요. 팬 입력에서 약 25 kHz, 올바른 active-low 극성, 단계별 duty 변화, 최소 안정 duty를 확인합니다. 이후 tach/RPM을 각각 검증하고 두 번째 팬을 추가합니다. endpoint 손실, tach 분리/stall, 앱 종료도 시험하세요. 안전 상태는 full speed를 강제해야 하며, PWM release 후 외부 드라이버/팬 입력은 데이터시트의 pull-up/default full-speed 상태로 돌아가야 합니다.

GPIO, 케이블, 트랜지스터, 커넥터, 전원이 뜨거워지거나, GPIO가 3.3 V보다 높은 전압을 보거나, 극성이 불확실하거나, 팬이 시작하지 않거나, 측정 전류가 전원/케이블 정격을 넘으면 즉시 전원을 제거하고 중지하세요. 이 프로젝트는 알 수 없는 팬, 조립된 회로, 실시간 DGX 데이터를 검증할 수 없습니다.

### 전기 참고 자료

- [Raspberry Pi GPIO 및 40핀 헤더 문서](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#gpio-and-the-40-pin-header)
- [Raspberry Pi 4 Model B 데이터시트 및 핀아웃](https://datasheets.raspberrypi.com/rpi4/raspberry-pi-4-datasheet.pdf)
- [공식 Raspberry Pi firmware overlay README](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README)
- [pigpio `gpioHardwarePWM` 구현(GPIO mode와 PWM clock 설정)](https://github.com/joan2937/pigpio/blob/master/pigpio.c#L12319-L12410)
- [Intel 4선 팬 전기 가이드(참조 토폴로지)](https://www.intel.com.tr/content/dam/www/public/us/en/documents/design-guides/celeron-400-guide.pdf)

Intel 가이드는 일반적인 4선 PWM 전기 토폴로지를 설명하는 참고 자료이며, 알 수 없는 5 V 팬의 커넥터 순서, 전압, 전류, 동작을 인증하지는 않습니다.

앱은 시작 시 네트워크를 읽기 전에 안전한 full-speed 출력을 설정합니다. 정상 종료나 오류 때 PWM을 release합니다. 외부 open-collector 회로는 팬 제어 입력이 pull-up/default full-speed 동작으로 돌아가도록 해야 합니다.

## MVP 한계

systemd daemon, Unix socket, 영구 `/etc` 설정, 설정 편집기, meatball 메뉴, GPU process table, native touch 지원, `uvx` 릴리스 패키지, 실제 하드웨어 검증은 아직 없습니다. 대상 Pi와 DGX에서 이 직접 실행형 Python MVP를 검증한 뒤에 추가할 항목입니다.
