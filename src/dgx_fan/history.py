"""Bounded, persistent metric history for the optional History dashboard.

The history writer deliberately consumes the original Prometheus response rather
than the compact live ``GPUStat`` snapshots.  The latter discard MIG labels and
unrelated DCGM fields, which makes them unsuitable as an archive format.
"""

from __future__ import annotations

import asyncio
import math
import queue
import sqlite3
import threading
import time
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from prometheus_client.parser import text_string_to_metric_families

from .config import EndpointConfig

_RETENTION_SECONDS: Final = 8 * 24 * 60 * 60
_SCHEMA_VERSION: Final = 1
_MAX_QUEUE_ITEMS: Final = 128
_MAX_ITEM_BYTES: Final = 2 * 1024 * 1024
_PRUNE_INTERVAL_SECONDS: Final = 60.0
_PRUNE_BATCH_SIZE: Final = 1_000
_QUERY_SQL_TIMEOUT_SECONDS: Final = 15.0
_SENTINELS: Final = {
    -9223372036854775808,
    -9223372036854775807,
    9223372036854775807,
    9223372036854775808,
}
_SIGNED_64_MAX: Final = 9223372036854775807
_STOP: Final = object()


@dataclass(frozen=True)
class _Submission:
    endpoint_id: str
    source: str
    text: str | None
    captured_at: float
    interval_seconds: float
    error: str | None


@dataclass(frozen=True)
class _Point:
    memory: float | None
    utilization: float | None
    temperature: float | None
    power: float | None


class HistoryService:
    """Asynchronously archive exporter responses and query bounded chart data.

    ``submit`` only places a bounded item on an in-process queue.  SQLite and
    Prometheus parsing stay in the writer thread so a slow disk never delays
    the fan control event loop.
    """

    def __init__(self, config_path: Path, endpoints: tuple[EndpointConfig, ...]) -> None:
        self.db_path = config_path.absolute().parent / "data" / "history.sqlite3"
        self._endpoints = {endpoint.id: endpoint for endpoint in endpoints}
        self._queue: queue.Queue[_Submission | object] = queue.Queue(maxsize=_MAX_QUEUE_ITEMS)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = False
        self._status = ""
        self._status_lock = threading.Lock()
        self._read_slots = threading.BoundedSemaphore(4)
        self._last_prune = 0.0
        self._prune_pending = False

    async def start(self) -> None:
        """Start the single archive writer once; failure remains non-fatal."""
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._writer_main, name="dgx-fan-history", daemon=True)
        self._thread.start()

    async def close(self) -> None:
        """Stop the writer and wait for its SQLite connection to close."""
        if not self._started:
            return
        # Let the writer flush already accepted samples, but do not make app
        # shutdown wait indefinitely for a broken filesystem.
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            # Preserve accepted work, then let the writer leave once it has
            # drained the full queue instead of blocking shutdown on another
            # sentinel insertion.
            self._stop.set()
        thread = self._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, 5.0)
            if thread.is_alive():
                self._set_status("history writer did not stop before timeout")
                return
        self._thread = None
        self._started = False

    def submit(
        self,
        endpoint_id: str,
        source: str,
        text: str | None,
        captured_at: float,
        interval_seconds: float,
        error: str | None = None,
    ) -> None:
        """Queue one *actual* response or failure without waiting for SQLite."""
        self._validate_submission(endpoint_id, source, text, captured_at, interval_seconds, error)
        if not self._started:
            self._set_status("history writer is not running")
            return
        if text is not None and len(text.encode("utf-8", errors="replace")) > _MAX_ITEM_BYTES:
            self._set_status("history response dropped: item exceeds size limit")
            return
        try:
            self._queue.put_nowait(_Submission(endpoint_id, source, text, captured_at, interval_seconds, error))
        except queue.Full:
            self._set_status("history response dropped: writer queue is full")

    async def query(self, endpoint_id: str, start: float, end: float, width: int) -> dict[str, object]:
        """Return fixed-width, UTC epoch chart columns without holding the event loop."""
        self._validate_query(endpoint_id, start, end, width)
        now = time.time()
        retention_start = now - _RETENTION_SECONDS
        bounded_start = max(start, retention_start)
        if end <= retention_start:
            return self._empty_result(endpoint_id, start, end, now, retention_start, width, self._warning())
        if not self._read_slots.acquire(blocking=False):
            return self._empty_result(endpoint_id, start, end, now, retention_start, width, "history read busy; retry")
        task = asyncio.create_task(
            asyncio.to_thread(
                self._query_with_release,
                endpoint_id,
                bounded_start,
                min(end, now),
                width,
                start,
                end,
                now,
                retention_start,
            )
        )
        task.add_done_callback(_consume_task_exception)
        # A browser pan can cancel its obsolete await.  Shield keeps the
        # SQLite work and its semaphore reservation alive until it closes.
        return await asyncio.shield(task)

    def _validate_submission(
        self,
        endpoint_id: str,
        source: str,
        text: str | None,
        captured_at: float,
        interval_seconds: float,
        error: str | None,
    ) -> None:
        if endpoint_id not in self._endpoints:
            raise ValueError("unknown history endpoint")
        if source not in {"dcgm", "node"}:
            raise ValueError("history source must be dcgm or node")
        if text is not None and not isinstance(text, str):
            raise ValueError("history text must be a string or None")
        if isinstance(captured_at, bool) or not isinstance(captured_at, (int, float)) or not math.isfinite(captured_at):
            raise ValueError("history captured_at must be finite epoch UTC")
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or interval_seconds <= 0
        ):
            raise ValueError("history interval_seconds must be finite and positive")
        if error is not None and not isinstance(error, str):
            raise ValueError("history error must be a string or None")

    def _validate_query(self, endpoint_id: str, start: float, end: float, width: int) -> None:
        if endpoint_id not in self._endpoints:
            raise ValueError("unknown history endpoint")
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            for value in (start, end)
        ):
            raise ValueError("history range must use finite epoch UTC timestamps")
        if end <= start or end - start > _RETENTION_SECONDS:
            raise ValueError("history range must be positive and no longer than eight days")
        if not isinstance(width, int) or isinstance(width, bool) or not 1 <= width <= 1024:
            raise ValueError("history width must be an integer from 1 through 1024")

    def _writer_main(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.db_path, timeout=2.0)
            self._initialize(connection)
            self._prune_pending = self._prune(connection, time.time())
            while True:
                try:
                    item = self._queue.get(timeout=0.5)
                except queue.Empty:
                    if self._stop.is_set():
                        break
                    item = None
                if item is _STOP:
                    break
                if item is not None:
                    assert isinstance(item, _Submission)
                    self._write_submission(connection, item)
                if self._stop.is_set() and self._queue.empty():
                    break
                now = time.time()
                if self._prune_pending or now - self._last_prune >= _PRUNE_INTERVAL_SECONDS:
                    self._prune_pending = self._prune(connection, now)
        except Exception as error:  # noqa: BLE001 - archival failure must never affect fan control.
            self._set_status(f"history storage unavailable: {str(error) or type(error).__name__}")
        finally:
            if connection is not None:
                connection.close()

    def _initialize(self, connection: sqlite3.Connection) -> None:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "history_meta" in tables:
            row = connection.execute("SELECT value FROM history_meta WHERE key='schema_version'").fetchone()
            if row is None or row[0] != str(_SCHEMA_VERSION):
                raise RuntimeError("unsupported history database schema; archive left untouched")
            return
        if tables:
            raise RuntimeError("unknown history database schema; archive left untouched")
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE history_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO history_meta(key, value) VALUES ('schema_version', '1');
            CREATE TABLE history_scrapes (
                id INTEGER PRIMARY KEY,
                endpoint_id TEXT NOT NULL,
                source TEXT NOT NULL,
                captured_at REAL NOT NULL,
                interval_seconds REAL NOT NULL,
                error TEXT,
                raw_zlib BLOB
            );
            CREATE INDEX history_scrapes_time ON history_scrapes(endpoint_id, captured_at);
            CREATE INDEX history_scrapes_captured ON history_scrapes(captured_at);
            CREATE TABLE history_points (
                endpoint_id TEXT NOT NULL,
                source TEXT NOT NULL,
                captured_at REAL NOT NULL,
                memory REAL,
                utilization REAL,
                temperature REAL,
                power REAL,
                PRIMARY KEY(endpoint_id, source, captured_at)
            );
            CREATE INDEX history_points_time ON history_points(endpoint_id, captured_at);
            CREATE INDEX history_points_captured ON history_points(captured_at);
            CREATE INDEX history_points_query ON history_points(
                endpoint_id, captured_at, source, memory, utilization, temperature, power
            );
            """
        )
        connection.commit()

    def _write_submission(self, connection: sqlite3.Connection, item: _Submission) -> None:
        if item.captured_at < time.time() - _RETENTION_SECONDS:
            return
        raw = _archive_text(item.source, item.text) if item.text is not None else None
        blob = zlib.compress(raw.encode("utf-8"), level=3) if raw is not None else None
        connection.execute(
            "INSERT INTO history_scrapes(endpoint_id, source, captured_at, interval_seconds, error, raw_zlib) VALUES (?, ?, ?, ?, ?, ?)",
            (item.endpoint_id, item.source, item.captured_at, item.interval_seconds, item.error, blob),
        )
        if item.text is not None:
            point = _derive_point(item.source, item.text, self._endpoints[item.endpoint_id].memory_source)
            connection.execute(
                "INSERT OR REPLACE INTO history_points(endpoint_id, source, captured_at, memory, utilization, temperature, power) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item.endpoint_id, item.source, item.captured_at, point.memory, point.utilization, point.temperature, point.power),
            )
        connection.commit()

    def _prune(self, connection: sqlite3.Connection, now: float) -> bool:
        cutoff = now - _RETENTION_SECONDS
        pending = False
        for table in ("history_scrapes", "history_points"):
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE captured_at < ? LIMIT ?)",
                (cutoff, _PRUNE_BATCH_SIZE),
            )
            connection.commit()
            pending = pending or cursor.rowcount == _PRUNE_BATCH_SIZE
        self._last_prune = now
        return pending

    def _query_sync(
        self,
        endpoint_id: str,
        bounded_start: float,
        bounded_end: float,
        width: int,
        requested_start: float,
        requested_end: float,
        now: float,
        retention_start: float,
    ) -> dict[str, object]:
        warning = self._warning()
        if not self.db_path.exists():
            return self._empty_result(endpoint_id, requested_start, requested_end, now, retention_start, width, warning)
        try:
            connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=0.25)
            try:
                deadline = time.monotonic() + _QUERY_SQL_TIMEOUT_SECONDS
                connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10_000)
                row = connection.execute("SELECT value FROM history_meta WHERE key='schema_version'").fetchone()
                if row is None or row[0] != str(_SCHEMA_VERSION):
                    return self._empty_result(
                        endpoint_id,
                        requested_start,
                        requested_end,
                        now,
                        retention_start,
                        width,
                        "history database schema unsupported",
                    )
                span = requested_end - requested_start
                rows = connection.execute(
                    "SELECT source, CASE WHEN captured_at >= ? THEN ? ELSE CAST((captured_at - ?) / ? * ? AS INTEGER) END, "
                    "AVG(memory), MAX(utilization), MAX(temperature), MAX(power) FROM history_points "
                    "WHERE endpoint_id = ? AND captured_at >= ? AND captured_at <= ? "
                    "GROUP BY source, 2 ORDER BY 2",
                    (requested_end, width - 1, requested_start, span, width, endpoint_id, bounded_start, bounded_end),
                ).fetchall()
            finally:
                connection.set_progress_handler(None, 0)
                connection.close()
        except sqlite3.Error as error:
            return self._empty_result(
                endpoint_id,
                requested_start,
                requested_end,
                now,
                retention_start,
                width,
                f"history read unavailable: {str(error) or type(error).__name__}",
            )
        return _aggregate(
            endpoint_id,
            requested_start,
            requested_end,
            now,
            retention_start,
            width,
            bounded_start,
            bounded_end,
            rows,
            self._endpoints[endpoint_id].memory_source,
            warning,
        )

    def _query_with_release(
        self,
        endpoint_id: str,
        bounded_start: float,
        bounded_end: float,
        width: int,
        requested_start: float,
        requested_end: float,
        now: float,
        retention_start: float,
    ) -> dict[str, object]:
        try:
            return self._query_sync(
                endpoint_id,
                bounded_start,
                bounded_end,
                width,
                requested_start,
                requested_end,
                now,
                retention_start,
            )
        finally:
            self._read_slots.release()

    def _empty_result(
        self, endpoint_id: str, start: float, end: float, now: float, retention_start: float, width: int, status: str
    ) -> dict[str, object]:
        return {
            "endpoint_id": endpoint_id,
            "start": start,
            "end": end,
            "now": now,
            "retention_start": retention_start,
            "series": {name: [None] * width for name in ("memory", "utilization", "temperature", "power")},
            "status": status,
        }

    def _set_status(self, message: str) -> None:
        with self._status_lock:
            self._status = message[:240]

    def _warning(self) -> str:
        with self._status_lock:
            return self._status


def _archive_text(source: str, text: str) -> str:
    """Keep only requested metric families while retaining original sample syntax."""
    prefix = "DCGM_" if source == "dcgm" else "node_memory_"
    return "\n".join(line for line in text.splitlines() if line.lstrip().startswith(prefix))


def _derive_point(source: str, text: str, memory_source: str) -> _Point:
    try:
        families = tuple(text_string_to_metric_families(text))
    except Exception:  # noqa: BLE001 - raw malformed exporter output is still archived.
        return _Point(None, None, None, None)
    if source == "node":
        total = _single_unlabelled(families, "node_memory_MemTotal_bytes")
        available = _single_unlabelled(families, "node_memory_MemAvailable_bytes")
        memory = None
        if (
            memory_source == "node-exporter"
            and total is not None
            and available is not None
            and 0 < total < _SIGNED_64_MAX
            and 0 <= available <= total
        ):
            memory = (total - available) / total * 100.0
        return _Point(memory, None, None, None)
    return _derive_dcgm(families, memory_source)


def _derive_dcgm(families: Iterable[object], memory_source: str) -> _Point:
    physical: set[str] = set()
    values: dict[str, dict[str, float]] = {}
    for family in families:
        name = getattr(family, "name", "")
        if not isinstance(name, str) or not name.startswith("DCGM_"):
            continue
        for sample in getattr(family, "samples", ()):
            labels = dict(sample.labels)
            key = _physical_key(labels)
            if key is None:
                continue
            physical.add(key)
            value = _finite_number(sample.value)
            if value is not None:
                values.setdefault(key, {})[name] = value
    utilization = _maximum(values, "DCGM_FI_DEV_GPU_UTIL", 0.0, 100.0)
    temperature = _maximum(values, "DCGM_FI_DEV_GPU_TEMP", -20.0, 150.0)
    memory = None
    if memory_source == "dcgm" and physical:
        totals: list[tuple[float, float]] = []
        for key in physical:
            item = values.get(key, {})
            needed = ("DCGM_FI_DEV_FB_USED", "DCGM_FI_DEV_FB_FREE", "DCGM_FI_DEV_FB_RESERVED")
            if not all(metric in item and item[metric] >= 0 for metric in needed):
                totals = []
                break
            used = item[needed[0]]
            total = used + item[needed[1]] + item[needed[2]]
            if total <= 0:
                totals = []
                break
            totals.append((used, total))
        if totals:
            used_sum, total_sum = (sum(item[0] for item in totals), sum(item[1] for item in totals))
            memory = used_sum / total_sum * 100.0
    power = None
    if physical:
        watts = [values.get(key, {}).get("DCGM_FI_DEV_POWER_USAGE") for key in physical]
        if all(value is not None and 0 <= value < 1_000_000_000_000 for value in watts):
            power = sum(value for value in watts if value is not None)
    return _Point(memory, utilization, temperature, power)


def _physical_key(labels: dict[str, str]) -> str | None:
    if labels.get("GPU_I_ID") or labels.get("GPU_I_PROFILE"):
        return None
    return labels.get("UUID") or labels.get("uuid") or labels.get("gpu")


def _finite_number(value: object) -> float | None:
    if not isinstance(value, (int, float, str, bytes)) or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric in _SENTINELS:
        return None
    return numeric


def _maximum(values: dict[str, dict[str, float]], metric: str, lower: float, upper: float) -> float | None:
    candidates = [item[metric] for item in values.values() if metric in item and lower <= item[metric] <= upper]
    return max(candidates) if candidates else None


def _single_unlabelled(families: Iterable[object], metric: str) -> float | None:
    values: list[float] = []
    for family in families:
        if getattr(family, "name", None) != metric:
            continue
        for sample in getattr(family, "samples", ()):
            if sample.labels:
                return None
            value = _finite_number(sample.value)
            if value is None:
                return None
            values.append(value)
    return values[0] if len(values) == 1 else None


def _aggregate(
    endpoint_id: str,
    start: float,
    end: float,
    now: float,
    retention_start: float,
    width: int,
    bounded_start: float,
    bounded_end: float,
    rows: list[tuple[object, ...]],
    memory_source: str,
    status: str,
) -> dict[str, object]:
    buckets: dict[str, list[list[float]]] = {name: [[] for _ in range(width)] for name in ("memory", "utilization", "temperature", "power")}
    span = bounded_end - bounded_start
    if span > 0:
        for source, bucket, memory, utilization, temperature, power in rows:
            index = min(width - 1, max(0, _as_integer(bucket)))
            if source == "node" and memory_source == "node-exporter" and _valid_plot(memory):
                buckets["memory"][index].append(_as_float(memory))
            if source == "dcgm":
                if memory_source == "dcgm" and _valid_plot(memory):
                    buckets["memory"][index].append(_as_float(memory))
                for name, value in (("utilization", utilization), ("temperature", temperature), ("power", power)):
                    if _valid_plot(value):
                        buckets[name][index].append(_as_float(value))
    series = {
        "memory": [sum(bucket) / len(bucket) if bucket else None for bucket in buckets["memory"]],
        **{name: [max(bucket) if bucket else None for bucket in buckets[name]] for name in ("utilization", "temperature", "power")},
    }
    return {
        "endpoint_id": endpoint_id,
        "start": start,
        "end": end,
        "now": now,
        "retention_start": retention_start,
        "series": series,
        "status": status,
    }


def _valid_plot(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _as_float(value: object) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def _as_integer(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()
