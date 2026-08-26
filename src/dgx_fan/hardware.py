from __future__ import annotations

import threading
import time
from errno import EBUSY
from pathlib import Path
from typing import Any, ClassVar, Protocol

from .config import HardwareConfig
from .models import FanReading


class FanHardware(Protocol):
    def set_duties(self, percents: tuple[int, int]) -> None: ...
    def readings(self, now: float) -> tuple[FanReading, FanReading]: ...
    def release(self, *, normal_shutdown: bool = False) -> None: ...


class FakeHardware:
    def __init__(self, shutdown_mode: str = "full") -> None:
        self.duties = (100, 100)
        self.released = False
        self.shutdown_mode = shutdown_mode
        self._readings: tuple[FanReading, FanReading] | None = None

    def set_duties(self, percents: tuple[int, int]) -> None:
        self.released = False
        self.duties = tuple(max(0, min(100, percent)) for percent in percents)  # type: ignore[assignment]

    def readings(self, now: float) -> tuple[FanReading, FanReading]:
        if self._readings is not None:
            return self._readings
        return tuple(
            FanReading(float(1200 * duty / 100), "RUNNING") if duty > 0 else FanReading(0, "STOPPED")
            for duty in self.duties
        )  # type: ignore[return-value]

    def set_readings(self, readings: tuple[FanReading, FanReading] | None) -> None:
        self._readings = readings

    def release(self, *, normal_shutdown: bool = False) -> None:
        self.released = True
        self.duties = (0, 0) if normal_shutdown and self.shutdown_mode == "off" else (100, 100)


class RaspberryPiHardware:
    """Pi 4 Linux PWM sysfs and libgpiod v2 adapter."""

    PWM_CHANNELS: ClassVar[dict[int, int]] = {12: 0, 18: 0, 13: 1, 19: 1}
    _TACH_READY_SECONDS: ClassVar[float] = 2.0
    _TACH_JOIN_SECONDS: ClassVar[float] = 2.0
    _PWM_READY_SECONDS: ClassVar[float] = 1.0

    def __init__(self, config: HardwareConfig) -> None:
        self.config = config
        self._period_ns = 1_000_000_000 // config.pwm_frequency_hz
        self._pulse_lock = threading.Lock()
        self._pulses = [0, 0]
        now = time.monotonic()
        self._last = [(0, now), (0, now)]
        self._tach_stop = threading.Event()
        self._tach_ready = threading.Event()
        self._tach_error: Exception | None = None
        self._tach_request: Any | None = None
        self._tach_thread: threading.Thread | None = None
        self._release_complete = False
        self._pwm_paths: list[Path] = []
        try:
            self._prepare_pwm_channels()
            self.set_duties((100, 100))
            self._start_tach_worker()
        except Exception:
            try:
                self.release()
            except RuntimeError as cleanup_error:
                _ = cleanup_error
            raise

    @classmethod
    def _channel_for_gpio(cls, gpio: int) -> int:
        try:
            return cls.PWM_CHANNELS[gpio]
        except KeyError as error:
            raise RuntimeError(f"BCM{gpio} has no supported Pi 4 hardware PWM channel") from error

    def _prepare_pwm_channels(self) -> None:
        chip = Path(self.config.pwm_chip_path)
        if not chip.is_dir():
            raise RuntimeError(
                f"Linux PWM chip not found at {chip}; enable dtoverlay=pwm-2chan and reboot"
            )
        for gpio in self.config.pwm_gpio_bcm:
            channel = self._channel_for_gpio(gpio)
            path = chip / f"pwm{channel}"
            if not path.exists():
                self._export_pwm_channel(chip, channel)
            if not self._wait_for_pwm_channel(path):
                raise RuntimeError(
                    f"PWM channel {channel} did not appear at {path}; check pwm-2chan overlay and permissions"
                )
            self._pwm_paths.append(path)
            self._write(path / "enable", "0", "disable PWM while configuring")
            self._write(path / "duty_cycle", "0", "clear PWM duty before changing period")
            self._write(path / "period", str(self._period_ns), "set PWM period")
            self._write(path / "duty_cycle", str(self._safe_duty_ns()), "set initial full-speed duty")
            self._write(path / "enable", "1", "enable PWM")

    def _export_pwm_channel(self, chip: Path, channel: int) -> None:
        try:
            (chip / "export").write_text(str(channel))
        except OSError as error:
            if error.errno != EBUSY:
                raise RuntimeError(f"cannot export PWM channel {channel} at {chip}: {error}") from error

    def _wait_for_pwm_channel(self, path: Path) -> bool:
        deadline = time.monotonic() + self._PWM_READY_SECONDS
        required = ("enable", "period", "duty_cycle")
        while True:
            if path.is_dir() and all((path / name).exists() for name in required):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    @staticmethod
    def _write(path: Path, value: str, action: str) -> None:
        try:
            path.write_text(value)
        except PermissionError as error:
            raise RuntimeError(f"permission denied while attempting to {action} at {path}; run with device access") from error
        except OSError as error:
            raise RuntimeError(f"cannot {action} at {path}: {error}") from error

    def _effective_percent(self, percent: int) -> int:
        bounded = max(0, min(100, percent))
        return 100 - bounded if self.config.pwm_inverted else bounded

    def _duty_ns(self, percent: int) -> int:
        return self._period_ns * self._effective_percent(percent) // 100

    def _safe_duty_ns(self) -> int:
        return self._duty_ns(100)

    def _write_duty(self, path: Path, percent: int) -> None:
        self._write(path / "duty_cycle", str(self._duty_ns(percent)), "set PWM duty")
        self._write(path / "enable", "1", "keep PWM enabled")

    def _write_all_duties(self, percent: int) -> list[Exception]:
        failures: list[Exception] = []
        for path in self._pwm_paths:
            try:
                self._write_duty(path, percent)
            except Exception as error:  # noqa: BLE001 - attempt both physical channels.
                failures.append(error)
        return failures

    def _safe_full_speed(self) -> list[Exception]:
        return self._write_all_duties(100)

    def set_duties(self, percents: tuple[int, int]) -> None:
        failures: list[Exception] = []
        for path, percent in zip(self._pwm_paths, percents, strict=True):
            try:
                self._write_duty(path, percent)
            except Exception as error:  # noqa: BLE001 - attempt the other fan before failing closed.
                failures.append(error)
        if failures:
            safe_failures = self._safe_full_speed()
            suffix = "; full-speed retry also failed" if safe_failures else ""
            raise RuntimeError(f"failed to update one or more fan PWM channels{suffix}") from failures[0]

    def _start_tach_worker(self) -> None:
        self._tach_thread = threading.Thread(target=self._tach_worker, name="dgx-fan-tach", daemon=True)
        self._tach_thread.start()
        if not self._tach_ready.wait(self._TACH_READY_SECONDS):
            raise RuntimeError("timed out while opening tach GPIO lines on libgpiod")
        if self._tach_error is not None:
            raise RuntimeError(f"cannot open tach GPIO lines: {self._tach_error}") from self._tach_error

    def _tach_worker(self) -> None:
        request: Any | None = None
        try:
            try:
                import gpiod
            except ImportError as error:
                raise RuntimeError("raspberry-pi backend requires `dgx-fan[raspberry-pi]` (gpiod)") from error
            settings = gpiod.LineSettings(
                direction=gpiod.line.Direction.INPUT,
                edge_detection=gpiod.line.Edge.FALLING,
                bias=gpiod.line.Bias.PULL_UP,
            )
            request = gpiod.request_lines(
                self.config.gpio_chip_path,
                consumer="dgx-fan",
                config={gpio: settings for gpio in self.config.tach_gpio_bcm},
            )
            self._tach_request = request
        except Exception as error:  # noqa: BLE001 - relay setup details to the constructor.
            self._tach_error = error
            self._tach_ready.set()
            return
        self._tach_ready.set()
        try:
            while not self._tach_stop.is_set():
                if not request.wait_edge_events(timeout=0.1):
                    continue
                for event in request.read_edge_events():
                    try:
                        index = self.config.tach_gpio_bcm.index(event.line_offset)
                    except ValueError:
                        continue
                    with self._pulse_lock:
                        self._pulses[index] += 1
        except Exception as error:  # noqa: BLE001 - retain error for safe readings.
            self._tach_error = error

    def readings(self, now: float) -> tuple[FanReading, FanReading]:
        if self._tach_error is not None:
            return (FanReading(None, "NO TACH"), FanReading(None, "NO TACH"))
        result: list[FanReading] = []
        with self._pulse_lock:
            pulses = tuple(self._pulses)
        for index, (previous, at) in enumerate(self._last):
            elapsed = max(now - at, 0.001)
            count = pulses[index] - previous
            self._last[index] = (pulses[index], now)
            rpm = count / self.config.pulses_per_revolution[index] / elapsed * 60
            result.append(FanReading(rpm if count else None, "RUNNING" if count else "NO TACH"))
        return (result[0], result[1])

    def release(self, *, normal_shutdown: bool = False) -> None:
        if self._release_complete:
            return
        failures: list[Exception] = []
        self._tach_stop.set()
        target_percent = 0 if normal_shutdown and self.config.shutdown_mode == "off" else 100
        target_failures = self._write_all_duties(target_percent)
        failures.extend(target_failures)
        if target_percent == 0 and target_failures:
            # A partial stop would leave one fan uncontrolled.  Restore both channels to
            # the fail-safe duty before continuing tach cleanup and surfacing the error.
            failures.extend(self._safe_full_speed())
        cleanup_failed = False
        thread = self._tach_thread
        if thread is not None:
            thread.join(self._TACH_JOIN_SECONDS)
            if thread.is_alive():
                failures.append(RuntimeError("tach worker did not stop before GPIO release"))
                cleanup_failed = True
        request = self._tach_request
        if request is not None and (thread is None or not thread.is_alive()):
            try:
                request.release()
            except Exception as error:  # noqa: BLE001 - retry after a later release call.
                failures.append(error)
                cleanup_failed = True
            else:
                self._tach_request = None
        if target_percent == 0 and cleanup_failed:
            # The fans are already at the requested stop duty.  A later tach
            # cleanup fault changes this into an abnormal teardown, so return
            # both physical channels to full before surfacing that fault.
            failures.extend(self._safe_full_speed())
        if failures:
            raise RuntimeError("failed cleanup: PWM safe-full or tach release did not complete") from failures[0]
        self._release_complete = True


def create_hardware(config: HardwareConfig) -> FanHardware:
    return FakeHardware(config.shutdown_mode) if config.backend == "fake" else RaspberryPiHardware(config)
