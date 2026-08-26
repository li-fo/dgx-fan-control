from dgx_fan.hardware import FakeHardware


def test_fake_hardware_safe_release() -> None:
    hardware = FakeHardware()
    hardware.set_duty(20)
    hardware.release()
    hardware.release()
    assert hardware.released and hardware.duty == 100
