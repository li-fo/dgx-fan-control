import pytest

from dgx_fan.config import HardwareConfig
from dgx_fan.hardware import FakeHardware, RaspberryPiHardware
from dgx_fan.models import FanReading


def test_fake_hardware_safe_release() -> None:
    hardware = FakeHardware()
    hardware.set_duties((20, 80))
    hardware.release()
    hardware.release()
    assert hardware.released and hardware.duties == (100, 100)


def test_fake_hardware_simulates_running_and_allows_stall_probe() -> None:
    hardware = FakeHardware()
    hardware.set_duties((50, 0))
    assert tuple(fan.state for fan in hardware.readings(0)) == ("RUNNING", "STOPPED")
    assert tuple(fan.rpm for fan in hardware.readings(0)) == (600.0, 0)
    hardware.set_readings((FanReading(None, "NO TACH"), FanReading(800, "RUNNING")))
    assert hardware.readings(1)[0].state == "NO TACH"


def test_partial_pwm_write_attempts_other_channel_then_both_full_speed() -> None:
    class Pi:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, int]] = []
            self.fail_once = True

        def hardware_PWM(self, gpio: int, frequency: int, duty: int) -> None:
            self.calls.append((gpio, frequency, duty))
            if gpio == 18 and duty == 800_000 and self.fail_once:
                self.fail_once = False
                raise RuntimeError("first channel failed")

    hardware = object.__new__(RaspberryPiHardware)
    hardware.config = HardwareConfig("raspberry-pi", (18, 19), 25000, True, (23, 24), (2, 2), 1, 5)
    pi = Pi()
    hardware.pi = pi
    with pytest.raises(RuntimeError, match="one or more fan PWM"):
        hardware.set_duties((20, 30))
    assert hardware.pi.calls == [
        (18, 25000, 800_000),
        (19, 25000, 700_000),
        (18, 25000, 0),
        (19, 25000, 0),
    ]


def test_release_attempts_both_pwm_channels_after_one_failure() -> None:
    class Pi:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, int]] = []
            self.stopped = False

        def hardware_PWM(self, gpio: int, frequency: int, duty: int) -> None:
            self.calls.append((gpio, frequency, duty))
            if gpio == 18:
                raise RuntimeError("first release failed")

        def stop(self) -> None:
            self.stopped = True

    hardware = object.__new__(RaspberryPiHardware)
    hardware.config = HardwareConfig("raspberry-pi", (18, 19), 25000, True, (23, 24), (2, 2), 1, 5)
    pi = Pi()
    hardware.pi = pi
    hardware._callbacks = []
    with pytest.raises(RuntimeError, match="release one or more"):
        hardware.release()
    assert pi.calls == [(18, 0, 0), (19, 0, 0)]
    assert pi.stopped and hardware.pi is None
