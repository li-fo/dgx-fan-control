from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict
from collections.abc import Callable

import httpx
from prometheus_client.parser import text_string_to_metric_families

from .config import EndpointConfig
from .models import MemoryStat, NodeMemorySnapshot

Clock = Callable[[], float]
_REQUIRED = {"node_memory_MemTotal_bytes", "node_memory_MemAvailable_bytes"}
_SIGNED_64_MAX = 9223372036854775807


def parse_memory_metrics(text: str) -> MemoryStat:
    """Return physical Linux memory occupancy from one unlabelled meminfo sample."""
    values: dict[str, list[float]] = defaultdict(list)
    for family in text_string_to_metric_families(text):
        if family.name not in _REQUIRED:
            continue
        for sample in family.samples:
            # Meminfo is host-wide. A labelled occurrence would make source
            # selection ambiguous, even if an unlabelled sample is also present.
            if sample.labels:
                raise ValueError("node memory metrics must be unlabelled")
            values[family.name].append(float(sample.value))
    if any(len(values[name]) != 1 for name in _REQUIRED):
        raise ValueError("missing or ambiguous node memory metrics")
    total, available = (values["node_memory_MemTotal_bytes"][0], values["node_memory_MemAvailable_bytes"][0])
    # DCGM and some exporters conventionally use signed-64 maximum as an
    # unavailable sentinel. Reject that exact sentinel and larger values
    # without imposing an artificial host-memory ceiling.
    if (
        not math.isfinite(total)
        or not math.isfinite(available)
        or total >= _SIGNED_64_MAX
        or available >= _SIGNED_64_MAX
        or total <= 0
        or not 0 <= available <= total
    ):
        raise ValueError("invalid node memory metrics")
    mib = 1024 * 1024
    return MemoryStat((total - available) / mib, total / mib)


class NodeExporterCollector:
    """Best-effort node memory collector isolated from DCGM safety telemetry."""

    def __init__(
        self,
        endpoints: tuple[EndpointConfig, ...],
        timeout_seconds: float,
        stale_after_seconds: float,
        retry_count: int = 0,
        retry_delay_seconds: float = 10.0,
    ) -> None:
        self.endpoints, self.timeout, self.stale_after = endpoints, timeout_seconds, stale_after_seconds
        self.retry_count, self.retry_delay_seconds = retry_count, retry_delay_seconds
        self._last_good: dict[str, tuple[float, MemoryStat]] = {}
        self._errors: dict[str, str | None] = {}
        self._retrying: dict[str, int] = {}
        self._failed_attempts: dict[str, int] = {}
        self._revisions: dict[str, int] = defaultdict(int)

    async def collect_endpoint(
        self, endpoint: EndpointConfig, now: float | Clock | None = None
    ) -> NodeMemorySnapshot:
        assert endpoint.node_exporter_url is not None
        for attempt in range(self.retry_count + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(endpoint.node_exporter_url)
                if response.status_code in {408, 429} or 500 <= response.status_code <= 599:
                    raise _RetryableHTTPStatus(response.status_code)
                response.raise_for_status()
                memory = parse_memory_metrics(response.text)
            except asyncio.CancelledError:
                raise
            except (httpx.TransportError, _RetryableHTTPStatus) as error:
                if attempt < self.retry_count:
                    self._retrying[endpoint.id] = attempt + 1
                    await asyncio.sleep(self.retry_delay_seconds)
                    continue
                self._retrying.pop(endpoint.id, None)
                self._errors[endpoint.id] = str(error)
                self._failed_attempts[endpoint.id] = attempt + 1
                break
            except (httpx.HTTPError, ValueError) as error:
                self._retrying.pop(endpoint.id, None)
                self._errors[endpoint.id] = str(error)
                self._failed_attempts[endpoint.id] = attempt + 1
                break
            except Exception as error:  # noqa: BLE001 - endpoint isolation.
                self._retrying.pop(endpoint.id, None)
                self._errors[endpoint.id] = f"collector failure: {str(error) or type(error).__name__}"
                self._failed_attempts[endpoint.id] = attempt + 1
                break
            else:
                self._last_good[endpoint.id] = (self._completed_at(now), memory)
                self._errors[endpoint.id] = None
                self._retrying.pop(endpoint.id, None)
                self._failed_attempts.pop(endpoint.id, None)
                self._revisions[endpoint.id] += 1
                break
        return self.snapshot(endpoint, now)

    @staticmethod
    def _completed_at(now: float | Clock | None) -> float:
        return now() if callable(now) else time.monotonic() if now is None else now

    @staticmethod
    def _current_at(now: float | Clock | None) -> float:
        return time.monotonic() if now is None else now() if callable(now) else now

    def snapshot(self, endpoint: EndpointConfig, now: float | Clock | None = None) -> NodeMemorySnapshot:
        current = self._current_at(now)
        prior = self._last_good.get(endpoint.id)
        age = None if prior is None else max(0.0, current - prior[0])
        stale = age is None or age > self.stale_after
        error = self._errors.get(endpoint.id)
        retry_attempt = self._retrying.get(endpoint.id, 0)
        retrying = retry_attempt > 0
        if prior is None and error is None and not retrying:
            error = "awaiting first sample"
        return NodeMemorySnapshot(
            endpoint.id,
            prior is not None and not stale and error is None,
            age,
            stale,
            error,
            None if prior is None or stale else prior[1],
            self._revisions[endpoint.id],
            retrying,
            retry_attempt,
            self.retry_count,
            self._failed_attempts.get(endpoint.id, 0),
        )

    def snapshots(self, now: float | Clock | None = None) -> tuple[NodeMemorySnapshot, ...]:
        current = self._current_at(now)
        return tuple(self.snapshot(endpoint, current) for endpoint in self.endpoints)

    def mark_endpoint_unhealthy(self, endpoint: EndpointConfig, error: Exception | str) -> None:
        self._retrying.pop(endpoint.id, None)
        self._errors[endpoint.id] = f"collector failure: {str(error) or type(error).__name__}"
        self._failed_attempts[endpoint.id] = 0


class _RetryableHTTPStatus(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
