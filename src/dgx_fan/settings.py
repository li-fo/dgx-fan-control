"""Controller-owned, revisioned settings persistence and command transport."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import json
import os
import socket
import stat
import struct
import tempfile
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path
from typing import Any

import tomlkit

from .config import AppConfig, ConfigError, load_config

MAX_CONTROL_MESSAGE_BYTES = 64 * 1024
HistoryQuery = Callable[[str, float, float, int], Awaitable[dict[str, object]]]


class SettingsError(RuntimeError):
    """Base class for actionable settings failures."""


class SettingsConflict(SettingsError):
    """The editor or on-disk source is no longer current."""


class SettingsPersistenceError(SettingsError):
    """The configuration could not be made durable."""

    def __init__(
        self,
        message: str,
        *,
        recovered_fingerprint: str | None = None,
        retained_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.recovered_fingerprint = recovered_fingerprint
        self.retained_path = retained_path


class SettingsApplicationError(SettingsError):
    """Persistence succeeded but live application did not complete cleanly."""


def control_socket_path(monitor_socket: Path) -> Path:
    """Return the sibling command socket without changing the monitor path."""
    return monitor_socket.with_name(f"{monitor_socket.name}.control")


def _fingerprint(path: Path) -> str:
    """Fingerprint content and metadata that an editor must not silently replace."""
    info = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return ":".join(
        str(value)
        for value in (
            info.st_dev,
            info.st_ino,
            stat.S_IMODE(info.st_mode),
            info.st_uid,
            info.st_gid,
            info.st_size,
            info.st_mtime_ns,
            digest,
        )
    )


def _link_identity(path: Path) -> tuple[int, int, int, str | None]:
    """Capture enough identity to reject a symlink retarget during a save."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), os.readlink(path))
    # Atomic replacement intentionally changes a regular file's inode. Track
    # only its file type here; content/metadata conflicts use _fingerprint.
    return (0, 0, stat.S_IFMT(info.st_mode), None)


def _as_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} patch must be a table")
    if any(not isinstance(key, str) for key in value):
        raise ConfigError(f"{name} patch keys must be strings")
    return value


def settings_payload(config: AppConfig) -> dict[str, object]:
    """Build the stable, JSON-safe editor payload from an effective config."""
    colors = {
        key: value
        for key in ("memory", "utilization", "temperature", "power")
        if (value := getattr(config.dashboard_colors, key)) is not None
    }
    return {
        "collection": {
            "interval_seconds": config.collection.interval_seconds,
            "timeout_seconds": config.collection.timeout_seconds,
            "stale_after_seconds": config.collection.stale_after_seconds,
            "retry_count": config.collection.retry_count,
            "retry_delay_seconds": config.collection.retry_delay_seconds,
        },
        "control": {
            "fan_endpoint_ids": list(config.control.fan_endpoint_ids),
            "fan_mode": config.control.fan_mode,
            "enabled_at_startup": config.control.enabled_at_startup,
            "max_speed_percent": config.control.max_speed_percent,
            "fallback_speed_percent": config.control.fallback_speed_percent,
            "hysteresis_celsius": config.control.hysteresis_celsius,
            "emergency_temperature_celsius": config.control.emergency_temperature_celsius,
            "recovery_seconds": config.control.recovery_seconds,
            "stages": [
                {
                    **(
                        {"max_temperature_celsius": stage.max_temperature_celsius}
                        if stage.max_temperature_celsius is not None
                        else {}
                    ),
                    "speed_percent": stage.speed_percent,
                }
                for stage in config.control.stages
            ],
        },
        "hardware": {
            "startup_boost_seconds": config.hardware.startup_boost_seconds,
            "stall_timeout_seconds": config.hardware.stall_timeout_seconds,
            "shutdown_mode": config.hardware.shutdown_mode,
        },
        "dashboard": {"colors": colors, "graph_view": config.graph_view},
    }


@dataclass(frozen=True)
class EffectiveSettings:
    revision: int
    fingerprint: str
    config: AppConfig


@dataclass(frozen=True)
class _PersistedSave:
    config: AppConfig
    original: bytes
    replacement_fingerprint: str


@dataclass(frozen=True)
class _CommandOperation:
    body_hash: str
    task: asyncio.Task[dict[str, object]]


class SettingsService:
    """Serialize validated saves and apply them on the controller event loop."""

    def __init__(
        self,
        config: AppConfig,
        apply: Callable[[AppConfig, int], Awaitable[None]],
        set_power: Callable[[bool], None],
        get_power: Callable[[], bool],
        source_id: str,
    ) -> None:
        self._config_path = config.path.absolute()
        self._target = self._config_path.resolve(strict=True)
        self._link = _link_identity(self._config_path)
        self._effective = EffectiveSettings(0, _fingerprint(self._target), config)
        self._apply = apply
        self._set_power = set_power
        self._get_power = get_power
        self.source_id = source_id
        self._mutation_lock = asyncio.Lock()
        self._save_tasks: set[asyncio.Task[EffectiveSettings]] = set()
        self._closing = False

    @property
    def effective(self) -> EffectiveSettings:
        return self._effective

    def response(self) -> dict[str, object]:
        value = self._effective
        config = value.config
        return {
            "ok": True,
            "source_id": self.source_id,
            "revision": value.revision,
            "power_enabled": self._get_power(),
            "settings": settings_payload(config),
            "endpoints": [
                {"id": endpoint.id, "name": endpoint.name} for endpoint in config.endpoints
            ],
        }

    async def save(
        self, patch: dict[str, Any], expected_revision: int
    ) -> EffectiveSettings:
        """Complete an accepted transaction even if its client disconnects."""
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ConfigError("revision must be an integer")
        if self._closing:
            raise SettingsError("controller is shutting down; settings were not accepted")
        task = asyncio.create_task(self._save(patch, expected_revision))
        self._save_tasks.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A worker thread cannot be cancelled safely after replace. Drain it
            # and reconcile the runtime before allowing shutdown to continue.
            await task
            raise
        finally:
            self._save_tasks.discard(task)

    async def _save(
        self, patch: dict[str, Any], expected_revision: int
    ) -> EffectiveSettings:
        async with self._mutation_lock:
            if self._closing and asyncio.current_task() not in self._save_tasks:
                raise SettingsError("controller is shutting down; settings were not accepted")
            current = self._effective
            if expected_revision != current.revision:
                raise SettingsConflict("settings changed; reload before saving")
            try:
                persisted = await asyncio.to_thread(
                    self._persist, patch, current.fingerprint
                )
            except SettingsPersistenceError as error:
                if error.recovered_fingerprint is not None:
                    self._effective = replace(
                        current, fingerprint=error.recovered_fingerprint
                    )
                raise
            if not self._target_matches(persisted.replacement_fingerprint):
                raise SettingsConflict(
                    "configuration changed after persistence and before live application; "
                    "external file preserved; restart the controller before saving"
                )
            try:
                await self._apply(persisted.config, current.revision + 1)
            except BaseException as error:
                disk_rollback_error: BaseException | None = None
                runtime_rollback_error: BaseException | None = None
                restored_fingerprint: str | None = None
                try:
                    restored_fingerprint = await asyncio.to_thread(
                        self._rollback, persisted
                    )
                except BaseException as candidate:  # noqa: BLE001 - preserve external bytes.
                    disk_rollback_error = candidate
                try:
                    await self._apply(current.config, current.revision)
                except BaseException as candidate:  # noqa: BLE001 - rollback must survive cancellation.
                    runtime_rollback_error = candidate
                if runtime_rollback_error is None:
                    self._effective = replace(
                        current,
                        fingerprint=(
                            restored_fingerprint
                            if restored_fingerprint is not None
                            else current.fingerprint
                        ),
                    )
                if disk_rollback_error is not None or runtime_rollback_error is not None:
                    raise SettingsApplicationError(
                        "settings were written but live application failed and rollback "
                        "could not be confirmed; external file was preserved when present; "
                        "restart the controller before saving "
                        f"(disk={disk_rollback_error}, runtime={runtime_rollback_error})"
                    ) from error
                assert restored_fingerprint is not None
                if not self._target_matches(restored_fingerprint):
                    raise SettingsApplicationError(
                        "previous live settings were restored, but the configuration changed "
                        "during rollback application; external file preserved; restart the "
                        "controller before saving"
                    ) from error
                raise SettingsApplicationError(
                    "live application failed; the previous configuration was restored"
                ) from error
            updated = EffectiveSettings(
                current.revision + 1,
                persisted.replacement_fingerprint,
                persisted.config,
            )
            self._effective = updated
            if not self._target_matches(persisted.replacement_fingerprint):
                raise SettingsApplicationError(
                    "live settings were applied, but the configuration changed before "
                    "acknowledgement; external file preserved; restart the controller "
                    "before saving"
                )
            return updated

    async def set_power(self, enabled: bool, expected_revision: int) -> None:
        if not isinstance(enabled, bool):
            raise ConfigError("power must be true or false")
        if self._closing:
            raise SettingsError("controller is shutting down; power command was not accepted")
        async with self._mutation_lock:
            if self._closing:
                raise SettingsError("controller is shutting down; power command was not accepted")
            if expected_revision != self._effective.revision:
                raise SettingsConflict("settings changed; reload before setting power")
            self._set_power(enabled)

    def begin_close(self) -> None:
        """Reject new mutations while already-accepted saves reconcile."""
        self._closing = True

    async def close(self, grace_seconds: float = 2.0) -> bool:
        """Wait a bounded grace period for accepted saves; never abandon them."""
        self.begin_close()
        if not self._save_tasks:
            return True
        _done, pending = await asyncio.wait(
            tuple(self._save_tasks), timeout=max(0.0, grace_seconds)
        )
        return not pending

    def _check_source(self, expected_fingerprint: str) -> os.stat_result:
        if _link_identity(self._config_path) != self._link:
            raise SettingsConflict("configuration path changed; reload before saving")
        if self._config_path.resolve(strict=True) != self._target:
            raise SettingsConflict("configuration symlink target changed; reload before saving")
        info = self._target.stat()
        if not stat.S_ISREG(info.st_mode):
            raise SettingsPersistenceError("configuration target must be a regular file")
        if not os.access(self._target, os.W_OK, effective_ids=True):
            raise SettingsPersistenceError("configuration file is not writable by the controller user")
        if not os.access(self._target.parent, os.W_OK, effective_ids=True):
            raise SettingsPersistenceError("configuration directory is not writable by the controller user")
        if _fingerprint(self._target) != expected_fingerprint:
            raise SettingsConflict("configuration file changed; reload before saving")
        return info

    def _persist(self, patch: dict[str, Any], expected_fingerprint: str) -> _PersistedSave:
        info = self._check_source(expected_fingerprint)
        original = self._target.read_bytes()
        try:
            document = tomlkit.parse(original.decode())
            self._patch(document, patch)
            rendered = tomlkit.dumps(document).encode()
        except (UnicodeDecodeError, ValueError, TypeError) as error:
            if isinstance(error, ConfigError):
                raise
            raise ConfigError(f"invalid settings patch: {error}") from error

        candidate = self._write_temp(rendered, info, ".tmp")
        try:
            candidate_config = replace(load_config(candidate), path=self._config_path)
            # Close the external-edit window as late as possible.
            self._check_source(expected_fingerprint)
            self._before_exchange()
            replacement_fingerprint = self._exchange_install(
                candidate, expected_fingerprint
            )
        except (ConfigError, SettingsConflict):
            candidate.unlink(missing_ok=True)
            raise
        except SettingsPersistenceError as error:
            if error.retained_path is None:
                candidate.unlink(missing_ok=True)
            raise
        except BaseException as error:
            candidate.unlink(missing_ok=True)
            raise SettingsPersistenceError(
                f"settings were not written: {str(error) or type(error).__name__}"
            ) from error

        try:
            backup = self._target.with_name(f"{self._target.name}.bak")
            # The exchanged-away source remains at candidate until the backup
            # and target directory are durable.
            self._replace_bytes(backup, candidate.read_bytes(), candidate.stat())
            self._finish_exchange(candidate)
        except BaseException as error:
            rollback_error: BaseException | None = None
            try:
                rollback_candidate = (
                    candidate
                    if candidate.exists()
                    else self._write_temp(original, self._target.stat(), ".rollback.tmp")
                )
                self._exchange_install(rollback_candidate, replacement_fingerprint)
                self._finish_exchange(rollback_candidate)
            except BaseException as candidate_error:  # noqa: BLE001 - preserve displaced recovery data.
                rollback_error = candidate_error
            if rollback_error is not None:
                retained = candidate if candidate.exists() else None
                raise SettingsPersistenceError(
                    "configuration was replaced but durability/backup failed; rollback "
                    f"could not be confirmed: {rollback_error}",
                    retained_path=retained,
                ) from error
            raise SettingsPersistenceError(
                "configuration durability failed after exchange; the previous file was restored",
                recovered_fingerprint=_fingerprint(self._target),
            ) from error
        return _PersistedSave(candidate_config, original, replacement_fingerprint)

    def _target_matches(self, expected_fingerprint: str) -> bool:
        try:
            self._check_source(expected_fingerprint)
        except (OSError, RuntimeError):
            return False
        return True

    def _rollback(self, persisted: _PersistedSave) -> str:
        info = self._target.stat()
        candidate = self._write_temp(persisted.original, info, ".rollback.tmp")
        # _exchange_install keeps a displaced source when recovery is
        # uncertain. Never erase that only remaining recovery artifact.
        restored_fingerprint = self._exchange_install(
            candidate, persisted.replacement_fingerprint
        )
        self._finish_exchange(candidate)
        return restored_fingerprint

    def _before_exchange(self) -> None:
        """Deterministic test seam immediately before the atomic commit boundary."""

    @staticmethod
    def _exchange_paths(first: Path, second: Path) -> None:
        """Atomically exchange two Linux directory entries without overwrite fallback."""
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            renameat2 = libc.renameat2
        except AttributeError as error:
            raise SettingsPersistenceError(
                "atomic settings replacement is unsupported by this Linux runtime"
            ) from error
        result = renameat2(
            ctypes.c_int(-100),
            ctypes.c_char_p(os.fsencode(first)),
            ctypes.c_int(-100),
            ctypes.c_char_p(os.fsencode(second)),
            ctypes.c_uint(2),
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number in {
            errno.ENOSYS,
            errno.EINVAL,
            errno.EOPNOTSUPP,
            getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
        }:
            raise SettingsPersistenceError(
                "atomic settings replacement is unsupported by this filesystem"
            )
        raise OSError(error_number, os.strerror(error_number), str(second))

    def _exchange_install(self, candidate: Path, expected_fingerprint: str) -> str:
        """Install candidate and verify the atomically displaced source."""
        self._exchange_paths(candidate, self._target)
        replacement_fingerprint = _fingerprint(self._target)
        try:
            if _link_identity(self._config_path) != self._link:
                raise SettingsConflict(
                    "configuration path changed at the save boundary; external file preserved"
                )
            if self._config_path.resolve(strict=True) != self._target:
                raise SettingsConflict(
                    "configuration symlink target changed at the save boundary; external file preserved"
                )
            if _fingerprint(candidate) != expected_fingerprint:
                raise SettingsConflict(
                    "configuration file changed at the save boundary; external file preserved"
                )
            self._fsync_directory()
        except BaseException as error:
            try:
                if _fingerprint(self._target) != replacement_fingerprint:
                    raise SettingsConflict(
                        "configuration changed again after atomic exchange"
                    )
                self._exchange_paths(candidate, self._target)
                self._fsync_directory()
            except BaseException as restore_error:  # noqa: BLE001 - preserve the displaced source.
                raise SettingsPersistenceError(
                    "atomic settings exchange could not be restored; displaced source "
                    f"retained at {candidate}: {restore_error}",
                    retained_path=candidate,
                ) from error
            candidate.unlink(missing_ok=True)
            self._fsync_directory()
            if isinstance(error, SettingsConflict):
                raise
            raise SettingsPersistenceError(
                "configuration exchange durability failed; the previous file was restored",
                recovered_fingerprint=_fingerprint(self._target),
            ) from error
        return replacement_fingerprint

    def _finish_exchange(self, displaced: Path) -> None:
        displaced.unlink()
        self._fsync_directory()

    def _replace_bytes(self, destination: Path, content: bytes, info: os.stat_result) -> None:
        candidate = self._write_temp(content, info, f".{destination.name}.tmp")
        try:
            os.replace(candidate, destination)
            self._fsync_directory()
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise

    def _write_temp(self, content: bytes, info: os.stat_result, suffix: str) -> Path:
        fd, name = tempfile.mkstemp(
            prefix=f".{self._target.name}.", suffix=suffix, dir=self._target.parent
        )
        path = Path(name)
        try:
            os.fchmod(fd, stat.S_IMODE(info.st_mode))
            os.fchown(fd, info.st_uid, info.st_gid)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            if fd >= 0:
                os.close(fd)
            path.unlink(missing_ok=True)
            raise
        return path

    def _fsync_directory(self) -> None:
        directory_fd = os.open(self._target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _patch(document: Any, patch: dict[str, Any]) -> None:
        allowed = {"collection", "control", "hardware", "dashboard"}
        if any(not isinstance(key, str) for key in patch):
            raise ConfigError("settings section names must be strings")
        unknown = set(patch) - allowed
        if unknown:
            raise ConfigError(f"unknown settings section: {min(unknown)}")
        for section, raw_values in patch.items():
            values = _as_mapping(raw_values, section)
            permitted = {
                "collection": {
                    "interval_seconds",
                    "timeout_seconds",
                    "stale_after_seconds",
                    "retry_count",
                    "retry_delay_seconds",
                },
                "control": {
                    "fan_endpoint_ids",
                    "fan_mode",
                    "enabled_at_startup",
                    "max_speed_percent",
                    "fallback_speed_percent",
                    "hysteresis_celsius",
                    "emergency_temperature_celsius",
                    "recovery_seconds",
                    "stages",
                },
                "hardware": {
                    "startup_boost_seconds",
                    "stall_timeout_seconds",
                    "shutdown_mode",
                },
                "dashboard": {"colors", "graph_view"},
            }[section]
            unknown_keys = set(values) - permitted
            if unknown_keys:
                raise ConfigError(f"unknown settings key: {section}.{min(unknown_keys)}")
            if section not in document:
                document[section] = tomlkit.table()
            target = document[section]
            if section != "dashboard":
                for key, value in values.items():
                    if section == "control" and key == "stages":
                        if not isinstance(value, list):
                            raise ConfigError("control.stages patch must be a list")
                        for index, stage in enumerate(value):
                            stage_mapping = _as_mapping(
                                stage, f"control.stages[{index}]"
                            )
                            stage_unknown = set(stage_mapping) - {
                                "max_temperature_celsius",
                                "speed_percent",
                            }
                            if stage_unknown:
                                raise ConfigError(
                                    "unknown settings key: "
                                    f"control.stages[{index}].{min(stage_unknown)}"
                                )
                    target[key] = value
                continue
            if "colors" in values:
                colors = _as_mapping(values["colors"], "dashboard.colors")
                unknown_colors = set(colors) - {"memory", "utilization", "temperature", "power"}
                if unknown_colors:
                    raise ConfigError(
                        f"unknown settings key: dashboard.colors.{min(unknown_colors)}"
                    )
                if "colors" not in target:
                    target["colors"] = tomlkit.table()
                color_table = target["colors"]
                for key in ("memory", "utilization", "temperature", "power"):
                    if key in colors:
                        color_table[key] = colors[key]
                    elif key in color_table:
                        del color_table[key]
            if "graph_view" in values:
                target["graph_view"] = values["graph_view"]


class SettingsCommandServer:
    """Bounded owner-only NDJSON command socket, separate from telemetry."""

    MAX_BYTES = MAX_CONTROL_MESSAGE_BYTES
    MAX_CLIENTS = 16
    MAX_CACHE = 256
    REQUEST_TIMEOUT = 3.0
    ACK_TIMEOUT = 2.5
    HISTORY_ACK_TIMEOUT = 18.0
    HISTORY_TIMEOUT = 18.0
    MAX_HISTORY_QUERIES = 2

    def __init__(
        self,
        path: Path,
        service: SettingsService,
        allow_control: bool,
        history_query: HistoryQuery | None = None,
    ) -> None:
        self.path = path
        self.service = service
        self.allow_control = allow_control
        self._history_query = history_query
        self._history_slots = asyncio.Semaphore(self.MAX_HISTORY_QUERIES)
        self.server: asyncio.AbstractServer | None = None
        self._seen: OrderedDict[str, _CommandOperation] = OrderedDict()
        self._seen_lock = asyncio.Lock()
        self._clients: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._bound_identity: tuple[int, int] | None = None
        self._closing = False

    async def start(self) -> None:
        self._closing = False
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._remove_owned_stale_socket()
        self.server = await asyncio.start_unix_server(
            self._handle, path=str(self.path), limit=self.MAX_BYTES + 1
        )
        self.path.chmod(0o600)
        info = self.path.lstat()
        self._bound_identity = (info.st_dev, info.st_ino)

    def begin_close(self) -> None:
        """Close mutation/listener gates before hardware enters safe shutdown."""
        self._closing = True
        self.service.begin_close()
        if self.server is not None:
            self.server.close()

    async def close(self, grace_seconds: float = 2.0) -> bool:
        self.begin_close()
        if self.server is not None:
            await self.server.wait_closed()
            self.server = None
        for writer in tuple(self._writers):
            writer.close()
        current = asyncio.current_task()
        pending = tuple(task for task in self._clients if task is not current)
        if pending:
            await asyncio.wait(pending, timeout=max(0.0, grace_seconds))
        reconciled = await self.service.close(grace_seconds)
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return reconciled
        if (
            self._bound_identity == (info.st_dev, info.st_ino)
            and stat.S_ISSOCK(info.st_mode)
            and info.st_uid == os.geteuid()
        ):
            self.path.unlink()
        return reconciled

    def _remove_owned_stale_socket(self) -> None:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise RuntimeError("control socket path exists but is not an owned Unix socket")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.1)
            probe.connect(str(self.path))
        except OSError:
            self.path.unlink()
        else:
            raise RuntimeError("another dgx-fan command server is already active")
        finally:
            probe.close()

    def _peer_is_owner(self, writer: asyncio.StreamWriter) -> bool:
        peer = writer.get_extra_info("socket")
        if peer is None or not hasattr(socket, "SO_PEERCRED"):
            return True
        credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = struct.unpack("3i", credentials)
        return int(uid) == os.geteuid()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        if len(self._clients) >= self.MAX_CLIENTS:
            writer.close()
            await writer.wait_closed()
            return
        self._clients.add(task)
        self._writers.add(writer)
        response: dict[str, object]
        try:
            if not self._peer_is_owner(writer):
                raise PermissionError("control socket is restricted to the controller owner")
            raw = await asyncio.wait_for(reader.readline(), self.REQUEST_TIMEOUT)
            if not raw or len(raw) > self.MAX_BYTES or not raw.endswith(b"\n"):
                raise ConfigError("command has an invalid size")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ConfigError("command must be an object")
            request_id = request.get("request_id")
            if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
                raise ConfigError("request_id is required and must be at most 128 characters")
            body_hash = hashlib.sha256(
                json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            ).hexdigest()
            if request.get("operation") == "read-history":
                response = await asyncio.wait_for(
                    self._run_dispatch(request), self.HISTORY_ACK_TIMEOUT
                )
                raise _DirectResponse(response)
            async with self._seen_lock:
                cached = self._seen.get(request_id)
                if cached is not None:
                    if cached.body_hash != body_hash:
                        raise SettingsConflict("request_id was already used for a different command")
                    self._seen.move_to_end(request_id)
                else:
                    if self._closing:
                        raise SettingsError("controller is shutting down; command was not accepted")
                    self._prune_seen()
                    if len(self._seen) >= self.MAX_CACHE:
                        raise SettingsError("too many control operations are still pending")
                    cached = _CommandOperation(
                        body_hash, asyncio.create_task(self._run_dispatch(request))
                    )
                    self._seen[request_id] = cached
            try:
                response = await asyncio.wait_for(
                    asyncio.shield(cached.task), self.ACK_TIMEOUT
                )
            except TimeoutError:
                current = self.service.effective
                response = {
                    "ok": False,
                    "error": "SettingsPending",
                    "message": "control command is accepted and still pending",
                    "pending": True,
                    "request_id": request_id,
                    "source_id": self.service.source_id,
                    "revision": current.revision,
                }
        except (
            ConfigError,
            SettingsError,
            PermissionError,
            ValueError,
            json.JSONDecodeError,
            TimeoutError,
        ) as error:
            response = {
                "ok": False,
                "error": type(error).__name__,
                "message": str(error) or "control command failed",
            }
        except _DirectResponse as direct:
            response = direct.response
        except Exception as error:  # noqa: BLE001 - isolate malformed clients from the controller.
            response = {
                "ok": False,
                "error": "SettingsError",
                "message": f"control command failed: {type(error).__name__}",
            }
        try:
            encoded = self._encode_response(response)
            writer.write(encoded)
            await asyncio.wait_for(writer.drain(), self.REQUEST_TIMEOUT)
        except (ConnectionError, TimeoutError):
            pass
        finally:
            self._writers.discard(writer)
            self._clients.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    def _prune_seen(self) -> None:
        for request_id, operation in tuple(self._seen.items()):
            if len(self._seen) < self.MAX_CACHE:
                break
            if operation.task.done():
                del self._seen[request_id]

    def _encode_response(self, response: dict[str, object]) -> bytes:
        encoded = (json.dumps(response, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(encoded) <= self.MAX_BYTES:
            return encoded
        return (
            json.dumps(
                {
                    "ok": False,
                    "error": "SettingsError",
                    "message": "control response exceeds the bounded transport limit",
                },
                separators=(",", ":"),
            )
            + "\n"
        ).encode()

    async def _run_dispatch(self, request: dict[str, Any]) -> dict[str, object]:
        try:
            return await self._dispatch(request)
        except (
            ConfigError,
            SettingsError,
            PermissionError,
            ValueError,
            json.JSONDecodeError,
            TimeoutError,
        ) as error:
            return {
                "ok": False,
                "error": type(error).__name__,
                "message": str(error) or "control command failed",
            }
        except Exception as error:  # noqa: BLE001 - isolate controller operation failures.
            return {
                "ok": False,
                "error": "SettingsError",
                "message": f"control command failed: {type(error).__name__}",
            }

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, object]:
        if request.get("version") != 1:
            raise ConfigError("unsupported control protocol version")
        operation = request.get("operation")
        allowed_request_keys = {
            "read-settings": {"version", "operation", "request_id", "source_id"},
            "save-settings": {
                "version",
                "operation",
                "request_id",
                "source_id",
                "revision",
                "patch",
            },
            "set-power": {
                "version",
                "operation",
                "request_id",
                "source_id",
                "revision",
                "enabled",
            },
            "read-history": {
                "version",
                "operation",
                "request_id",
                "source_id",
                "endpoint_id",
                "start",
                "end",
                "width",
            },
        }
        if operation not in allowed_request_keys:
            raise ConfigError("unknown control operation")
        unknown = set(request) - allowed_request_keys[operation]
        if unknown:
            raise ConfigError(f"unknown command key: {min(unknown)}")
        if operation == "read-settings":
            return self.service.response()
        if operation == "read-history":
            if self._history_query is None:
                raise SettingsError("history is unavailable")
            source_id = request.get("source_id")
            if not isinstance(source_id, str) or source_id not in {"", self.service.source_id}:
                raise SettingsConflict("controller identity changed; reload history")
            endpoint_id = request.get("endpoint_id")
            start = request.get("start")
            end = request.get("end")
            width = request.get("width")
            if not isinstance(endpoint_id, str) or not endpoint_id:
                raise ConfigError("endpoint_id is required")
            if not isinstance(start, (int, float)) or isinstance(start, bool):
                raise ConfigError("start must be a number")
            if not isinstance(end, (int, float)) or isinstance(end, bool):
                raise ConfigError("end must be a number")
            if not isinstance(width, int) or isinstance(width, bool) or not 1 <= width <= 1024:
                raise ConfigError("width must be an integer from 1 through 1024")
            if not isfinite(float(start)) or not isfinite(float(end)) or not 0 <= start < end:
                raise ConfigError("start must be before end")
            if self._history_slots.locked():
                raise SettingsError("too many history requests are pending")
            async with self._history_slots:
                result = await asyncio.wait_for(
                    self._history_query(endpoint_id, float(start), float(end), width),
                    self.HISTORY_TIMEOUT,
                )
            return {"ok": True, **result}
        if not self.allow_control:
            raise PermissionError("browser control is disabled by web.allow_control")
        if request.get("source_id") != self.service.source_id:
            raise SettingsConflict("controller identity changed; reload settings")
        revision = request.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise ConfigError("revision must be an integer")
        if operation == "save-settings":
            patch = request.get("patch")
            if not isinstance(patch, dict):
                raise ConfigError("patch must be an object")
            await self.service.save(patch, revision)
        else:
            enabled = request.get("enabled")
            if not isinstance(enabled, bool):
                raise ConfigError("enabled must be true or false")
            await self.service.set_power(enabled, revision)
        return self.service.response()


class _DirectResponse(Exception):
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
