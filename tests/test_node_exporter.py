import asyncio

import httpx
import pytest

from dgx_fan.config import EndpointConfig
from dgx_fan.node_exporter import NodeExporterCollector, parse_memory_metrics

METRICS = """
node_memory_MemTotal_bytes 130596098048
node_memory_MemAvailable_bytes 125114916864
"""


def test_parse_memory_metrics_uses_physical_memavailable_without_swap() -> None:
    memory = parse_memory_metrics(METRICS + "node_memory_SwapFree_bytes 999999999999\n")
    assert memory.total_mib == 124546.14453125
    assert memory.used_mib == 5227.26171875


@pytest.mark.parametrize(
    "metrics",
    [
        "node_memory_MemTotal_bytes 1\n",
        "node_memory_MemTotal_bytes 1\nnode_memory_MemAvailable_bytes 2\n",
        "node_memory_MemTotal_bytes 0\nnode_memory_MemAvailable_bytes 0\n",
        "node_memory_MemTotal_bytes -1\nnode_memory_MemAvailable_bytes 0\n",
        "node_memory_MemTotal_bytes NaN\nnode_memory_MemAvailable_bytes 0\n",
        "node_memory_MemTotal_bytes{node=\"one\"} 1\nnode_memory_MemAvailable_bytes{node=\"one\"} 0\n",
        "node_memory_MemTotal_bytes 2\nnode_memory_MemTotal_bytes{node=\"one\"} 2\nnode_memory_MemAvailable_bytes 1\n",
        "node_memory_MemTotal_bytes 2\nnode_memory_MemTotal_bytes 2\nnode_memory_MemAvailable_bytes 1\n",
        "node_memory_MemTotal_bytes 9223372036854775807\nnode_memory_MemAvailable_bytes 1\n",
    ],
)
def test_parse_memory_metrics_rejects_missing_ambiguous_or_invalid_values(metrics: str) -> None:
    with pytest.raises(ValueError):
        parse_memory_metrics(metrics)


@pytest.mark.asyncio
async def test_node_collector_retries_without_changing_its_cached_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = EndpointConfig("one", "One", "http://dcgm/metrics", "node-exporter", "http://node/metrics")
    collector = NodeExporterCollector((endpoint,), 1, 5, retry_count=1, retry_delay_seconds=3)
    calls = 0

    async def fake_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, text=METRICS, request=httpx.Request("GET", url))
        if calls == 2:
            raise httpx.ConnectTimeout("temporary")
        return httpx.Response(200, text=METRICS, request=httpx.Request("GET", url))

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr("dgx_fan.node_exporter.asyncio.sleep", fake_sleep)
    first = await collector.collect_endpoint(endpoint, 0)
    second = await collector.collect_endpoint(endpoint, 1)
    assert first.healthy and second.healthy and second.sample_revision == 2
    assert sleeps == [3] and calls == 3


@pytest.mark.asyncio
async def test_node_collector_stale_failure_and_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = EndpointConfig("one", "One", "http://dcgm/metrics", "node-exporter", "http://node/metrics")
    collector = NodeExporterCollector((endpoint,), 1, 5, retry_count=1, retry_delay_seconds=60)

    async def good_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        return httpx.Response(200, text=METRICS, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", good_get)
    await collector.collect_endpoint(endpoint, 0)
    assert collector.snapshots(5.1)[0].stale

    async def timeout_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        raise httpx.ConnectTimeout("offline")

    sleeping = asyncio.Event()

    async def wait_forever(delay: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(httpx.AsyncClient, "get", timeout_get)
    monkeypatch.setattr("dgx_fan.node_exporter.asyncio.sleep", wait_forever)
    task = asyncio.create_task(collector.collect_endpoint(endpoint, 1))
    await sleeping.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
