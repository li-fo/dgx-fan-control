from dgx_fan.config import ControlConfig, HardwareConfig
from dgx_fan.controller import FanController
from dgx_fan.models import EndpointSnapshot, FanReading, GPUStat, Stage


def _controller() -> FanController:
    control = ControlConfig(True, 90, 2, 75, 10, (Stage(45, 20), Stage(55, 50), Stage(70, 80), Stage(None, 100)))
    hardware = HardwareConfig("fake", 18, 25000, True, (23, 24), (2, 2), 1, 5)
    return FanController(control, hardware)


def _endpoints(temp: float, healthy: bool = True) -> tuple[EndpointSnapshot, ...]:
    return (EndpointSnapshot("one", "One", healthy, 0, gpus=(GPUStat("GPU-a", "A100", temperature_celsius=temp),)),)


FANS = (FanReading(1000, "RUNNING"), FanReading(1000, "RUNNING"))


def test_startup_recovery_curve_hysteresis_cap_and_off() -> None:
    controller = _controller()
    snapshot = controller.update(_endpoints(40), FANS, 0)
    assert (snapshot.duty_percent, snapshot.active_stage) == (20, 0)
    assert controller.update(_endpoints(60), FANS, 1).duty_percent == 80
    assert controller.update(_endpoints(54), FANS, 2).duty_percent == 80
    assert controller.update(_endpoints(52), FANS, 3).duty_percent == 50
    controller.set_power(False)
    assert controller.update(_endpoints(40), FANS, 4).duty_percent == 0
    controller.set_power(True)
    assert controller.update(_endpoints(40), FANS, 5).state == "STARTUP BOOST"


def test_emergency_and_endpoint_failure_are_safe() -> None:
    controller = _controller()
    assert controller.update(_endpoints(80), FANS, 0).duty_percent == 100
    assert controller.update(_endpoints(40, healthy=False), FANS, 1).reason == "endpoint unavailable"


def test_safety_recovery_and_stall_latch() -> None:
    controller = _controller()
    assert controller.update(_endpoints(80), FANS, 0).state == "SAFETY OVERRIDE"
    assert controller.update(_endpoints(40), FANS, 5).state == "SAFETY OVERRIDE"
    assert controller.update(_endpoints(40), FANS, 15).state == "AUTO ON"
    no_tach = (FanReading(None, "NO TACH"), FanReading(1000, "RUNNING"))
    controller.update(_endpoints(40), no_tach, 16)
    controller.update(_endpoints(40), no_tach, 21)
    snapshot = controller.update(_endpoints(40), no_tach, 26)
    assert snapshot.reason == "fan stalled"
    assert snapshot.fans[0].state == "STALLED"
    assert snapshot.fans[1].state == "RUNNING"


def test_short_control_ticks_latch_stall_without_collection_interval() -> None:
    controller = _controller()
    no_tach = (FanReading(None, "NO TACH"), FanReading(1000, "RUNNING"))
    controller.update(_endpoints(40), no_tach, 0)
    controller.update(_endpoints(40), no_tach, 5)
    assert controller.update(_endpoints(40), no_tach, 10).fans[0].state == "STALLED"
