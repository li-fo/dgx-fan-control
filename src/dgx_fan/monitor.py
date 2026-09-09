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

from .config import AppConfig, DashboardColors
from .models import ControlSnapshot, EndpointSnapshot, FanReading, GPUStat, MemoryStat
from .ui import DashboardHistory, HistoryPoint

SCHEMA_VERSION = 2
# Two endpoints with eight GPUs each produce at most 16 * 3 chart series.  At
# the supported 0.1 second collection interval that is 57,600 points over the
# dashboard's 120 second window.  Leave room for the two UMA-memory series.
MAX_HISTORY_POINTS = 65_536
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
# Publishing each control tick would be wasteful at sub-second collection
# rates.  A one-second replacement frame still carries the complete history
# and every visible state field.
MIN_PUBLISH_INTERVAL_SECONDS = 1.0


class MonitorProtocolError(ValueError):
    """A monitor message is malformed, oversized, or incompatible."""


@dataclass(frozen=True)
class MonitorState:
    source_id: str
    revision: int
    captured_at: float
    snapshot: ControlSnapshot | None
    history: DashboardHistory | None
    transport_error: str | None = None
    settings_source_id: str | None = None
    settings_revision: int | None = None
    collection_interval_seconds: float | None = None
    emergency_temperature_celsius: float | None = None
    dashboard_colors: DashboardColors | None = None
    power_enabled: bool | None = None
    control_available: bool | None = None


@dataclass(frozen=True)
class _MonitorInput:
    """An immutable controller-state hand-off to the serializer worker."""

    generation: int
    snapshot: ControlSnapshot
    captured_at: float


@dataclass(frozen=True)
class _MonitorFrame:
    """One complete wire frame whose identity and bytes are inseparable."""

    generation: int
    revision: int
    encoded: bytes


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
                # Compact pairs keep a maximum supported 120-second history
                # comfortably below the byte bound without altering chart
                # samples or aggregation semantics.
                "points": [[point.at, point.value] for point in points],
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
            if not isinstance(point, list) or len(point) != 2:
                raise MonitorProtocolError(
                    f"history[{series_index}].points[{point_index}] must contain two values"
                )
            points.append(
                HistoryPoint(
                    _number(point[0], f"history[{series_index}].points[{point_index}][0]"),
                    _number(point[1], f"history[{series_index}].points[{point_index}][1]"),
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
        "snapshot": None if state.snapshot is None else _snapshot_to_wire(state.snapshot),
        "history": None if state.history is None else _history_to_wire(state.history),
        "transport_error": state.transport_error,
        "settings_source_id": state.settings_source_id,
        "settings_revision": state.settings_revision,
        "collection_interval_seconds": state.collection_interval_seconds,
        "emergency_temperature_celsius": state.emergency_temperature_celsius,
        "dashboard_colors": (
            None
            if state.dashboard_colors is None
            else {
                "memory": state.dashboard_colors.memory,
                "utilization": state.dashboard_colors.utilization,
                "temperature": state.dashboard_colors.temperature,
                "power": state.dashboard_colors.power,
            }
        ),
        "power_enabled": state.power_enabled,
        "control_available": state.control_available,
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
    transport_error = _optional_string(mapping.get("transport_error"), "transport_error")
    snapshot_raw = mapping.get("snapshot")
    history_raw = mapping.get("history")
    if transport_error is None and (snapshot_raw is None or history_raw is None):
        raise MonitorProtocolError("healthy monitor message requires snapshot and history")
    if transport_error is not None and (snapshot_raw is not None or history_raw is not None):
        raise MonitorProtocolError("monitor error message cannot include snapshot or history")
    colors_raw = mapping.get("dashboard_colors")
    colors: DashboardColors | None = None
    if colors_raw is not None:
        colors_mapping = _mapping(colors_raw, "dashboard_colors")
        colors = DashboardColors(
            _optional_string(colors_mapping.get("memory"), "dashboard_colors.memory"),
            _optional_string(colors_mapping.get("utilization"), "dashboard_colors.utilization"),
            _optional_string(colors_mapping.get("temperature"), "dashboard_colors.temperature"),
            _optional_string(colors_mapping.get("power", "ansi_green"), "dashboard_colors.power"),
        )
    settings_revision_raw = mapping.get("settings_revision")
    settings_revision = (
        None
        if settings_revision_raw is None
        else _integer(settings_revision_raw, "settings_revision")
    )
    effective_interval_raw = mapping.get("collection_interval_seconds")
    effective_interval = (
        None
        if effective_interval_raw is None
        else _number(effective_interval_raw, "collection_interval_seconds")
    )
    emergency_raw = mapping.get("emergency_temperature_celsius")
    emergency = (
        None
        if emergency_raw is None
        else _number(emergency_raw, "emergency_temperature_celsius")
    )
    power_raw = mapping.get("power_enabled")
    power = None if power_raw is None else _bool(power_raw, "power_enabled")
    control_available_raw = mapping.get("control_available")
    control_available = (
        None
        if control_available_raw is None
        else _bool(control_available_raw, "control_available")
    )
    return MonitorState(
        _string(mapping.get("source_id"), "source_id"),
        _integer(mapping.get("revision"), "revision"),
        _number(mapping.get("captured_at"), "captured_at"),
        None if snapshot_raw is None else _snapshot_from_wire(snapshot_raw),
        None if history_raw is None else _history_from_wire(history_raw, collection_interval_seconds),
        transport_error,
        _optional_string(mapping.get("settings_source_id"), "settings_source_id"),
        settings_revision,
        effective_interval,
        emergency,
        colors,
        power,
        control_available,
    )


class MonitorPublisher:
    """A non-blocking fan-controller side publisher with read-only clients."""

    def __init__(self, socket_path: Path, collection_interval_seconds: float = MIN_PUBLISH_INTERVAL_SECONDS) -> None:
        self.socket_path = socket_path
        self._server: asyncio.AbstractServer | None = None
        self._frame: _MonitorFrame | None = None
        self._next_revision = 0
        self._accepted_generation = 0
        self._processed_generation = 0
        self._source_id = uuid.uuid4().hex
        self._clients: set[asyncio.StreamWriter] = set()
        self._publish_interval_seconds = max(MIN_PUBLISH_INTERVAL_SECONDS, collection_interval_seconds)
        self._next_publish_at = float("-inf")
        self._history = DashboardHistory(collection_interval_seconds)
        self._pending: _MonitorInput | None = None
        self._latest_input: _MonitorInput | None = None
        self._work_event = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._closing = False
        self._settings_source_id: str | None = None
        self._settings_revision: int | None = None
        self._emergency_temperature_celsius: float | None = None
        self._dashboard_colors: DashboardColors | None = None
        self._power_enabled: bool | None = None
        self._control_available: bool | None = None

    def reconfigure(
        self,
        config: AppConfig,
        settings_source_id: str,
        settings_revision: int,
        power_enabled: bool,
        control_available: bool,
    ) -> None:
        """Apply controller-authoritative presentation and cadence metadata."""
        interval = config.collection.interval_seconds
        self._publish_interval_seconds = max(MIN_PUBLISH_INTERVAL_SECONDS, interval)
        self._history.collection_interval_seconds = interval
        self._settings_source_id = settings_source_id
        self._settings_revision = settings_revision
        self._emergency_temperature_celsius = config.control.emergency_temperature_celsius
        self._dashboard_colors = config.dashboard_colors
        self._power_enabled = power_enabled
        self._control_available = control_available
        # Make the accepted effective state observable without waiting out the
        # previous cadence. The next control tick still supplies fan telemetry.
        self._next_publish_at = float("-inf")

    async def start(self) -> None:
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._remove_owned_stale_socket()
        self._server = await asyncio.start_unix_server(self._serve_client, path=str(self.socket_path))
        self.socket_path.chmod(0o600)
        self._worker = asyncio.create_task(self._run_worker())

    def publish(self, snapshot: ControlSnapshot, now: float) -> bool:
        """Coalesce immutable state for the private off-loop serializer."""
        if now < self._next_publish_at:
            return False
        self._next_publish_at = now + self._publish_interval_seconds
        self._accepted_generation += 1
        item = _MonitorInput(self._accepted_generation, snapshot, now)
        self._latest_input = item
        self._pending = item
        self._work_event.set()
        return True

    async def _run_worker(self) -> None:
        """Build bounded history and JSON outside the control event-loop path."""
        while True:
            await self._work_event.wait()
            self._work_event.clear()
            if self._closing:
                return
            item = self._pending
            self._pending = None
            if item is None:
                continue
            if (
                item.generation <= self._processed_generation
                and self._frame is not None
                and self._frame.generation >= item.generation
            ):
                # A reconnect may request hydration while this same
                # generation is encoding. Once a frame exists, do not let
                # that queued duplicate advance the public revision again.
                # A history-only generation still needs its first hydration.
                continue
            # Revisions belong to the event loop.  The worker receives the
            # proposed revision as immutable input and cannot advance it.
            proposed_revision = self._next_revision + 1
            # With no viewers retain only history; JSON is deferred until a
            # client requests hydration.  This makes idle publication cheap.
            encode = bool(self._clients)
            result = await asyncio.to_thread(self._build_frame, item, proposed_revision, encode)
            self._processed_generation = max(self._processed_generation, item.generation)
            # The event loop installs each completed candidate as one atomic
            # identity/bytes pair.  Do not drop a completed older candidate
            # merely because another input arrived while it encoded: doing so
            # can starve all delivery when encoding is slower than cadence.
            # New clients apply their connection-generation barrier below.
            if result is not None:
                self._next_revision = result.revision
                self._frame = result

    def _build_frame(
        self, item: _MonitorInput, revision: int, encode: bool
    ) -> _MonitorFrame | None:
        """Build a candidate using publisher-private history off the event loop."""
        for endpoint in item.snapshot.endpoint_snapshots:
            self._history.append(
                endpoint.endpoint_id,
                endpoint.sample_revision,
                endpoint.gpus,
                item.captured_at,
                endpoint.memory_source,
                endpoint.uma_memory,
                endpoint.memory_sample_revision,
            )
        if not encode:
            return None
        try:
            encoded = encode_state(
                MonitorState(
                    self._source_id,
                    revision,
                    item.captured_at,
                    item.snapshot,
                    self._history,
                    settings_source_id=self._settings_source_id,
                    settings_revision=self._settings_revision,
                    collection_interval_seconds=self._history.collection_interval_seconds,
                    emergency_temperature_celsius=self._emergency_temperature_celsius,
                    dashboard_colors=self._dashboard_colors,
                    power_enabled=self._power_enabled,
                    control_available=self._control_available,
                )
            )
        except (MonitorProtocolError, ValueError) as error:
            encoded = encode_state(
                MonitorState(
                    self._source_id,
                    revision,
                    item.captured_at,
                    None,
                    None,
                    f"PUBLISH ERROR: {type(error).__name__}",
                )
            )
        return _MonitorFrame(item.generation, revision, encoded)

    async def close(self) -> None:
        for writer in tuple(self._clients):
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._closing = True
        self._work_event.set()
        if self._worker is not None:
            await self._worker
            self._worker = None
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
        # A client that reconnects after a newer state was accepted must not
        # hydrate the previous wire frame while the newest one is encoding.
        required_generation = self._accepted_generation
        sent_generation = required_generation - 1
        self._clients.add(writer)
        self._request_hydration()
        try:
            while True:
                frame = self._frame
                if (
                    frame is not None
                    and frame.generation >= required_generation
                    and frame.generation > sent_generation
                ):
                    writer.write(frame.encoded)
                    await writer.drain()
                    sent_generation = frame.generation
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

    def _request_hydration(self) -> None:
        """Request the newest state without replacing newer queued input."""
        item = self._latest_input
        if item is None:
            return
        frame = self._frame
        if frame is not None and frame.generation >= item.generation:
            return
        pending = self._pending
        if pending is None or pending.generation <= item.generation:
            self._pending = item
        self._work_event.set()


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
