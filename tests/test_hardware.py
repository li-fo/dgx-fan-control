from dgx_fan.hardware import FakeHardware
from dgx_fan.models import FanReading


def test_fake_hardware_safe_release() -> None:
    hardware = FakeHardware()
    hardware.set_duty(20)
    hardware.release()
    hardware.release()
    assert hardware.released and hardware.duty == 100


def test_fake_hardware_simulates_running_and_allows_stall_probe() -> None:
    hardware = FakeHardware()
    hardware.set_duty(50)
    assert all(fan.state == "RUNNING" for fan in hardware.readings(0))
    hardware.set_readings((FanReading(None, "NO TACH"), FanReading(800, "RUNNING")))
    assert hardware.readings(1)[0].state == "NO TACH"
