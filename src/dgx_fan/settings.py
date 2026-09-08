"""Controller-owned, revisioned settings persistence and command transport."""

from __future__ import annotations

import asyncio
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
from pathlib import Path
from typing import Any

import tomlkit

from .config import AppConfig, ConfigError, load_config


class SettingsError(RuntimeError):
    """Base class for actionable settings failures."""


class SettingsConflict(SettingsError):
    """The editor or on-disk source is no longer current."""


class SettingsPersistenceError(SettingsError):
    """The configuration could not be made durable."""

    def __init__(self, message: str, *, recovered_fingerprint: str | None = None) -> None:
        super().__init__(message)
        self.recovered_fingerprint = recovered_fingerprint


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
        for key in ("memory", "utilization", "temperature")
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
        "dashboard": {"colors": colors},
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
            try:
                await self._apply(persisted.config, current.revision + 1)
            except BaseException as error:
                rollback_error: BaseException | None = None
                try:
                    await asyncio.to_thread(self._rollback, persisted)
                    await self._apply(current.config, current.revision)
                    self._effective = replace(
                        current, fingerprint=_fingerprint(self._target)
                    )
                except BaseException as candidate:  # noqa: BLE001 - rollback must survive cancellation.
                    rollback_error = candidate
                if rollback_error is not None:
                    raise SettingsApplicationError(
                        "settings were written but live application failed and rollback "
                        f"could not be confirmed: {rollback_error}"
                    ) from error
                raise SettingsApplicationError(
                    "live application failed; the previous configuration was restored"
                ) from error
            updated = EffectiveSettings(
                current.revision + 1,
                _fingerprint(self._target),
                persisted.config,
            )
            self._effective = updated
            return updated

    async def set_power(self, enabled: bool, expected_revision: int) -> None:
        if not isinstance(enabled, bool):
            raise ConfigError("power must be true or false")
        async with self._mutation_lock:
            if expected_revision != self._effective.revision:
                raise SettingsConflict("settings changed; reload before setting power")
            self._set_power(enabled)

    async def close(self) -> None:
        """Drain transactions whose clients disappeared during persistence."""
        if self._save_tasks:
            await asyncio.gather(*tuple(self._save_tasks), return_exceptions=True)

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
        replaced = False
        try:
            candidate_config = replace(load_config(candidate), path=self._config_path)
            # Close the external-edit window as late as possible.
            self._check_source(expected_fingerprint)
            backup = self._target.with_name(f"{self._target.name}.bak")
            self._replace_bytes(backup, original, info)
            self._check_source(expected_fingerprint)
            os.replace(candidate, self._target)
            replaced = True
            replacement_fingerprint = _fingerprint(self._target)
            self._fsync_directory()
        except (ConfigError, SettingsConflict):
            candidate.unlink(missing_ok=True)
            raise
        except BaseException as error:
            candidate.unlink(missing_ok=True)
            if not replaced:
                raise SettingsPersistenceError(
                    f"settings were not written: {str(error) or type(error).__name__}"
                ) from error
            rollback_error: BaseException | None = None
            try:
                persisted = _PersistedSave(
                    candidate_config, original, replacement_fingerprint
                )
                self._rollback(persisted)
            except BaseException as candidate_error:  # noqa: BLE001 - report uncertain durability.
                rollback_error = candidate_error
            if rollback_error is not None:
                raise SettingsPersistenceError(
                    "configuration was replaced but directory durability failed; "
                    f"rollback could not be confirmed: {rollback_error}"
                ) from error
            raise SettingsPersistenceError(
                "configuration durability failed after replace; the previous file was restored",
                recovered_fingerprint=_fingerprint(self._target),
            ) from error
        return _PersistedSave(candidate_config, original, replacement_fingerprint)

    def _rollback(self, persisted: _PersistedSave) -> None:
        if _fingerprint(self._target) != persisted.replacement_fingerprint:
            raise SettingsConflict(
                "configuration changed again after persistence; refusing unsafe rollback"
            )
        info = self._target.stat()
        self._replace_bytes(self._target, persisted.original, info)

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
                "dashboard": {"colors"},
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
            colors = _as_mapping(values.get("colors", {}), "dashboard.colors")
            unknown_colors = set(colors) - {"memory", "utilization", "temperature"}
            if unknown_colors:
                raise ConfigError(
                    f"unknown settings key: dashboard.colors.{min(unknown_colors)}"
                )
            if "colors" not in target:
                target["colors"] = tomlkit.table()
            color_table = target["colors"]
            for key in ("memory", "utilization", "temperature"):
                if key in colors:
                    color_table[key] = colors[key]
                elif key in color_table:
                    del color_table[key]


class SettingsCommandServer:
    """Bounded owner-only NDJSON command socket, separate from telemetry."""

    MAX_BYTES = 64 * 1024
    MAX_CLIENTS = 16
    MAX_CACHE = 256
    REQUEST_TIMEOUT = 3.0
    MUTATION_TIMEOUT = 15.0

    def __init__(self, path: Path, service: SettingsService, allow_control: bool) -> None:
        self.path = path
        self.service = service
        self.allow_control = allow_control
        self.server: asyncio.AbstractServer | None = None
        self._seen: OrderedDict[str, tuple[str, dict[str, object]]] = OrderedDict()
        self._seen_lock = asyncio.Lock()
        self._clients: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._bound_identity: tuple[int, int] | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._remove_owned_stale_socket()
        self.server = await asyncio.start_unix_server(
            self._handle, path=str(self.path), limit=self.MAX_BYTES + 1
        )
        self.path.chmod(0o600)
        info = self.path.lstat()
        self._bound_identity = (info.st_dev, info.st_ino)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for writer in tuple(self._writers):
            writer.close()
        current = asyncio.current_task()
        pending = tuple(task for task in self._clients if task is not current)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self.service.close()
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if (
            self._bound_identity == (info.st_dev, info.st_ino)
            and stat.S_ISSOCK(info.st_mode)
            and info.st_uid == os.geteuid()
        ):
            self.path.unlink()

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
            async with self._seen_lock:
                cached = self._seen.get(request_id)
                if cached is not None:
                    if cached[0] != body_hash:
                        raise SettingsConflict("request_id was already used for a different command")
                    response = cached[1]
                    self._seen.move_to_end(request_id)
                else:
                    try:
                        response = await asyncio.wait_for(
                            self._dispatch(request), self.MUTATION_TIMEOUT
                        )
                    except TimeoutError as error:
                        raise SettingsError("control command timed out") from error
                    self._seen[request_id] = (body_hash, response)
                    while len(self._seen) > self.MAX_CACHE:
                        self._seen.popitem(last=False)
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
        except Exception as error:  # noqa: BLE001 - isolate malformed clients from the controller.
            response = {
                "ok": False,
                "error": "SettingsError",
                "message": f"control command failed: {type(error).__name__}",
            }
        try:
            encoded = (json.dumps(response, separators=(",", ":"), allow_nan=False) + "\n").encode()
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
        }
        if operation not in allowed_request_keys:
            raise ConfigError("unknown control operation")
        unknown = set(request) - allowed_request_keys[operation]
        if unknown:
            raise ConfigError(f"unknown command key: {min(unknown)}")
        if operation == "read-settings":
            return self.service.response()
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
