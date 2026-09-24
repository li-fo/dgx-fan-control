import asyncio

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


def test_parse_optional_physical_gpu_power() -> None:
    gpus = parse_metrics(
        "one", "One", METRICS + 'DCGM_FI_DEV_POWER_USAGE{UUID="GPU-a"} 321.5\n'
    )
    assert gpus[0].power_watts == 321.5


@pytest.mark.asyncio
async def test_power_only_scrape_remains_unusable_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = EndpointConfig("one", "One", "http://example/metrics")
    collector = DCGMCollector((endpoint,), 1, 5, retry_count=1, retry_delay_seconds=0)
    calls = 0

    async def power_only(self: httpx.AsyncClient, url: str) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            text='DCGM_FI_DEV_POWER_USAGE{UUID="GPU-a"} 321.5\n',
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", power_only)
    snapshot = await collector.collect_endpoint(endpoint, 10)
    assert calls == 1
    assert not snapshot.healthy
    assert snapshot.error == "no usable GPU data"
    assert snapshot.failed_attempts == 1


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


@pytest.mark.asyncio
async def test_transient_failure_retries_with_fresh_cache_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = EndpointConfig("one", "One", "http://example/metrics")
    collector = DCGMCollector((endpoint,), 1, 5, retry_count=1, retry_delay_seconds=3)
    calls = 0
    retry_waiting = asyncio.Event()
    release_retry = asyncio.Event()

    async def fake_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1 or calls == 3:
            return httpx.Response(200, text=METRICS, request=httpx.Request("GET", url))
        raise httpx.ConnectTimeout("temporary outage")

    async def fake_sleep(delay: float) -> None:
        assert delay == 3
        retry_waiting.set()
        await release_retry.wait()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr("dgx_fan.dcgm.asyncio.sleep", fake_sleep)
    await collector.collect_endpoint(endpoint, 10)
    retry = asyncio.create_task(collector.collect_endpoint(endpoint, 11))
    await retry_waiting.wait()
    waiting = collector.snapshots(12)[0]
    assert waiting.healthy and waiting.retrying
    assert waiting.retry_attempt == 1 and waiting.retry_count == 1
    assert waiting.sample_revision == 1
    release_retry.set()
    completed = await retry
    assert completed.healthy and not completed.retrying and completed.sample_revision == 2
    assert calls == 3


@pytest.mark.asyncio
async def test_transient_exhaustion_is_immediately_unhealthy_and_next_cycle_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = EndpointConfig("one", "One", "http://example/metrics")
    collector = DCGMCollector((endpoint,), 1, 20, retry_count=1, retry_delay_seconds=0)
    calls = 0

    async def fake_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1 or calls == 4:
            return httpx.Response(200, text=METRICS, request=httpx.Request("GET", url))
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    await collector.collect_endpoint(endpoint, 0)
    failed = await collector.collect_endpoint(endpoint, 1)
    assert not failed.healthy and not failed.retrying
    assert failed.gpus and failed.age_seconds == 1
    assert "offline" in (failed.error or "") and failed.sample_revision == 1
    assert failed.failed_attempts == 2
    recovered = await collector.collect_endpoint(endpoint, 2)
    assert recovered.healthy and recovered.sample_revision == 2


@pytest.mark.asyncio
async def test_terminal_failure_stays_fail_safe_through_next_retry_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = EndpointConfig("one", "One", "http://example/metrics")
    collector = DCGMCollector((endpoint,), 1, 20, retry_count=1, retry_delay_seconds=5)
    calls = 0
    retry_waiting = asyncio.Event()
    allow_retry = asyncio.Event()

    async def fake_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls in {1, 5}:
            return httpx.Response(200, text=METRICS, request=httpx.Request("GET", url))
        raise httpx.ConnectError("offline")

    async def fake_sleep(delay: float) -> None:
        assert delay == 5
        if calls == 4:
            retry_waiting.set()
            await allow_retry.wait()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr("dgx_fan.dcgm.asyncio.sleep", fake_sleep)
    await collector.collect_endpoint(endpoint, 0)
    exhausted = await collector.collect_endpoint(endpoint, 1)
    assert not exhausted.healthy and exhausted.sample_revision == 1

    retrying = asyncio.create_task(collector.collect_endpoint(endpoint, 2))
    await retry_waiting.wait()
    waiting = collector.snapshots(2)[0]
    assert waiting.retrying and not waiting.healthy and waiting.sample_revision == 1
    controller = FanController(
        ControlConfig(
            True,
            100,
            2,
            75,
            10,
            (Stage(45, 20), Stage(55, 50), Stage(70, 80), Stage(None, 100)),
            ("one", "one"),
        ),
        HardwareConfig("fake", (18, 19), 25000, True, (23, 24), (2, 2), 1, 5),
    )
    fans = (FanReading(1000, "RUNNING"), FanReading(1000, "RUNNING"))
    assert controller.update((waiting,), fans, 2).duty_percents == (100, 100)
    allow_retry.set()
    recovered = await retrying
    assert recovered.healthy and recovered.sample_revision == 2


@pytest.mark.asyncio
async def test_advancing_clock_stamps_retried_success_at_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = EndpointConfig("one", "One", "http://example/metrics")
    collector = DCGMCollector((endpoint,), 1, 20, retry_count=1, retry_delay_seconds=3)
    clock = 10.0
    calls = 0

    def now() -> float:
        return clock

    async def fake_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectTimeout("temporary")
        return httpx.Response(200, text=METRICS, request=httpx.Request("GET", url))

    async def fake_sleep(delay: float) -> None:
        nonlocal clock
        assert delay == 3
        clock = 13.0

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr("dgx_fan.dcgm.asyncio.sleep", fake_sleep)
    completed = await collector.collect_endpoint(endpoint, now)
    assert completed.healthy and completed.age_seconds == 0
    assert collector._last_good[endpoint.id][0] == 13.0
    assert collector.snapshots(now)[0].age_seconds == 0


@pytest.mark.asyncio
async def test_numeric_retry_keeps_the_deterministic_freshness_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = EndpointConfig("one", "One", "http://example/metrics")
    collector = DCGMCollector((endpoint,), 1, 5, retry_count=1, retry_delay_seconds=0)
    calls = 0

    async def fake_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectTimeout("temporary")
        return httpx.Response(200, text=METRICS, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    completed = await collector.collect_endpoint(endpoint, 10)
    assert completed.healthy and collector._last_good[endpoint.id][0] == 10
    fresh = collector.snapshots(15)[0]
    stale = collector.snapshots(15.1)[0]
    assert fresh.healthy and fresh.age_seconds == 5
    assert stale.stale and not stale.healthy and stale.age_seconds == 5.1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 401])
async def test_non_retryable_http_status_fails_once(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    endpoint = EndpointConfig("one", "One", "http://example/metrics")
    collector = DCGMCollector((endpoint,), 1, 5, retry_count=3, retry_delay_seconds=0)
    calls = 0

    async def fake_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    snapshot = await collector.collect_endpoint(endpoint, 1)
    assert calls == 1 and not snapshot.healthy and not snapshot.retrying
    assert snapshot.failed_attempts == 1


@pytest.mark.asyncio
async def test_retryable_http_status_and_unusable_metrics_have_distinct_attempt_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = EndpointConfig("one", "One", "http://example/metrics")
    collector = DCGMCollector((endpoint,), 1, 5, retry_count=1, retry_delay_seconds=0)
    calls = 0

    async def server_error(self: httpx.AsyncClient, url: str) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", server_error)
    failed = await collector.collect_endpoint(endpoint, 1)
    assert calls == 2 and failed.failed_attempts == 2

    async def empty_metrics(self: httpx.AsyncClient, url: str) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="# no GPUs\n", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", empty_metrics)
    unusable = await collector.collect_endpoint(endpoint, 2)
    assert calls == 3 and unusable.failed_attempts == 1
    assert "no usable GPU data" in (unusable.error or "")


@pytest.mark.asyncio
async def test_retry_stale_boundary_and_cancellation_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint = EndpointConfig("one", "One", "http://example/metrics")
    collector = DCGMCollector((endpoint,), 1, 5, retry_count=1, retry_delay_seconds=60)
    calls = 0
    sleeping = asyncio.Event()

    async def fake_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, text=METRICS, request=httpx.Request("GET", url))
        raise httpx.ReadTimeout("gone")

    async def fake_sleep(delay: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr("dgx_fan.dcgm.asyncio.sleep", fake_sleep)
    await collector.collect_endpoint(endpoint, 0)
    task = asyncio.create_task(collector.collect_endpoint(endpoint, 1))
    await sleeping.wait()
    stale = collector.snapshots(6.1)[0]
    assert stale.retrying and stale.stale and not stale.healthy and stale.gpus == ()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert collector.snapshots(1)[0].retrying


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


@pytest.mark.asyncio
async def test_collector_state_attributes_remain_authoritative_after_runtime_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = EndpointConfig("one", "One", "http://example/metrics")
    collector = DCGMCollector((endpoint,), 1, 5, retry_count=0, retry_delay_seconds=3)
    collector._last_good = {}
    collector._last_good[endpoint.id] = (0, parse_metrics(endpoint.id, endpoint.name, METRICS))
    collector.stale_after = 10
    assert collector.snapshots(6)[0].healthy
    collector.stale_after = 5
    assert collector.snapshots(6)[0].stale

    calls = 0

    async def fake_get(self: httpx.AsyncClient, url: str) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectTimeout("temporary")
        return httpx.Response(200, text=METRICS, request=httpx.Request("GET", url))

    async def fake_sleep(delay: float) -> None:
        assert delay == 3

    collector.retry_count = 1
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr("dgx_fan.dcgm.asyncio.sleep", fake_sleep)
    retried = await collector.collect_endpoint(endpoint, 10)
    assert calls == 2 and retried.healthy and retried.retry_count == 1
