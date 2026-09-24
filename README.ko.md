# DGX Fan Controller TUI (MVP)

[English README](README.md)

`dgx-fan-control`은 DGX Spark의 케이스를 만들면서, "DGX Spark 온도에 따라 팬 컨트롤을 하면 어떨까?" 라는 생각에서 시작된 앱니다.

![DGX Fan Control 7inch LCE](./images/dgx-fan-control.webp)

하드웨어 구성을 단순화 하기 위해, Noctua NF-A6x25 5V PWM와 라즈베리 파이4를 사용했습니다.
(라즈베리 파이4의 5v pin out으로 팬에 전력을 직접 공급하고, PWM 속도를 조절할 수 있습니다.)

대시보드 표시를 위해 라즈베리 파이용 7inch 1024x600 해상도의 터치 모니터를 사용했지만,
웹 브라우저로 확인할 수 있기 때문에 팬 컨트롤만을 위해서는 불필요 합니다.

소프트웨어의 경우 DGX Spark에 DCGM exporter와 [node_exporter](https://github.com/prometheus/node_exporter) 를 설치가 필요 합니다.
일반적이라면 DCGM Exporter를 통해서 GPU 정보를 모두 얻을 수 있지만,
unified memory를 사용하는 DGX Spark특성 상 DCGM에서는 메모리 관련 정보를 얻을 수 없어 node_exporter로 간접적으로 정보를 얻고 있습니다.

## 설치 및 실행 (Raspberry Pi)

먼저 [`uv`](https://docs.astral.sh/uv/getting-started/installation/)를 설치한 뒤, 프로젝트를 clone하고 일반 로그인 사용자로 설치 스크립트를 실행합니다.

### config.toml 생성

```bash
cp config.example.toml config.toml
```

`config.toml`에서 DGX exporter URL과 팬 연결 정보를 입력 해야 하며, 
라즈베리4에서 실제 작동을 위해서는 `[hardware] backend = "fake"`를 `[hardware] backend = "raspberry-pi"`로 변경 해야 합니다. 
웹 모니터를 사용하기 위해서는 `[web] enabled = false` 를 `[web]enabled = true`로 변경 해야 합니다. 

### 설치 및 삭제

```bash
./install.sh --no-launch
```

설치만 마친 뒤 직접 실행하려면 `--no-launch`를 사용합니다. 제거할 때는 별도로 다음 명령을 실행합니다. 

```bash
./uninstall.sh
```

### 실행 및 종료

```bash
./dgx-fan-control.sh
Choose the primary TUI display:
  1) Current terminal
  2) HDMI: labwc + fullscreen LXTerminal
  3) HDMI: Linux console (tty8 fallback)
  h) Help
  q) Cancel
Choice [1/2/3]: 2
Start the optional web monitor? [y/N] y
Running as unit: dgx-fan-web.service; invocation ID: f2f86be3d29648e69b17eba39f47969b
Browser monitor: http://192.168.1.7:8000/
```

실행을 하게 되면 옵션을 선택할 수 있습니다. 

| 메뉴 | 선택 기준 |
| --- | --- |
| `1` | 현재 터미널·SSH에서 바로 실행합니다. 터미널을 닫으면 앱도 종료될 수 있습니다. |
| `2` | 권장 그래픽 HDMI 화면입니다. 풍부한 글리프·색상을 사용하며 labwc·LXTerminal이 필요합니다. |
| `3` | 그래픽 환경 없이 Linux 콘솔 tty8로 표시합니다. 글꼴·색 표현에 제약이 있습니다. |
| `h` / `q` | 도움말 보기 / 실행 취소. |

이어서 묻는 웹 모니터는 선택 사항입니다. 

`stop`은 관리 중인 웹·HDMI 화면을 중지하며, 현재 터미널에서 실행한 앱은 그 터미널에서 **Ctrl+Q**로 종료하면 됩니다. 
자세한 시작·중지·문제 해결은 [한국어 사용 안내](docs/usage.ko.md)를 참고하시면 됩니다. 


```bash
./dgx-fan-control.sh stop
```


## 화면

![DGX Fan Control ](./images/dgx-fan-control-scr-001.png)
![DGX Fan Control ](./images/dgx-fan-control-scr-002.png)
![DGX Fan Control ](./images/dgx-fan-control-scr-003.png)
![DGX Fan Control ](./images/dgx-fan-control-scr-004.png)


## 테스트 한계

테스트한 라즈베리 파이 환경은 라즈베리 파이 4 기본 이미지이며, `Debian GNU/Linux 13 (trixie)` 이며,
자동 실행을 위해 부팅 시 GUI가 아닌 콘솔로 로그인 되도록 설정 했습니다. 

제가 가지고 있는 기기에서만 테스트했기 때문에 다른 기기에서는 오류가 발생할 수 있습니다.

