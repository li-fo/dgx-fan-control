from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict

import httpx
from prometheus_client.parser import text_string_to_metric_families

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
}


def _valid(value: float, metric: str) -> bool:
    if not math.isfinite(value) or value in _SENTINELS:
        return False
    if metric == "temperature_celsius":
        return -20 <= value <= 150
    if metric == "utilization_percent":
        return 0 <= value <= 100
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
    for key, item in values.items():
        labels = labels_by_key[key]
        used, free, reserved = item.get("memory_used_mib"), item.get("memory_free_mib"), item.get("memory_reserved_mib")
        total = used + free + reserved if used is not None and free is not None and reserved is not None else None
        result.append(GPUStat(key=key, name=labels.get("modelName") or labels.get("model") or "GPU", memory_used_mib=used,
            memory_total_mib=total, utilization_percent=item.get("utilization_percent"), temperature_celsius=item.get("temperature_celsius")))
    return tuple(sorted(result, key=lambda gpu: gpu.key))


class DCGMCollector:
    def __init__(self, endpoints: tuple[EndpointConfig, ...], timeout_seconds: float, stale_after_seconds: float) -> None:
        self.endpoints, self.timeout, self.stale_after = endpoints, timeout_seconds, stale_after_seconds
        self._last_good: dict[str, tuple[float, tuple[GPUStat, ...]]] = {}
        self._errors: dict[str, str | None] = {}

    async def collect(self, now: float | None = None) -> tuple[EndpointSnapshot, ...]:
        current = time.monotonic() if now is None else now
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            await asyncio.gather(*(self._fetch(client, endpoint, current) for endpoint in self.endpoints))
        return self.snapshots(current)

    def snapshots(self, now: float | None = None) -> tuple[EndpointSnapshot, ...]:
        """Recompute freshness between polls; controller consumers must call this every tick."""
        current = time.monotonic() if now is None else now
        snapshots: list[EndpointSnapshot] = []
        for endpoint in self.endpoints:
            prior = self._last_good.get(endpoint.id)
            age = None if prior is None else max(0.0, current - prior[0])
            stale = age is None or age > self.stale_after
            error = self._errors.get(endpoint.id)
            if prior is None and error is None:
                error = "awaiting first sample"
            snapshots.append(EndpointSnapshot(
                endpoint.id, endpoint.name, prior is not None and not stale and error is None,
                age, stale, error, () if prior is None or stale else prior[1],
            ))
        return tuple(snapshots)

    def mark_unhealthy(self, error: Exception | str) -> None:
        """Publish unexpected poll-loop failures as unsafe without discarding fresh diagnostics."""
        message = str(error) or type(error).__name__
        for endpoint in self.endpoints:
            self._errors[endpoint.id] = f"collector failure: {message}"

    async def _fetch(self, client: httpx.AsyncClient, endpoint: EndpointConfig, now: float) -> None:
        try:
            response = await client.get(endpoint.url)
            response.raise_for_status()
            gpus = parse_metrics(endpoint.id, endpoint.name, response.text)
            self._last_good[endpoint.id] = (now, gpus)
            self._errors[endpoint.id] = None
        except (httpx.HTTPError, ValueError) as error:
            self._errors[endpoint.id] = str(error)
