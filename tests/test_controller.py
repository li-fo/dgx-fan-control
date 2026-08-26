from dgx_fan.config import ControlConfig, HardwareConfig
from dgx_fan.controller import FanController
from dgx_fan.models import EndpointSnapshot, FanReading, GPUStat, Stage


def _controller() -> FanController:
    control = ControlConfig(
        True, 90, 2, 75, 10,
        (Stage(45, 20), Stage(55, 50), Stage(70, 80), Stage(None, 100)),
        ("one", "two"),
    )
    hardware = HardwareConfig("fake", (18, 19), 25000, True, (23, 24), (2, 2), 1, 5)
    return FanController(control, hardware)


def _endpoints(
    first: float | None, second: float | None, *, healthy: bool = True
) -> tuple[EndpointSnapshot, ...]:
    def endpoint(endpoint_id: str, temp: float | None) -> EndpointSnapshot:
        gpus = () if temp is None else (GPUStat(f"GPU-{endpoint_id}", "A100", temperature_celsius=temp),)
        return EndpointSnapshot(endpoint_id, endpoint_id.title(), healthy, 0, gpus=gpus)

    return (endpoint("one", first), endpoint("two", second))


FANS = (FanReading(1000, "RUNNING"), FanReading(1000, "RUNNING"))


def test_normal_curve_hysteresis_and_targets_are_independent() -> None:
    controller = _controller()
    snapshot = controller.update(_endpoints(40, 60), FANS, 0)
    assert snapshot.duty_percents == (20, 80)
    assert snapshot.active_stages == (0, 2)
    assert snapshot.fan_temperatures_celsius == (40, 60)
    assert controller.update(_endpoints(54, 52), FANS, 1).duty_percents == (50, 50)
    assert controller.update(_endpoints(54, 52), FANS, 2).active_stages == (1, 1)


def test_global_off_and_per_fan_startup_boost() -> None:
    controller = _controller()
    controller.set_power(False)
    assert controller.update(_endpoints(40, 60), FANS, 0).duty_percents == (0, 0)
    controller.set_power(True)
    snapshot = controller.update(_endpoints(40, 60), FANS, 1)
    assert snapshot.state == "STARTUP BOOST" and snapshot.duty_percents == (100, 100)
    assert controller.update(_endpoints(40, 60), FANS, 2).duty_percents == (20, 80)


def test_any_endpoint_failure_missing_temperature_or_emergency_couples_safety() -> None:
    controller = _controller()
    assert controller.update(_endpoints(40, 80), FANS, 0).duty_percents == (100, 100)
    assert controller.update(_endpoints(40, None), FANS, 1).reason == "no valid GPU temperature"
    unhealthy = _endpoints(40, 40, healthy=False)
    assert controller.update(unhealthy, FANS, 2).reason == "endpoint unavailable"


def test_unmapped_configured_endpoint_emergency_still_couples_safety() -> None:
    control = ControlConfig(
        True, 90, 2, 75, 10,
        (Stage(45, 20), Stage(55, 50), Stage(70, 80), Stage(None, 100)),
        ("one", "one"),
    )
    hardware = HardwareConfig("fake", (18, 19), 25000, True, (23, 24), (2, 2), 1, 5)
    snapshot = FanController(control, hardware).update(_endpoints(40, 80), FANS, 0)
    assert snapshot.reason == "emergency temperature"
    assert snapshot.duty_percents == (100, 100)


def test_safety_recovery_and_one_fan_stall_latch_couple_both_outputs() -> None:
    controller = _controller()
    assert controller.update(_endpoints(80, 40), FANS, 0).state == "SAFETY OVERRIDE"
    assert controller.update(_endpoints(40, 40), FANS, 5).state == "SAFETY OVERRIDE"
    assert controller.update(_endpoints(40, 40), FANS, 15).state == "AUTO ON"
    no_tach = (FanReading(None, "NO TACH"), FanReading(1000, "RUNNING"))
    controller.update(_endpoints(40, 40), no_tach, 16)
    controller.update(_endpoints(40, 40), no_tach, 21)
    snapshot = controller.update(_endpoints(40, 40), no_tach, 26)
    assert snapshot.reason == "fan stalled"
    assert snapshot.duty_percents == (100, 100)
    assert snapshot.fans[0].state == "STALLED"


def test_one_fan_no_tach_boost_does_not_change_other_normal_target() -> None:
    controller = _controller()
    no_tach = (FanReading(None, "NO TACH"), FanReading(1000, "RUNNING"))
    controller.update(_endpoints(40, 60), no_tach, 0)
    snapshot = controller.update(_endpoints(40, 60), no_tach, 5)
    assert snapshot.state == "STARTUP BOOST"
    assert snapshot.duty_percents == (100, 80)
