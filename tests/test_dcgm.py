import httpx
import pytest

from dgx_fan.config import EndpointConfig
from dgx_fan.dcgm import DCGMCollector, parse_metrics

METRICS = '''
DCGM_FI_DEV_GPU_TEMP{UUID="GPU-a",modelName="A100"} 55
DCGM_FI_DEV_GPU_UTIL{uuid="GPU-a"} 88
DCGM_FI_DEV_FB_USED{UUID="GPU-a"} 100
DCGM_FI_DEV_FB_FREE{UUID="GPU-a"} 200
DCGM_FI_DEV_FB_RESERVED{UUID="GPU-a"} 10
DCGM_FI_DEV_GPU_TEMP{UUID="GPU-mig",GPU_I_ID="1"} 90
DCGM_FI_DEV_GPU_TEMP{UUID="GPU-b"} -9223372036854775808
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
    assert (await collector.collect(10))[0].healthy
    assert not (await collector.collect(11))[0].healthy
    assert not (await collector.collect(13))[0].healthy
