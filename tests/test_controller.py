from dgx_fan.config import ControlConfig, HardwareConfig
from dgx_fan.controller import FanController
from dgx_fan.models import EndpointSnapshot, FanReading, GPUStat, Stage


def _controller(fallback_speed_percent: int = 100) -> FanController:
    control = ControlConfig(
        True, 90, 2, 75, 10,
        (Stage(45, 20), Stage(55, 50), Stage(70, 80), Stage(None, 100)),
        ("one", "two"),
        fallback_speed_percent,
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


def test_configured_fallback_couples_all_immediate_safety_reasons() -> None:
    controller = _controller(35)
    cases = (
        ((), "endpoint unavailable"),
        (_endpoints(40, None), "no valid GPU temperature"),
        (_endpoints(40, 80), "emergency temperature"),
        (_endpoints(40, 40, healthy=False), "endpoint unavailable"),
    )
    for endpoints, reason in cases:
        snapshot = controller.update(endpoints, FANS, 0)
        assert snapshot.reason == reason
        assert snapshot.state == "SAFETY OVERRIDE"
        assert snapshot.duty_percents == (35, 35)


def test_fallback_speed_is_not_limited_by_max_speed_percent() -> None:
    snapshot = _controller(95).update((), FANS, 0)
    assert snapshot.state == "SAFETY OVERRIDE"
    assert snapshot.duty_percents == (95, 95)


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


def test_unmapped_endpoint_must_clear_global_recovery_boundary() -> None:
    control = ControlConfig(
        True, 90, 2, 75, 10,
        (Stage(45, 20), Stage(55, 50), Stage(70, 80), Stage(None, 100)),
        ("one", "one"),
    )
    hardware = HardwareConfig("fake", (18, 19), 25000, True, (23, 24), (2, 2), 1, 5)
    controller = FanController(control, hardware)
    assert controller.update(_endpoints(40, 80), FANS, 0).state == "SAFETY OVERRIDE"
    assert controller.update(_endpoints(40, 74), FANS, 5).reason == "safety recovery temperature"
    assert controller.update(_endpoints(40, 72), FANS, 6).reason == "safety recovery dwell"


def test_configured_fallback_couples_safety_recovery_and_fan_stall() -> None:
    controller = _controller(35)
    assert controller.update(_endpoints(80, 40), FANS, 0).state == "SAFETY OVERRIDE"
    recovery_temperature = controller.update(_endpoints(40, 74), FANS, 5)
    assert recovery_temperature.reason == "safety recovery temperature"
    assert recovery_temperature.duty_percents == (35, 35)
    recovery_dwell = controller.update(_endpoints(40, 40), FANS, 6)
    assert recovery_dwell.reason == "safety recovery dwell"
    assert recovery_dwell.duty_percents == (35, 35)
    assert controller.update(_endpoints(40, 40), FANS, 16).state == "AUTO ON"
    no_tach = (FanReading(None, "NO TACH"), FanReading(1000, "RUNNING"))
    controller.update(_endpoints(40, 40), no_tach, 17)
    controller.update(_endpoints(40, 40), no_tach, 22)
    snapshot = controller.update(_endpoints(40, 40), no_tach, 27)
    assert snapshot.reason == "fan stalled"
    assert snapshot.duty_percents == (35, 35)
    assert snapshot.fans[0].state == "STALLED"


def test_one_fan_no_tach_boost_does_not_change_other_normal_target() -> None:
    controller = _controller()
    no_tach = (FanReading(None, "NO TACH"), FanReading(1000, "RUNNING"))
    controller.update(_endpoints(40, 60), no_tach, 0)
    snapshot = controller.update(_endpoints(40, 60), no_tach, 5)
    assert snapshot.state == "STARTUP BOOST"
    assert snapshot.duty_percents == (100, 80)


def test_zero_percent_stage_resets_non_latched_tach_timeout_before_restart() -> None:
    control = ControlConfig(
        True, 100, 2, 75, 10,
        (Stage(45, 0), Stage(55, 50), Stage(70, 80), Stage(None, 100)),
        ("one", "two"),
    )
    hardware = HardwareConfig("fake", (18, 19), 25000, True, (23, 24), (2, 2), 1, 5)
    controller = FanController(control, hardware)
    no_tach = (FanReading(None, "NO TACH"), FanReading(1000, "RUNNING"))
    controller.update(_endpoints(40, 40), no_tach, 0)
    controller.update(_endpoints(40, 40), no_tach, 1)
    assert controller.update(_endpoints(60, 40), no_tach, 6).state == "STARTUP BOOST"
    assert controller.update(_endpoints(60, 40), no_tach, 7).fans[0].state != "STALLED"
