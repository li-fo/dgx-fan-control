from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast


@dataclass(frozen=True)
class Stage:
    max_temperature_celsius: float | None
    speed_percent: int


@dataclass(frozen=True)
class GPUStat:
    key: str
    name: str
    memory_used_mib: float | None = None
    memory_total_mib: float | None = None
    utilization_percent: float | None = None
    temperature_celsius: float | None = None


@dataclass(frozen=True)
class EndpointSnapshot:
    endpoint_id: str
    name: str
    healthy: bool
    age_seconds: float | None
    stale: bool = False
    error: str | None = None
    gpus: tuple[GPUStat, ...] = ()
    sample_revision: int = 0


@dataclass(frozen=True)
class FanReading:
    rpm: float | None
    state: Literal["RUNNING", "STOPPED", "STALLED", "NO TACH"]


@dataclass(frozen=True, init=False)
class ControlSnapshot:
    duty_percents: tuple[int, int]
    reason: str
    state: Literal["AUTO ON", "USER OFF", "SAFETY OVERRIDE", "STARTUP BOOST"]
    max_temperature_celsius: float | None
    active_stages: tuple[int | None, int | None]
    fan_temperatures_celsius: tuple[float | None, float | None]
    fan_endpoint_ids: tuple[str, str]
    fans: tuple[FanReading, FanReading]
    endpoint_snapshots: tuple[EndpointSnapshot, ...] = field(default_factory=tuple)

    def __init__(
        self,
        duty_percents: tuple[int, int] | int,
        reason: str,
        state: Literal["AUTO ON", "USER OFF", "SAFETY OVERRIDE", "STARTUP BOOST"],
        max_temperature_celsius: float | None,
        active_stages: tuple[int | None, int | None] | int | None,
        fan_temperatures_celsius: tuple[float | None, float | None] | tuple[FanReading, FanReading],
        fan_endpoint_ids: tuple[str, str] | tuple[EndpointSnapshot, ...],
        fans: tuple[FanReading, FanReading] | None = None,
        endpoint_snapshots: tuple[EndpointSnapshot, ...] = (),
    ) -> None:
        """Construct a per-fan snapshot; accept the pre-v2 positional form for UI extensions."""
        if fans is None:
            # Compatibility for existing presentation callers: scalar duty/stage, fans, endpoints.
            assert isinstance(duty_percents, int)
            assert active_stages is None or isinstance(active_stages, int)
            legacy_fans = fan_temperatures_celsius
            legacy_endpoints = fan_endpoint_ids
            assert len(legacy_fans) == 2 and len(legacy_endpoints) >= 0
            duties = (duty_percents, duty_percents)
            stages = (active_stages, active_stages)
            temperatures = (max_temperature_celsius, max_temperature_celsius)
            endpoint_ids = ("unknown", "unknown")
            actual_fans = legacy_fans
            actual_endpoints = legacy_endpoints
        else:
            assert isinstance(duty_percents, tuple) and isinstance(active_stages, tuple)
            assert isinstance(fan_temperatures_celsius, tuple) and isinstance(fan_endpoint_ids, tuple)
            duties = duty_percents
            stages = active_stages
            temperatures = cast(tuple[float | None, float | None], fan_temperatures_celsius)
            endpoint_ids = cast(tuple[str, str], fan_endpoint_ids)
            actual_fans = fans
            actual_endpoints = endpoint_snapshots
        object.__setattr__(self, "duty_percents", duties)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "max_temperature_celsius", max_temperature_celsius)
        object.__setattr__(self, "active_stages", stages)
        object.__setattr__(self, "fan_temperatures_celsius", temperatures)
        object.__setattr__(self, "fan_endpoint_ids", endpoint_ids)
        object.__setattr__(self, "fans", actual_fans)
        object.__setattr__(self, "endpoint_snapshots", actual_endpoints)

    @property
    def duty_percent(self) -> int:
        """Compatibility summary for callers that only display the highest fan duty."""
        return max(self.duty_percents)

    @property
    def active_stage(self) -> int | None:
        """Compatibility summary for callers that only display the highest active stage."""
        return max((stage for stage in self.active_stages if stage is not None), default=None)
