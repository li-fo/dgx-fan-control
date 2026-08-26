from __future__ import annotations

from dataclasses import replace

from .config import ControlConfig, HardwareConfig
from .models import ControlSnapshot, EndpointSnapshot, FanReading


class FanController:
    def __init__(self, config: ControlConfig, hardware: HardwareConfig) -> None:
        self.config, self.hardware = config, hardware
        self.power = config.enabled_at_startup
        self._stage: int | None = None
        self._recovery_since: float | None = None
        self._safety_active = False
        self._boost_until: float | None = None
        self._previous_duty = 100
        self._no_tach_since: list[float | None] = [None, None]
        self._restart_attempted = [False, False]
        self._stalled = [False, False]

    def set_power(self, enabled: bool) -> None:
        self.power = enabled

    def update(self, endpoints: tuple[EndpointSnapshot, ...], fans: tuple[FanReading, FanReading], now: float) -> ControlSnapshot:
        fans = self._observe_tach(fans, now)
        temperatures = [gpu.temperature_celsius for endpoint in endpoints for gpu in endpoint.gpus if gpu.temperature_celsius is not None]
        maximum = max(temperatures) if temperatures else None
        unhealthy = not endpoints or any(not endpoint.healthy for endpoint in endpoints)
        stalled = any(self._stalled) or any(fan.state == "STALLED" for fan in fans)
        safety_reason = "endpoint unavailable" if unhealthy else "no valid GPU temperature" if maximum is None else "fan stalled" if stalled else "emergency temperature" if maximum >= self.config.emergency_temperature_celsius else None
        if safety_reason:
            self._safety_active = True
            self._recovery_since = None
            self._previous_duty = 100
            return ControlSnapshot(100, safety_reason, "SAFETY OVERRIDE", maximum, self._stage, fans, endpoints)
        if self._safety_active:
            if maximum is not None and maximum < self.config.emergency_temperature_celsius - self.config.hysteresis_celsius:
                if self._recovery_since is None:
                    self._recovery_since = now
                if now - self._recovery_since < self.config.recovery_seconds:
                    return ControlSnapshot(100, "safety recovery dwell", "SAFETY OVERRIDE", maximum, self._stage, fans, endpoints)
            else:
                self._recovery_since = None
                return ControlSnapshot(100, "safety recovery temperature", "SAFETY OVERRIDE", maximum, self._stage, fans, endpoints)
            self._safety_active = False
            self._recovery_since = None
        if not self.power:
            self._previous_duty = 0
            return ControlSnapshot(0, "user disabled", "USER OFF", maximum, self._stage, fans, endpoints)
        assert maximum is not None
        stage = self._select_stage(maximum)
        target = min(self.config.stages[stage].speed_percent, self.config.max_speed_percent)
        if self._previous_duty == 0 and target > 0:
            self._boost_until = now + self.hardware.startup_boost_seconds
        if self._boost_until is not None and now < self._boost_until:
            self._previous_duty = 100
            return ControlSnapshot(100, "startup boost", "STARTUP BOOST", maximum, stage, fans, endpoints)
        self._boost_until = None
        self._previous_duty = target
        return ControlSnapshot(target, "temperature curve", "AUTO ON", maximum, stage, fans, endpoints)

    def _select_stage(self, temperature: float) -> int:
        candidate = next(index for index, stage in enumerate(self.config.stages) if stage.max_temperature_celsius is None or temperature <= stage.max_temperature_celsius)
        if self._stage is not None and candidate < self._stage:
            boundary = self.config.stages[candidate].max_temperature_celsius
            if boundary is not None and temperature > boundary - self.config.hysteresis_celsius:
                return self._stage
        self._stage = candidate
        return candidate

    def _observe_tach(self, fans: tuple[FanReading, FanReading], now: float) -> tuple[FanReading, FanReading]:
        readings = list(fans)
        if self._previous_duty <= 0:
            return fans
        for index, fan in enumerate(fans):
            if self._stalled[index]:
                readings[index] = replace(fan, state="STALLED")
                continue
            if fan.state == "RUNNING":
                self._no_tach_since[index] = None
                self._restart_attempted[index] = False
                continue
            if self._no_tach_since[index] is None:
                self._no_tach_since[index] = now
            else:
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
                    self._boost_until = now + self.hardware.startup_boost_seconds
        return (readings[0], readings[1])
