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
    # Live-only DCGM telemetry.  The archival path intentionally retains the
    # unmodified scrape response, so older history databases remain compatible.
    power_watts: float | None = None


@dataclass(frozen=True)
class MemoryStat:
    """One endpoint-wide physical memory sample, expressed in MiB."""

    used_mib: float
    total_mib: float


@dataclass(frozen=True)
class NodeMemorySnapshot:
    """Independent node_exporter memory state; never a fan-safety input."""

    endpoint_id: str
    healthy: bool
    age_seconds: float | None
    stale: bool = False
    error: str | None = None
    memory: MemoryStat | None = None
    sample_revision: int = 0
    retrying: bool = False
    retry_attempt: int = 0
    retry_count: int = 0
    failed_attempts: int = 0


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
    retrying: bool = False
    retry_attempt: int = 0
    retry_count: int = 0
    failed_attempts: int = 0
    memory_source: str = "dcgm"
    uma_memory: MemoryStat | None = None
    memory_healthy: bool = True
    memory_age_seconds: float | None = None
    memory_stale: bool = False
    memory_error: str | None = None
    memory_sample_revision: int = 0
    memory_retrying: bool = False
    memory_retry_attempt: int = 0
    memory_retry_count: int = 0
    memory_failed_attempts: int = 0


@dataclass(frozen=True)
class FanReading:
    rpm: float | None
    state: Literal["RUNNING", "STOPPED", "STALLED", "NO TACH"]


@dataclass(frozen=True)
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
