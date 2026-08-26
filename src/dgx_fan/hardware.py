from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from .config import HardwareConfig
from .models import FanReading


class FanHardware(Protocol):
    def set_duty(self, percent: int) -> None: ...
    def readings(self, now: float) -> tuple[FanReading, FanReading]: ...
    def release(self) -> None: ...


class FakeHardware:
    def __init__(self) -> None:
        self.duty = 100
        self.released = False
        self._readings = (FanReading(None, "NO TACH"), FanReading(None, "NO TACH"))

    def set_duty(self, percent: int) -> None:
        self.released = False
        self.duty = max(0, min(100, percent))

    def readings(self, now: float) -> tuple[FanReading, FanReading]:
        return self._readings

    def release(self) -> None:
        self.released = True
        self.duty = 100


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
            raise RuntimeError("cannot connect to pigpiod")
        self._pulses = [0, 0]
        self._last = [(0, time.monotonic()), (0, time.monotonic())]
        self._callbacks = []
        for index, gpio in enumerate(config.tach_gpio_bcm):
            self.pi.set_mode(gpio, pigpio.INPUT)
            self.pi.set_pull_up_down(gpio, pigpio.PUD_UP)
            self._callbacks.append(self.pi.callback(gpio, pigpio.FALLING_EDGE, self._tach_callback(index)))
        self.set_duty(100)

    def _tach_callback(self, index: int) -> Callable[[int, int, int], None]:
        def callback(gpio: int, level: int, tick: int) -> None:
            if level == 0:
                self._pulses[index] += 1
        return callback

    def set_duty(self, percent: int) -> None:
        effective = 100 - percent if self.config.pwm_inverted else percent
        self.pi.hardware_PWM(self.config.pwm_gpio_bcm, self.config.pwm_frequency_hz, effective * 10_000)

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
        for callback in self._callbacks:
            callback.cancel()
        self._callbacks = []
        self.pi.hardware_PWM(self.config.pwm_gpio_bcm, 0, 0)
        self.pi.stop()
        self.pi = None


def create_hardware(config: HardwareConfig) -> FanHardware:
    return FakeHardware() if config.backend == "fake" else RaspberryPiHardware(config)
