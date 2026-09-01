"""Read-only, bounded monitor-state transport for browser companions.

The primary controller is the sole writer.  Consumers receive newline-delimited
JSON snapshots over a same-user Unix socket and cannot send commands back.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from socket import AF_UNIX, SOCK_STREAM, socket

from .models import ControlSnapshot, EndpointSnapshot, FanReading, GPUStat, MemoryStat
from .ui import DashboardHistory, HistoryPoint

SCHEMA_VERSION = 1
MAX_HISTORY_POINTS = 4096
MAX_MESSAGE_BYTES = 1_000_000


class MonitorProtocolError(ValueError):
    """A monitor message is malformed, oversized, or incompatible."""


@dataclass(frozen=True)
class MonitorState:
    source_id: str
    revision: int
    captured_at: float
    snapshot: ControlSnapshot
    history: DashboardHistory


def _memory_to_wire(memory: MemoryStat | None) -> dict[str, float] | None:
    if memory is None:
        return None
    return {"used_mib": memory.used_mib, "total_mib": memory.total_mib}


def _memory_from_wire(value: object, name: str) -> MemoryStat | None:
    if value is None:
        return None
    mapping = _mapping(value, name)
    return MemoryStat(
        _number(mapping.get("used_mib"), f"{name}.used_mib"),
        _number(mapping.get("total_mib"), f"{name}.total_mib"),
    )


def _gpu_to_wire(gpu: GPUStat) -> dict[str, object]:
    return {
        "key": gpu.key,
        "name": gpu.name,
        "memory_used_mib": gpu.memory_used_mib,
        "memory_total_mib": gpu.memory_total_mib,
        "utilization_percent": gpu.utilization_percent,
        "temperature_celsius": gpu.temperature_celsius,
    }


def _gpu_from_wire(value: object, name: str) -> GPUStat:
    mapping = _mapping(value, name)
    return GPUStat(
        _string(mapping.get("key"), f"{name}.key"),
        _string(mapping.get("name"), f"{name}.name"),
        _optional_number(mapping.get("memory_used_mib"), f"{name}.memory_used_mib"),
        _optional_number(mapping.get("memory_total_mib"), f"{name}.memory_total_mib"),
        _optional_number(mapping.get("utilization_percent"), f"{name}.utilization_percent"),
        _optional_number(mapping.get("temperature_celsius"), f"{name}.temperature_celsius"),
    )


def _endpoint_to_wire(endpoint: EndpointSnapshot) -> dict[str, object]:
    return {
        "endpoint_id": endpoint.endpoint_id,
        "name": endpoint.name,
        "healthy": endpoint.healthy,
        "age_seconds": endpoint.age_seconds,
        "stale": endpoint.stale,
        "error": endpoint.error,
        "gpus": [_gpu_to_wire(gpu) for gpu in endpoint.gpus],
        "sample_revision": endpoint.sample_revision,
        "retrying": endpoint.retrying,
        "retry_attempt": endpoint.retry_attempt,
        "retry_count": endpoint.retry_count,
        "failed_attempts": endpoint.failed_attempts,
        "memory_source": endpoint.memory_source,
        "uma_memory": _memory_to_wire(endpoint.uma_memory),
        "memory_healthy": endpoint.memory_healthy,
        "memory_age_seconds": endpoint.memory_age_seconds,
        "memory_stale": endpoint.memory_stale,
        "memory_error": endpoint.memory_error,
        "memory_sample_revision": endpoint.memory_sample_revision,
        "memory_retrying": endpoint.memory_retrying,
        "memory_retry_attempt": endpoint.memory_retry_attempt,
        "memory_retry_count": endpoint.memory_retry_count,
        "memory_failed_attempts": endpoint.memory_failed_attempts,
    }


def _endpoint_from_wire(value: object, name: str) -> EndpointSnapshot:
    mapping = _mapping(value, name)
    gpus = mapping.get("gpus")
    if not isinstance(gpus, list):
        raise MonitorProtocolError(f"{name}.gpus must be a list")
    return EndpointSnapshot(
        _string(mapping.get("endpoint_id"), f"{name}.endpoint_id"),
        _string(mapping.get("name"), f"{name}.name"),
        _bool(mapping.get("healthy"), f"{name}.healthy"),
        _optional_number(mapping.get("age_seconds"), f"{name}.age_seconds"),
        _bool(mapping.get("stale", False), f"{name}.stale"),
        _optional_string(mapping.get("error"), f"{name}.error"),
        tuple(_gpu_from_wire(gpu, f"{name}.gpus[{index}]") for index, gpu in enumerate(gpus)),
        _integer(mapping.get("sample_revision"), f"{name}.sample_revision"),
        _bool(mapping.get("retrying", False), f"{name}.retrying"),
        _integer(mapping.get("retry_attempt", 0), f"{name}.retry_attempt"),
        _integer(mapping.get("retry_count", 0), f"{name}.retry_count"),
        _integer(mapping.get("failed_attempts", 0), f"{name}.failed_attempts"),
        _string(mapping.get("memory_source", "dcgm"), f"{name}.memory_source"),
        _memory_from_wire(mapping.get("uma_memory"), f"{name}.uma_memory"),
        _bool(mapping.get("memory_healthy", True), f"{name}.memory_healthy"),
        _optional_number(mapping.get("memory_age_seconds"), f"{name}.memory_age_seconds"),
        _bool(mapping.get("memory_stale", False), f"{name}.memory_stale"),
        _optional_string(mapping.get("memory_error"), f"{name}.memory_error"),
        _integer(mapping.get("memory_sample_revision", 0), f"{name}.memory_sample_revision"),
        _bool(mapping.get("memory_retrying", False), f"{name}.memory_retrying"),
        _integer(mapping.get("memory_retry_attempt", 0), f"{name}.memory_retry_attempt"),
        _integer(mapping.get("memory_retry_count", 0), f"{name}.memory_retry_count"),
        _integer(mapping.get("memory_failed_attempts", 0), f"{name}.memory_failed_attempts"),
    )


def _snapshot_to_wire(snapshot: ControlSnapshot) -> dict[str, object]:
    return {
        "duty_percents": list(snapshot.duty_percents),
        "reason": snapshot.reason,
        "state": snapshot.state,
        "max_temperature_celsius": snapshot.max_temperature_celsius,
        "active_stages": list(snapshot.active_stages),
        "fan_temperatures_celsius": list(snapshot.fan_temperatures_celsius),
        "fan_endpoint_ids": list(snapshot.fan_endpoint_ids),
        "fans": [{"rpm": fan.rpm, "state": fan.state} for fan in snapshot.fans],
        "endpoint_snapshots": [_endpoint_to_wire(endpoint) for endpoint in snapshot.endpoint_snapshots],
    }


def _snapshot_from_wire(value: object) -> ControlSnapshot:
    mapping = _mapping(value, "snapshot")
    duty = _pair_int(mapping.get("duty_percents"), "snapshot.duty_percents")
    stages = _pair_optional_int(mapping.get("active_stages"), "snapshot.active_stages")
    temperatures = _pair_optional_number(mapping.get("fan_temperatures_celsius"), "snapshot.fan_temperatures_celsius")
    endpoint_ids = _pair_string(mapping.get("fan_endpoint_ids"), "snapshot.fan_endpoint_ids")
    fans_raw = mapping.get("fans")
    endpoints_raw = mapping.get("endpoint_snapshots")
    if not isinstance(fans_raw, list) or len(fans_raw) != 2:
        raise MonitorProtocolError("snapshot.fans must contain two entries")
    if not isinstance(endpoints_raw, list):
        raise MonitorProtocolError("snapshot.endpoint_snapshots must be a list")
    fans = tuple(
        FanReading(
            _optional_number(_mapping(fan, f"snapshot.fans[{index}]").get("rpm"), f"snapshot.fans[{index}].rpm"),
            _string(_mapping(fan, f"snapshot.fans[{index}]").get("state"), f"snapshot.fans[{index}].state"),  # type: ignore[arg-type]
        )
        for index, fan in enumerate(fans_raw)
    )
    return ControlSnapshot(
        duty,
        _string(mapping.get("reason"), "snapshot.reason"),
        _string(mapping.get("state"), "snapshot.state"),  # type: ignore[arg-type]
        _optional_number(mapping.get("max_temperature_celsius"), "snapshot.max_temperature_celsius"),
        stages,
        temperatures,
        endpoint_ids,
        fans,  # type: ignore[arg-type]
        tuple(_endpoint_from_wire(endpoint, f"snapshot.endpoint_snapshots[{index}]") for index, endpoint in enumerate(endpoints_raw)),
    )


def _history_to_wire(history: DashboardHistory) -> list[dict[str, object]]:
    series: list[dict[str, object]] = []
    point_count = 0
    for (endpoint_id, gpu_key, metric), points in sorted(history.points.items()):
        point_count += len(points)
        if point_count > MAX_HISTORY_POINTS:
            raise MonitorProtocolError("history exceeds its bounded transport limit")
        series.append(
            {
                "endpoint_id": endpoint_id,
                "gpu_key": gpu_key,
                "metric": metric,
                "points": [{"at": point.at, "value": point.value} for point in points],
            }
        )
    return series


def _history_from_wire(value: object, interval_seconds: float) -> DashboardHistory:
    if not isinstance(value, list):
        raise MonitorProtocolError("history must be a list")
    history = DashboardHistory(interval_seconds)
    point_count = 0
    for series_index, item in enumerate(value):
        series = _mapping(item, f"history[{series_index}]")
        endpoint_id = _string(series.get("endpoint_id"), f"history[{series_index}].endpoint_id")
        gpu_key = _string(series.get("gpu_key"), f"history[{series_index}].gpu_key")
        metric = _string(series.get("metric"), f"history[{series_index}].metric")
        raw_points = series.get("points")
        if not isinstance(raw_points, list):
            raise MonitorProtocolError(f"history[{series_index}].points must be a list")
        points: list[HistoryPoint] = []
        for point_index, point in enumerate(raw_points):
            point_count += 1
            if point_count > MAX_HISTORY_POINTS:
                raise MonitorProtocolError("history exceeds its bounded transport limit")
            point_map = _mapping(point, f"history[{series_index}].points[{point_index}]")
            points.append(
                HistoryPoint(
                    _number(point_map.get("at"), f"history[{series_index}].points[{point_index}].at"),
                    _number(point_map.get("value"), f"history[{series_index}].points[{point_index}].value"),
                )
            )
        history.points[(endpoint_id, gpu_key, metric)] = points
        if points and gpu_key != "__uma__":
            history.last_seen[(endpoint_id, gpu_key)] = max(point.at for point in points)
    return history


def encode_state(state: MonitorState) -> bytes:
    """Encode one full bounded replacement snapshot as UTF-8 NDJSON."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_id": state.source_id,
        "revision": state.revision,
        "captured_at": state.captured_at,
        "snapshot": _snapshot_to_wire(state.snapshot),
        "history": _history_to_wire(state.history),
    }
    encoded = (json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise MonitorProtocolError("monitor message exceeds its bounded transport limit")
    return encoded


def decode_state(encoded: bytes, collection_interval_seconds: float) -> MonitorState:
    """Validate and decode one complete monitor snapshot.

    Full replacement messages make reconnect and out-of-order handling simple:
    callers retain only a strictly newer revision.
    """
    if not encoded or len(encoded) > MAX_MESSAGE_BYTES:
        raise MonitorProtocolError("monitor message has an invalid size")
    try:
        raw = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MonitorProtocolError("monitor message is not valid JSON") from error
    mapping = _mapping(raw, "message")
    if _integer(mapping.get("schema_version"), "schema_version") != SCHEMA_VERSION:
        raise MonitorProtocolError("unsupported monitor schema_version")
    return MonitorState(
        _string(mapping.get("source_id"), "source_id"),
        _integer(mapping.get("revision"), "revision"),
        _number(mapping.get("captured_at"), "captured_at"),
        _snapshot_from_wire(mapping.get("snapshot")),
        _history_from_wire(mapping.get("history"), collection_interval_seconds),
    )


class MonitorPublisher:
    """A non-blocking fan-controller side publisher with read-only clients."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._latest: bytes | None = None
        self._revision = 0
        self._source_id = uuid.uuid4().hex
        self._clients: set[asyncio.StreamWriter] = set()

    async def start(self) -> None:
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._remove_owned_stale_socket()
        self._server = await asyncio.start_unix_server(self._serve_client, path=str(self.socket_path))
        self.socket_path.chmod(0o600)

    def publish(self, snapshot: ControlSnapshot, history: DashboardHistory, now: float) -> None:
        """Replace the latest state without awaiting or blocking control ticks."""
        self._revision += 1
        try:
            self._latest = encode_state(
                MonitorState(self._source_id, self._revision, now, snapshot, history)
            )
        except (MonitorProtocolError, ValueError):
            # A monitor serialization fault is display-only. The controller's
            # existing fan-safety path must not observe it.
            return

    async def close(self) -> None:
        for writer in tuple(self._clients):
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid():
            self.socket_path.unlink()

    def _remove_owned_stale_socket(self) -> None:
        try:
            info = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise RuntimeError("monitor socket path exists but is not an owned Unix socket")
        probe = socket(AF_UNIX, SOCK_STREAM)
        try:
            probe.settimeout(0.1)
            probe.connect(str(self.socket_path))
        except OSError:
            self.socket_path.unlink()
        else:
            raise RuntimeError("another dgx-fan monitor publisher is already active")
        finally:
            probe.close()

    async def _serve_client(
        self, _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        sent_revision = -1
        self._clients.add(writer)
        try:
            while True:
                latest = self._latest
                if latest is not None and sent_revision != self._revision:
                    writer.write(latest)
                    await writer.drain()
                    sent_revision = self._revision
                if _reader.at_eof():
                    return
                await asyncio.sleep(0.25)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._clients.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise MonitorProtocolError(f"{name} must be an object")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise MonitorProtocolError(f"{name} must be a string")
    return value


def _optional_string(value: object, name: str) -> str | None:
    return None if value is None else _string(value, name)


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise MonitorProtocolError(f"{name} must be a boolean")
    return value


def _number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise MonitorProtocolError(f"{name} must be a number")
    return float(value)


def _optional_number(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MonitorProtocolError(f"{name} must be a non-negative integer")
    return value


def _pair_int(value: object, name: str) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise MonitorProtocolError(f"{name} must contain two values")
    return (_integer(value[0], f"{name}[0]"), _integer(value[1], f"{name}[1]"))


def _pair_optional_int(value: object, name: str) -> tuple[int | None, int | None]:
    if not isinstance(value, list) or len(value) != 2:
        raise MonitorProtocolError(f"{name} must contain two values")
    return (
        None if value[0] is None else _integer(value[0], f"{name}[0]"),
        None if value[1] is None else _integer(value[1], f"{name}[1]"),
    )


def _pair_optional_number(value: object, name: str) -> tuple[float | None, float | None]:
    if not isinstance(value, list) or len(value) != 2:
        raise MonitorProtocolError(f"{name} must contain two values")
    return (
        _optional_number(value[0], f"{name}[0]"),
        _optional_number(value[1], f"{name}[1]"),
    )


def _pair_string(value: object, name: str) -> tuple[str, str]:
    if not isinstance(value, list) or len(value) != 2:
        raise MonitorProtocolError(f"{name} must contain two values")
    return (_string(value[0], f"{name}[0]"), _string(value[1], f"{name}[1]"))
