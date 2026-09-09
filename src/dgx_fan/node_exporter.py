from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from collections.abc import Callable

import httpx
from prometheus_client.parser import text_string_to_metric_families

from ._collector import Clock, CollectorStateMixin
from .config import EndpointConfig
from .models import MemoryStat, NodeMemorySnapshot

_REQUIRED = {"node_memory_MemTotal_bytes", "node_memory_MemAvailable_bytes"}
_SIGNED_64_MAX = 9223372036854775807

ArchiveCallback = Callable[[EndpointConfig, str | None, str | None], None]


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


class NodeExporterCollector(CollectorStateMixin[MemoryStat]):
    """Best-effort node memory collector isolated from DCGM safety telemetry."""

    def __init__(
        self,
        endpoints: tuple[EndpointConfig, ...],
        timeout_seconds: float,
        stale_after_seconds: float,
        retry_count: int = 0,
        retry_delay_seconds: float = 10.0,
        archive_callback: ArchiveCallback | None = None,
    ) -> None:
        self.endpoints, self.timeout, self.stale_after = endpoints, timeout_seconds, stale_after_seconds
        self.retry_count, self.retry_delay_seconds = retry_count, retry_delay_seconds
        self._archive_callback = archive_callback
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
                    self._archive(endpoint, None, f"HTTP {response.status_code}")
                    raise _RetryableHTTPStatus(response.status_code)
                if response.is_error:
                    self._archive(endpoint, None, f"HTTP {response.status_code}")
                response.raise_for_status()
                self._archive(endpoint, response.text, None)
                memory = parse_memory_metrics(response.text)
            except asyncio.CancelledError:
                raise
            except (httpx.TransportError, _RetryableHTTPStatus) as error:
                if isinstance(error, httpx.TransportError):
                    self._archive(endpoint, None, str(error))
                if attempt < self.retry_count:
                    self._record_retry_wait(endpoint.id, attempt)
                    await asyncio.sleep(self.retry_delay_seconds)
                    continue
                self._record_failure(endpoint.id, str(error), attempt + 1)
                break
            except (httpx.HTTPError, ValueError) as error:
                self._record_failure(endpoint.id, str(error), attempt + 1)
                break
            except Exception as error:  # noqa: BLE001 - endpoint isolation.
                self._record_failure(
                    endpoint.id,
                    f"collector failure: {str(error) or type(error).__name__}",
                    attempt + 1,
                )
                break
            else:
                self._record_success(endpoint.id, memory, now)
                break
        return self.snapshot(endpoint, now)

    def snapshot(self, endpoint: EndpointConfig, now: float | Clock | None = None) -> NodeMemorySnapshot:
        state = self._snapshot_state(endpoint.id, now)
        return NodeMemorySnapshot(
            endpoint.id,
            state.prior is not None and not state.stale and state.error is None,
            state.age_seconds,
            state.stale,
            state.error,
            None if state.prior is None or state.stale else state.prior[1],
            state.sample_revision,
            state.retrying,
            state.retry_attempt,
            state.retry_count,
            state.failed_attempts,
        )

    def snapshots(self, now: float | Clock | None = None) -> tuple[NodeMemorySnapshot, ...]:
        current = self._current_at(now)
        return tuple(self.snapshot(endpoint, current) for endpoint in self.endpoints)

    def mark_endpoint_unhealthy(self, endpoint: EndpointConfig, error: Exception | str) -> None:
        self._record_failure(
            endpoint.id,
            f"collector failure: {str(error) or type(error).__name__}",
            0,
        )

    def _archive(
        self, endpoint: EndpointConfig, text: str | None, error: str | None
    ) -> None:
        if self._archive_callback is None:
            return
        try:
            self._archive_callback(endpoint, text, error)
        except Exception:  # noqa: BLE001 - archive isolation is intentional.
            # Node memory remains display-only and history failures must not
            # alter its collector state or DCGM fan safety.
            return


class _RetryableHTTPStatus(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
