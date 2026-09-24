from dataclasses import replace

from dgx_fan.config import ControlConfig, HardwareConfig
from dgx_fan.controller import FanController
from dgx_fan.models import EndpointSnapshot, FanReading, GPUStat, Stage


def _controller(
    fallback_speed_percent: int = 100, *, fan_mode: str = "independent", max_speed_percent: int = 90
) -> FanController:
    control = ControlConfig(
        True, max_speed_percent, 2, 75, 10,
        (Stage(45, 20), Stage(55, 50), Stage(70, 80), Stage(None, 100)),
        ("one", "two"),
        fallback_speed_percent,
        fan_mode,
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


def test_linked_mode_uses_higher_capped_mapped_demand_in_both_directions() -> None:
    controller = _controller(fan_mode="linked", max_speed_percent=100)
    first_hot = controller.update(_endpoints(40, 71), FANS, 0)
    assert first_hot.state == "AUTO ON" and first_hot.duty_percents == (100, 100)
    assert first_hot.active_stages == (3, 3) and controller._stages == [0, 3]
    second_hot = controller.update(_endpoints(71, 40), FANS, 1)
    assert second_hot.state == "AUTO ON" and second_hot.duty_percents == (100, 100)
    assert second_hot.active_stages == (3, 3) and controller._stages == [3, 0]
    assert controller.update(_endpoints(40, 40), FANS, 2).duty_percents == (20, 20)
    controller.reconfigure(replace(controller.config, fan_mode="independent"), controller.hardware)
    assert controller.update(_endpoints(71, 40), FANS, 3).duty_percents == (100, 20)


def test_linked_mode_preserves_stages_and_applies_zero_and_normal_cap() -> None:
    controller = _controller(fan_mode="linked", max_speed_percent=25)
    assert controller.update(_endpoints(60, 40), FANS, 0).duty_percents == (25, 25)
    assert controller.update(_endpoints(54, 40), FANS, 1).active_stages == (2, 2)
    assert controller._stages == [2, 0]  # Snapshot projection must not change hysteresis.
    control = replace(controller.config, stages=(Stage(45, 0), Stage(55, 0), Stage(70, 0), Stage(None, 100)))
    controller.reconfigure(control, controller.hardware)
    assert controller.update(_endpoints(40, 40), FANS, 2).duty_percents == (0, 0)


def test_linked_equal_speeds_and_cap_still_show_highest_selected_stage() -> None:
    controller = _controller(fan_mode="linked", max_speed_percent=25)
    controller.reconfigure(replace(controller.config, stages=(
        Stage(45, 20), Stage(55, 20), Stage(70, 80), Stage(None, 100),
    )), controller.hardware)
    equal_speed = controller.update(_endpoints(40, 52), FANS, 0)
    assert equal_speed.duty_percents == (20, 20)
    assert equal_speed.active_stages == (1, 1) and controller._stages == [0, 1]
    capped = controller.update(_endpoints(40, 60), FANS, 1)
    assert capped.duty_percents == (25, 25)
    assert capped.active_stages == (2, 2) and controller._stages == [0, 2]
    hysteresis = controller.update(_endpoints(40, 54), FANS, 2)
    assert hysteresis.active_stages == (2, 2) and controller._stages == [0, 2]


def test_mode_reconfigure_preserves_latches_and_linked_boost_is_shared() -> None:
    controller = _controller()
    controller.set_power(False)
    assert controller.update(_endpoints(40, 60), FANS, 0).duty_percents == (0, 0)
    controller.reconfigure(replace(controller.config, fan_mode="linked"), controller.hardware)
    controller.set_power(True)
    snapshot = controller.update(_endpoints(40, 60), FANS, 1)
    assert snapshot.duty_percents == (100, 100)
    assert snapshot.active_stages == (2, 2)
    assert controller._stages == [0, 2]
    assert controller._previous_duties == [100, 100]
    assert controller.update(_endpoints(40, 60), FANS, 2).duty_percents == (80, 80)


def test_global_off_and_per_fan_startup_boost() -> None:
    controller = _controller()
    controller.set_power(False)
    assert controller.update(_endpoints(40, 60), FANS, 0).duty_percents == (0, 0)
    controller.set_power(True)
    snapshot = controller.update(_endpoints(40, 60), FANS, 1)
    assert snapshot.state == "STARTUP BOOST" and snapshot.duty_percents == (100, 100)
    assert controller.update(_endpoints(40, 60), FANS, 2).duty_percents == (20, 80)


def test_off_and_safety_snapshots_hide_preserved_internal_stages() -> None:
    controller = _controller(fan_mode="linked")
    normal = controller.update(_endpoints(40, 60), FANS, 0)
    assert normal.active_stages == (2, 2) and controller._stages == [0, 2]

    controller.set_power(False)
    off = controller.update(_endpoints(40, 60), FANS, 1)
    assert off.state == "USER OFF" and off.active_stages == (None, None)
    assert controller._stages == [0, 2]

    controller.set_power(True)
    safety = controller.update(_endpoints(40, 80), FANS, 2)
    assert safety.state == "SAFETY OVERRIDE" and safety.active_stages == (None, None)
    assert controller._stages == [0, 2]
    recovery = controller.update(_endpoints(40, 72), FANS, 3)
    assert recovery.reason == "safety recovery dwell"
    assert recovery.active_stages == (None, None) and controller._stages == [0, 2]
    restored = controller.update(_endpoints(40, 60), FANS, 13)
    assert restored.state == "AUTO ON" and restored.active_stages == (2, 2)


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


def test_reconfigure_preserves_power_safety_stall_and_boost_state() -> None:
    controller = _controller(35)
    controller.set_power(False)
    controller._safety_active = True
    controller._recovery_since = 9.0
    controller._stalled[0] = True
    controller._no_tach_since[0] = 4.0
    controller._boost_until[1] = 20.0
    controller._stages = [2, 1]
    updated_control = replace(
        controller.config,
        hysteresis_celsius=3.0,
        emergency_temperature_celsius=80.0,
        recovery_seconds=20.0,
    )
    updated_hardware = replace(
        controller.hardware,
        startup_boost_seconds=2.0,
        stall_timeout_seconds=8.0,
        shutdown_mode="off",
    )

    controller.reconfigure(updated_control, updated_hardware)

    assert controller.power is False
    assert controller._safety_active is True
    assert controller._recovery_since is None
    assert controller._stalled == [True, False]
    assert controller._no_tach_since == [4.0, None]
    assert controller._boost_until == [None, 20.0]
    assert controller._stages == [None, None]
