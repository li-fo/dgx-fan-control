from __future__ import annotations

import asyncio
import os
import stat

import pytest

from dgx_fan.models import ControlSnapshot, EndpointSnapshot, FanReading, GPUStat
from dgx_fan.monitor import (
    MonitorProtocolError,
    MonitorPublisher,
    MonitorState,
    decode_state,
    encode_state,
)
from dgx_fan.ui import DashboardHistory


def _state(revision: int = 1) -> MonitorState:
    endpoint = EndpointSnapshot(
        "dgx-1",
        "DGX-1",
        True,
        0.1,
        gpus=(GPUStat("gpu-0", "GPU 0", 100, 1000, 40, 55),),
        sample_revision=revision,
    )
    snapshot = ControlSnapshot(
        (20, 40),
        "temperature curve",
        "AUTO ON",
        55,
        (0, 1),
        (50, 55),
        ("dgx-1", "dgx-1"),
        (FanReading(1000, "RUNNING"), FanReading(1100, "RUNNING")),
        (endpoint,),
    )
    history = DashboardHistory(2)
    history.append("dgx-1", revision, endpoint.gpus, 100.0)
    return MonitorState("test-source", revision, 100.0, snapshot, history)


def test_monitor_state_round_trip_preserves_chart_history() -> None:
    state = _state()
    decoded = decode_state(encode_state(state), 2)
    assert decoded.revision == 1
    assert decoded.source_id == "test-source"
    assert decoded.snapshot.duty_percents == (20, 40)
    assert decoded.snapshot.endpoint_snapshots[0].gpus[0].temperature_celsius == 55
    assert decoded.history.last_seen == {("dgx-1", "gpu-0"): 100.0}
    assert decoded.history.area("dgx-1", "gpu-0", "mem", 100, 8, 100)


@pytest.mark.parametrize(
    "payload",
    [b"", b"not-json", b'{"schema_version":99,"revision":1}'],
)
def test_monitor_protocol_rejects_malformed_or_incompatible_messages(payload: bytes) -> None:
    with pytest.raises(MonitorProtocolError):
        decode_state(payload, 2)


def test_monitor_publisher_is_read_only_bounded_and_cleans_up(tmp_path) -> None:
    async def exercise() -> None:
        path = tmp_path / "monitor.sock"
        publisher = MonitorPublisher(path)
        await publisher.start()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        publisher.publish(_state().snapshot, _state().history, 100)
        reader, writer = await asyncio.open_unix_connection(str(path))
        message = await asyncio.wait_for(reader.readline(), timeout=2)
        assert decode_state(message, 2).revision == 1
        writer.close()
        await writer.wait_closed()
        await publisher.close()
        assert not path.exists()

    asyncio.run(exercise())


def test_monitor_publisher_rejects_non_socket_path(tmp_path) -> None:
    path = tmp_path / "monitor.sock"
    path.write_text("not a socket")

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="owned Unix socket"):
            await MonitorPublisher(path).start()

    asyncio.run(exercise())
    assert path.read_text() == "not a socket"
    assert os.path.isfile(path)
