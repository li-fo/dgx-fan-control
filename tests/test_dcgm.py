import httpx
import pytest

from dgx_fan.config import ControlConfig, EndpointConfig, HardwareConfig
from dgx_fan.controller import FanController
from dgx_fan.dcgm import DCGMCollector, parse_metrics
from dgx_fan.models import FanReading, Stage

METRICS = '''
DCGM_FI_DEV_GPU_TEMP{UUID="GPU-a",modelName="A100"} 55
DCGM_FI_DEV_GPU_UTIL{uuid="GPU-a"} 88
DCGM_FI_DEV_FB_USED{UUID="GPU-a"} 100
DCGM_FI_DEV_FB_FREE{UUID="GPU-a"} 200
DCGM_FI_DEV_FB_RESERVED{UUID="GPU-a"} 10
DCGM_FI_DEV_GPU_TEMP{UUID="GPU-mig",GPU_I_ID="1"} 90
DCGM_FI_DEV_GPU_TEMP{UUID="GPU-b"} -9223372036854775808
DCGM_FI_DEV_GPU_UTIL{UUID="GPU-c"} 9223372036854775807
DCGM_FI_DEV_FB_USED{UUID="GPU-c"} 9223372036854775807
'''


def test_parse_uuid_mig_and_sentinel() -> None:
    gpus = parse_metrics("one", "One", METRICS)
    assert len(gpus) == 1
    assert gpus[0].key == "GPU-a"
    assert gpus[0].memory_total_mib == 310
    assert gpus[0].temperature_celsius == 55


@pytest.mark.asyncio
async def test_collector_retains_fresh_sample_then_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = EndpointConfig("one", "One", "http://example/metrics")
    collector = DCGMCollector((endpoint,), 1, 2)
    calls = 0

    async def fake_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, text=METRICS, request=httpx.Request("GET", url))
        raise httpx.ConnectTimeout("nope")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    first = (await collector.collect(10))[0]
    assert first.healthy and first.sample_revision == 1
    assert collector.snapshots(10.25)[0].sample_revision == 1
    assert not (await collector.collect(11))[0].healthy
    assert not (await collector.collect(13))[0].healthy
    assert (await collector.collect(13))[0].stale


def test_parse_rejects_positive_sentinels_and_out_of_range_util() -> None:
    gpus = parse_metrics("one", "One", METRICS + 'DCGM_FI_DEV_GPU_UTIL{UUID="GPU-d"} 101\n')
    assert all(gpu.key not in {"GPU-c", "GPU-d"} for gpu in gpus)


@pytest.mark.asyncio
async def test_snapshots_expire_between_polls_and_force_control_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = EndpointConfig("one", "One", "http://example/metrics")
    collector = DCGMCollector((endpoint,), timeout_seconds=60, stale_after_seconds=5)

    async def good_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        return httpx.Response(200, text=METRICS, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", good_get)
    await collector.collect(0)
    assert collector.snapshots(4.9)[0].healthy
    stale = collector.snapshots(5.1)
    assert stale[0].stale and not stale[0].healthy and stale[0].gpus == ()
    controller = FanController(
        ControlConfig(True, 100, 2, 75, 10, (Stage(45, 20), Stage(55, 50), Stage(70, 80), Stage(None, 100)), ("one", "one")),
        HardwareConfig("fake", (18, 19), 25000, True, (23, 24), (2, 2), 1, 5),
    )
    fans = (FanReading(1000, "RUNNING"), FanReading(1000, "RUNNING"))
    assert controller.update(stale, fans, 5.1).duty_percents == (100, 100)


def test_unexpected_collector_failure_publishes_unhealthy_snapshot() -> None:
    collector = DCGMCollector((EndpointConfig("one", "One", "http://example/metrics"),), 60, 5)
    collector.mark_unhealthy(RuntimeError("unexpected poll failure"))
    snapshot = collector.snapshots(1)
    assert not snapshot[0].healthy
    assert "collector failure" in (snapshot[0].error or "")
