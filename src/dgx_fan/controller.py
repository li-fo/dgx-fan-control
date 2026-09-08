from __future__ import annotations

from dataclasses import replace
from typing import Literal

from .config import ControlConfig, HardwareConfig
from .models import ControlSnapshot, EndpointSnapshot, FanReading


class FanController:
    """Two independent normal-control channels with one coupled fail-safe state."""

    def __init__(self, config: ControlConfig, hardware: HardwareConfig) -> None:
        self.config, self.hardware = config, hardware
        self.power = config.enabled_at_startup
        self._stages: list[int | None] = [None, None]
        self._recovery_since: float | None = None
        self._safety_active = False
        self._boost_until: list[float | None] = [None, None]
        self._previous_duties = [config.fallback_speed_percent, config.fallback_speed_percent]
        self._no_tach_since: list[float | None] = [None, None]
        self._restart_attempted = [False, False]
        self._stalled = [False, False]

    def set_power(self, enabled: bool) -> None:
        self.power = enabled

    def reconfigure(self, config: ControlConfig, hardware: HardwareConfig) -> None:
        """Apply policy without discarding safety, tach, or boost observations."""
        curve_changed = (self.config.stages, self.config.fan_endpoint_ids, self.config.hysteresis_celsius) != (
            config.stages, config.fan_endpoint_ids, config.hysteresis_celsius
        )
        recovery_changed = (self.config.recovery_seconds, self.config.emergency_temperature_celsius, self.config.hysteresis_celsius) != (config.recovery_seconds, config.emergency_temperature_celsius, config.hysteresis_celsius)
        self.config, self.hardware = config, hardware
        if curve_changed:
            self._stages = [None, None]
        if recovery_changed and self._safety_active:
            self._recovery_since = None

    def update(
        self, endpoints: tuple[EndpointSnapshot, ...], fans: tuple[FanReading, FanReading], now: float
    ) -> ControlSnapshot:
        fans = self._observe_tach(fans, now)
        temperatures = self._fan_temperatures(endpoints)
        all_temperatures = [
            gpu.temperature_celsius
            for endpoint in endpoints
            for gpu in endpoint.gpus
            if gpu.temperature_celsius is not None
        ]
        maximum = max(all_temperatures) if all_temperatures else None
        unhealthy = not endpoints or any(not endpoint.healthy for endpoint in endpoints)
        missing_temperature = any(temperature is None for temperature in temperatures)
        stalled = any(self._stalled) or any(fan.state == "STALLED" for fan in fans)
        emergency = (
            maximum is not None and maximum >= self.config.emergency_temperature_celsius
        )
        safety_reason = (
            "endpoint unavailable"
            if unhealthy
            else "no valid GPU temperature"
            if missing_temperature
            else "fan stalled"
            if stalled
            else "emergency temperature"
            if emergency
            else None
        )
        if safety_reason:
            self._safety_active = True
            self._recovery_since = None
            self._previous_duties = list(self._fallback_duties)
            return self._snapshot(
                self._fallback_duties,
                safety_reason,
                "SAFETY OVERRIDE",
                maximum,
                temperatures,
                fans,
                endpoints,
            )
        assert temperatures[0] is not None and temperatures[1] is not None
        normal_temperatures = (temperatures[0], temperatures[1])
        if self._safety_active:
            below_recovery = maximum is not None and maximum < (
                self.config.emergency_temperature_celsius - self.config.hysteresis_celsius
            )
            if below_recovery:
                if self._recovery_since is None:
                    self._recovery_since = now
                if now - self._recovery_since < self.config.recovery_seconds:
                    return self._snapshot(
                        self._fallback_duties,
                        "safety recovery dwell",
                        "SAFETY OVERRIDE",
                        maximum,
                        temperatures, fans, endpoints,
                    )
            else:
                self._recovery_since = None
                return self._snapshot(
                    self._fallback_duties,
                    "safety recovery temperature",
                    "SAFETY OVERRIDE",
                    maximum,
                    temperatures, fans, endpoints,
                )
            self._safety_active = False
            self._recovery_since = None
        if not self.power:
            self._previous_duties = [0, 0]
            return self._snapshot((0, 0), "user disabled", "USER OFF", maximum, temperatures, fans, endpoints)

        stages = (
            self._select_stage(0, normal_temperatures[0]),
            self._select_stage(1, normal_temperatures[1]),
        )
        targets = tuple(min(self.config.stages[stage].speed_percent, self.config.max_speed_percent) for stage in stages)
        duties = list(targets)
        boosting = False
        for index, target in enumerate(targets):
            if self._previous_duties[index] == 0 and target > 0:
                self._boost_until[index] = now + self.hardware.startup_boost_seconds
            boost_until = self._boost_until[index]
            if boost_until is not None and now < boost_until:
                duties[index] = 100
                boosting = True
            else:
                self._boost_until[index] = None
        self._previous_duties = duties
        return self._snapshot(
            (duties[0], duties[1]), "startup boost" if boosting else "temperature curve",
            "STARTUP BOOST" if boosting else "AUTO ON", maximum, temperatures, fans, endpoints, stages,
        )

    @property
    def _fallback_duties(self) -> tuple[int, int]:
        return (self.config.fallback_speed_percent, self.config.fallback_speed_percent)

    def _snapshot(
        self,
        duties: tuple[int, int],
        reason: str,
        state: Literal["AUTO ON", "USER OFF", "SAFETY OVERRIDE", "STARTUP BOOST"],
        maximum: float | None,
        temperatures: tuple[float | None, float | None],
        fans: tuple[FanReading, FanReading],
        endpoints: tuple[EndpointSnapshot, ...],
        stages: tuple[int | None, int | None] | None = None,
    ) -> ControlSnapshot:
        return ControlSnapshot(
            duties, reason, state, maximum,
            stages if stages is not None else (self._stages[0], self._stages[1]),
            temperatures, self.config.fan_endpoint_ids, fans, endpoints,
        )

    def _fan_temperatures(
        self, endpoints: tuple[EndpointSnapshot, ...]
    ) -> tuple[float | None, float | None]:
        by_id = {endpoint.endpoint_id: endpoint for endpoint in endpoints}
        result: list[float | None] = []
        for endpoint_id in self.config.fan_endpoint_ids:
            endpoint = by_id.get(endpoint_id)
            values = [
                gpu.temperature_celsius for gpu in endpoint.gpus if gpu.temperature_celsius is not None
            ] if endpoint is not None else []
            result.append(max(values) if values else None)
        return (result[0], result[1])

    def _select_stage(self, fan_index: int, temperature: float) -> int:
        candidate = next(
            index for index, stage in enumerate(self.config.stages)
            if stage.max_temperature_celsius is None or temperature <= stage.max_temperature_celsius
        )
        previous = self._stages[fan_index]
        if previous is not None and candidate < previous:
            boundary = self.config.stages[candidate].max_temperature_celsius
            if boundary is not None and temperature > boundary - self.config.hysteresis_celsius:
                return previous
        self._stages[fan_index] = candidate
        return candidate

    def _observe_tach(
        self, fans: tuple[FanReading, FanReading], now: float
    ) -> tuple[FanReading, FanReading]:
        readings = list(fans)
        for index, fan in enumerate(fans):
            if self._stalled[index]:
                readings[index] = replace(fan, state="STALLED")
                continue
            if self._previous_duties[index] <= 0:
                self._no_tach_since[index] = None
                self._restart_attempted[index] = False
                self._boost_until[index] = None
                continue
            if fan.state == "RUNNING":
                self._no_tach_since[index] = None
                self._restart_attempted[index] = False
                continue
            if self._no_tach_since[index] is None:
                self._no_tach_since[index] = now
                continue
            started_at = self._no_tach_since[index]
            assert started_at is not None
            if now - started_at < self.hardware.stall_timeout_seconds:
                continue
            if self._restart_attempted[index]:
                self._stalled[index] = True
                readings[index] = replace(fan, state="STALLED")
            else:
                self._restart_attempted[index] = True
                self._no_tach_since[index] = now
                self._boost_until[index] = now + self.hardware.startup_boost_seconds
        return (readings[0], readings[1])
