from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


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


@dataclass(frozen=True)
class FanReading:
    rpm: float | None
    state: Literal["RUNNING", "STOPPED", "STALLED", "NO TACH"]


@dataclass(frozen=True)
class ControlSnapshot:
    duty_percent: int
    reason: str
    state: Literal["AUTO ON", "USER OFF", "SAFETY OVERRIDE", "STARTUP BOOST"]
    max_temperature_celsius: float | None
    active_stage: int | None
    fans: tuple[FanReading, FanReading]
    endpoint_snapshots: tuple[EndpointSnapshot, ...] = field(default_factory=tuple)
