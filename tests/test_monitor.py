from __future__ import annotations

import asyncio
import os
import stat

import pytest

from dgx_fan.models import ControlSnapshot, EndpointSnapshot, FanReading, GPUStat
from dgx_fan.monitor import (
    MAX_HISTORY_POINTS,
    MonitorProtocolError,
    MonitorPublisher,
    MonitorState,
    decode_state,
    encode_state,
)
from dgx_fan.ui import DashboardHistory, HistoryPoint


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
    assert decoded.snapshot is not None and decoded.history is not None
    assert decoded.snapshot.duty_percents == (20, 40)
    assert decoded.snapshot.endpoint_snapshots[0].gpus[0].temperature_celsius == 55
    assert decoded.history.last_seen == {("dgx-1", "gpu-0"): 100.0}
    assert decoded.history.area("dgx-1", "gpu-0", "mem", 100, 8, 100)


def test_monitor_state_supports_full_two_dgx_eight_gpu_minimum_interval_history() -> None:
    """The transport covers 120 seconds at 0.1s for the documented topology."""
    state = _state()
    assert state.history is not None
    history = DashboardHistory(0.1)
    for endpoint_index in range(2):
        for gpu_index in range(8):
            for metric in ("mem", "util", "temp"):
                history.points[(f"dgx-{endpoint_index}", f"gpu-{gpu_index}", metric)] = [
                    HistoryPoint(sample / 10, float(sample % 101)) for sample in range(1200)
                ]
    dense = MonitorState("dense", 1, 120.0, state.snapshot, history)
    encoded = encode_state(dense)
    decoded = decode_state(encoded, 0.1)
    assert len(encoded) < 8 * 1024 * 1024
    assert decoded.history is not None
    assert sum(len(points) for points in decoded.history.points.values()) == 57_600


def test_monitor_state_supports_full_two_dgx_eight_gpu_one_second_history() -> None:
    state = _state()
    assert state.history is not None
    history = DashboardHistory(1.0)
    for endpoint_index in range(2):
        for gpu_index in range(8):
            for metric in ("mem", "util", "temp"):
                history.points[(f"dgx-{endpoint_index}", f"gpu-{gpu_index}", metric)] = [
                    HistoryPoint(float(sample), float(sample % 101)) for sample in range(120)
                ]
    decoded = decode_state(encode_state(MonitorState("dense", 1, 120.0, state.snapshot, history)), 1)
    assert decoded.history is not None
    assert sum(len(points) for points in decoded.history.points.values()) == 5_760
    assert MAX_HISTORY_POINTS >= 60_000


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


def test_monitor_publisher_replaces_encode_fault_with_observable_error_and_retries(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        path = tmp_path / "monitor.sock"
        publisher = MonitorPublisher(path)
        await publisher.start()
        try:
            import dgx_fan.monitor as monitor_module

            monkeypatch.setattr(
                monitor_module,
                "_history_to_wire",
                lambda _history: (_ for _ in ()).throw(MonitorProtocolError("too large")),
            )
            assert publisher.publish(_state().snapshot, _state().history, 100) is True
            reader, writer = await asyncio.open_unix_connection(str(path))
            error_state = decode_state(await asyncio.wait_for(reader.readline(), timeout=2), 2)
            assert error_state.transport_error == "PUBLISH ERROR: MonitorProtocolError"
            assert error_state.snapshot is None
            writer.close()
            await writer.wait_closed()
            monkeypatch.undo()
            assert publisher.publish(_state(2).snapshot, _state(2).history, 101) is True
            reader, writer = await asyncio.open_unix_connection(str(path))
            recovered = decode_state(await asyncio.wait_for(reader.readline(), timeout=2), 2)
            assert recovered.revision == 2
            assert recovered.transport_error is None
            writer.close()
            await writer.wait_closed()
        finally:
            await publisher.close()

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


def test_monitor_publisher_bounds_subsecond_publish_cadence(tmp_path) -> None:
    publisher = MonitorPublisher(tmp_path / "monitor.sock", collection_interval_seconds=0.1)
    state = _state()
    assert publisher.publish(state.snapshot, state.history, 10.0) is True
    assert publisher.publish(state.snapshot, state.history, 10.5) is False
    assert publisher.publish(state.snapshot, state.history, 11.0) is True
