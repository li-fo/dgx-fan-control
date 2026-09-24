from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from collections.abc import Callable

import httpx
from prometheus_client.parser import text_string_to_metric_families

from ._collector import Clock, CollectorStateMixin
from .config import EndpointConfig
from .models import EndpointSnapshot, GPUStat

_SENTINELS = {
    -9223372036854775808,
    -9223372036854775807,
    9223372036854775807,
    9223372036854775808,
}
_METRICS = {
    "DCGM_FI_DEV_GPU_TEMP": "temperature_celsius",
    "DCGM_FI_DEV_GPU_UTIL": "utilization_percent",
    "DCGM_FI_DEV_FB_USED": "memory_used_mib",
    "DCGM_FI_DEV_FB_FREE": "memory_free_mib",
    "DCGM_FI_DEV_FB_RESERVED": "memory_reserved_mib",
    "DCGM_FI_DEV_POWER_USAGE": "power_watts",
}

ArchiveCallback = Callable[[EndpointConfig, str | None, str | None], None]
def _valid(value: float, metric: str) -> bool:
    if not math.isfinite(value) or value in _SENTINELS:
        return False
    if metric == "temperature_celsius":
        return -20 <= value <= 150
    if metric == "utilization_percent":
        return 0 <= value <= 100
    if metric == "power_watts":
        return 0 <= value < 1_000_000
    return 0 <= value < 1_000_000_000_000


def parse_metrics(endpoint_id: str, name: str, text: str) -> tuple[GPUStat, ...]:
    values: dict[str, dict[str, float]] = defaultdict(dict)
    labels_by_key: dict[str, dict[str, str]] = {}
    for family in text_string_to_metric_families(text):
        field = _METRICS.get(family.name)
        if field is None:
            continue
        for sample in family.samples:
            labels = dict(sample.labels)
            if labels.get("GPU_I_ID") or labels.get("GPU_I_PROFILE"):
                continue  # Do not let MIG instances double-count physical GPUs.
            raw_key = labels.get("UUID") or labels.get("uuid") or labels.get("gpu")
            if not raw_key:
                continue
            value = float(sample.value)
            if _valid(value, field):
                values[raw_key][field] = value
                labels_by_key[raw_key] = labels
    result: list[GPUStat] = []
    has_legacy_metric = any(set(item) - {"power_watts"} for item in values.values())
    if not has_legacy_metric:
        return ()
    for key, item in values.items():
        # Power augments an otherwise usable scrape but cannot, by itself,
        # make the entire endpoint a healthy temperature/control sample.
        # Once a legacy metric exists, retain power-only physical GPUs so a
        # dashboard power sum cannot falsely claim completeness.
        labels = labels_by_key[key]
        used, free, reserved = item.get("memory_used_mib"), item.get("memory_free_mib"), item.get("memory_reserved_mib")
        total = used + free + reserved if used is not None and free is not None and reserved is not None else None
        result.append(GPUStat(key=key, name=labels.get("modelName") or labels.get("model") or "GPU", memory_used_mib=used,
            memory_total_mib=total, utilization_percent=item.get("utilization_percent"), temperature_celsius=item.get("temperature_celsius"),
            power_watts=item.get("power_watts")))
    return tuple(sorted(result, key=lambda gpu: gpu.key))


class DCGMCollector(CollectorStateMixin[tuple[GPUStat, ...]]):
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
        self._last_good: dict[str, tuple[float, tuple[GPUStat, ...]]] = {}
        self._errors: dict[str, str | None] = {}
        self._retrying: dict[str, int] = {}
        self._failed_attempts: dict[str, int] = {}
        self._revisions: dict[str, int] = defaultdict(int)

    async def collect(self, now: float | Clock | None = None) -> tuple[EndpointSnapshot, ...]:
        await asyncio.gather(*(self.collect_endpoint(endpoint, now) for endpoint in self.endpoints))
        return self.snapshots(now)

    async def collect_endpoint(
        self, endpoint: EndpointConfig, now: float | Clock | None = None
    ) -> EndpointSnapshot:
        """Collect one endpoint through a complete, non-overlapping retry cycle.

        A numeric ``now`` is a deterministic fixed-time seam for the whole
        call. Production uses ``None`` and stamps the real monotonic
        completion time. Tests that need deterministic retry timing may pass
        a zero-argument advancing clock instead.
        """
        for attempt in range(self.retry_count + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.get(endpoint.url)
                if response.status_code in {408, 429} or 500 <= response.status_code <= 599:
                    self._archive(endpoint, None, f"HTTP {response.status_code}")
                    raise _RetryableHTTPStatus(response.status_code)
                if response.is_error:
                    self._archive(endpoint, None, f"HTTP {response.status_code}")
                response.raise_for_status()
                self._archive(endpoint, response.text, None)
                gpus = parse_metrics(endpoint.id, endpoint.name, response.text)
                if not gpus:
                    raise ValueError("no usable GPU data")
            except asyncio.CancelledError:
                raise
            except (httpx.TransportError, _RetryableHTTPStatus) as error:
                if isinstance(error, httpx.TransportError):
                    self._archive(endpoint, None, str(error))
                if attempt < self.retry_count:
                    # retry_attempt is one-based from the operator's point of
                    # view: it identifies the next extra attempt to be made.
                    self._record_retry_wait(endpoint.id, attempt)
                    # A prior terminal failure is a safety latch. Keep it
                    # through a later cycle's retry wait; only usable metrics
                    # may clear it. The UI still prioritizes retrying state.
                    await asyncio.sleep(self.retry_delay_seconds)
                    continue
                self._record_failure(endpoint.id, str(error), attempt + 1)
                break
            except (httpx.HTTPError, ValueError) as error:
                self._record_failure(endpoint.id, str(error), attempt + 1)
                break
            except Exception as error:  # noqa: BLE001 - isolate an endpoint supervisor failure.
                self._record_failure(
                    endpoint.id,
                    f"collector failure: {str(error) or type(error).__name__}",
                    attempt + 1,
                )
                break
            else:
                # Production/callable clocks stamp usable metrics at request
                # completion; a numeric seam intentionally remains fixed.
                self._record_success(endpoint.id, gpus, now)
                break
        return self.snapshot(endpoint, now)

    def snapshot(
        self, endpoint: EndpointConfig, now: float | Clock | None = None
    ) -> EndpointSnapshot:
        state = self._snapshot_state(endpoint.id, now)
        # A retry may use only a fresh prior sample. A terminal error always
        # wins, even when the cache has not yet aged out.
        healthy = state.prior is not None and not state.stale and state.error is None
        return EndpointSnapshot(
            endpoint.id,
            endpoint.name,
            healthy,
            state.age_seconds,
            state.stale,
            state.error,
            () if state.prior is None or state.stale else state.prior[1],
            state.sample_revision,
            state.retrying,
            state.retry_attempt,
            state.retry_count,
            state.failed_attempts,
            endpoint.memory_source,
        )

    def snapshots(self, now: float | Clock | None = None) -> tuple[EndpointSnapshot, ...]:
        """Recompute freshness between polls; controller consumers must call this every tick."""
        current = self._current_at(now)
        return tuple(self.snapshot(endpoint, current) for endpoint in self.endpoints)

    def mark_unhealthy(self, error: Exception | str) -> None:
        """Publish unexpected poll-loop failures as unsafe without discarding fresh diagnostics."""
        message = str(error) or type(error).__name__
        for endpoint in self.endpoints:
            self._record_failure(endpoint.id, f"collector failure: {message}", 0)

    def mark_endpoint_unhealthy(self, endpoint: EndpointConfig, error: Exception | str) -> None:
        """Publish an unexpected task-level failure without affecting peers."""
        message = str(error) or type(error).__name__
        self._record_failure(endpoint.id, f"collector failure: {message}", 0)

    def _archive(
        self, endpoint: EndpointConfig, text: str | None, error: str | None
    ) -> None:
        if self._archive_callback is None:
            return
        try:
            self._archive_callback(endpoint, text, error)
        except Exception:  # noqa: BLE001 - storage failure must not alter fan safety.
            # Persistent history is advisory; it must not affect endpoint
            # health, retry state, or the controller's fail-safe policy.
            return


class _RetryableHTTPStatus(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
