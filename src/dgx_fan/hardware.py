from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from .config import HardwareConfig
from .models import FanReading


class FanHardware(Protocol):
    def set_duties(self, percents: tuple[int, int]) -> None: ...
    def readings(self, now: float) -> tuple[FanReading, FanReading]: ...
    def release(self) -> None: ...


class FakeHardware:
    def __init__(self) -> None:
        self.duties = (100, 100)
        self.released = False
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
        """Set deterministic tach data for tests; None restores plausible simulated fans."""
        self._readings = readings

    def release(self) -> None:
        self.released = True
        self.duties = (100, 100)


class RaspberryPiHardware:
    """Lazy pigpio adapter; importing this module never touches GPIO."""
    def __init__(self, config: HardwareConfig) -> None:
        try:
            import pigpio
        except ImportError as error:
            raise RuntimeError("raspberry-pi backend requires `dgx-fan[raspberry-pi]`") from error
        self._pigpio = pigpio
        self.config = config
        self.pi = pigpio.pi()
        if not self.pi.connected:
            self.pi.stop()
            raise RuntimeError("cannot connect to pigpiod")
        self._pulses = [0, 0]
        self._last = [(0, time.monotonic()), (0, time.monotonic())]
        self._callbacks = []
        try:
            for index, gpio in enumerate(config.tach_gpio_bcm):
                self.pi.set_mode(gpio, pigpio.INPUT)
                self.pi.set_pull_up_down(gpio, pigpio.PUD_UP)
                self._callbacks.append(self.pi.callback(gpio, pigpio.FALLING_EDGE, self._tach_callback(index)))
            self.set_duties((100, 100))
        except Exception:  # preserve the original hardware setup error.
            try:
                self.release()
            except Exception as cleanup_error:  # noqa: BLE001 - best-effort cleanup must not mask setup failure.
                _ = cleanup_error
            raise

    def _tach_callback(self, index: int) -> Callable[[int, int, int], None]:
        def callback(gpio: int, level: int, tick: int) -> None:
            if level == 0:
                self._pulses[index] += 1
        return callback

    def set_duties(self, percents: tuple[int, int]) -> None:
        failures: list[Exception] = []
        for gpio, percent in zip(self.config.pwm_gpio_bcm, percents, strict=True):
            effective = 100 - max(0, min(100, percent)) if self.config.pwm_inverted else max(0, min(100, percent))
            try:
                self.pi.hardware_PWM(gpio, self.config.pwm_frequency_hz, effective * 10_000)
            except Exception as error:  # noqa: BLE001 - always attempt the other physical channel.
                failures.append(error)
        if failures:
            # A partial normal update is unsafe: best-effort both channels to full speed
            # before allowing the application supervisor to clean up/release GPIO.
            safe_failures: list[Exception] = []
            for gpio in self.config.pwm_gpio_bcm:
                safe_effective = 0 if self.config.pwm_inverted else 100
                try:
                    self.pi.hardware_PWM(gpio, self.config.pwm_frequency_hz, safe_effective * 10_000)
                except Exception as error:  # noqa: BLE001 - preserve both-channel attempt.
                    safe_failures.append(error)
            detail = " (safe full-speed retry also failed)" if safe_failures else ""
            raise RuntimeError(f"failed to update one or more fan PWM channels{detail}") from failures[0]

    def readings(self, now: float) -> tuple[FanReading, FanReading]:
        result: list[FanReading] = []
        for index, (previous, at) in enumerate(self._last):
            elapsed = max(now - at, 0.001)
            pulses = self._pulses[index] - previous
            self._last[index] = (self._pulses[index], now)
            rpm = pulses / self.config.pulses_per_revolution[index] / elapsed * 60
            result.append(FanReading(rpm if pulses else None, "RUNNING" if pulses else "NO TACH"))
        return (result[0], result[1])

    def release(self) -> None:
        if getattr(self, "pi", None) is None:
            return
        failures: list[Exception] = []
        for callback in self._callbacks:
            try:
                callback.cancel()
            except Exception as error:  # noqa: BLE001 - release both PWM channels despite callback failure.
                failures.append(error)
        self._callbacks = []
        for gpio in self.config.pwm_gpio_bcm:
            try:
                self.pi.hardware_PWM(gpio, 0, 0)
            except Exception as error:  # noqa: BLE001 - always attempt both physical channels.
                failures.append(error)
        try:
            self.pi.stop()
        except Exception as error:  # noqa: BLE001 - all GPIO release attempts have completed.
            failures.append(error)
        finally:
            self.pi = None
        if failures:
            raise RuntimeError("failed to release one or more fan PWM channels") from failures[0]


def create_hardware(config: HardwareConfig) -> FanHardware:
    return FakeHardware() if config.backend == "fake" else RaspberryPiHardware(config)
