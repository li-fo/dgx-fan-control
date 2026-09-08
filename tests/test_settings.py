from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from dgx_fan.config import AppConfig, load_config
from dgx_fan.settings import (
    SettingsApplicationError,
    SettingsCommandServer,
    SettingsConflict,
    SettingsPersistenceError,
    SettingsService,
    control_socket_path,
)


def _config_path(tmp_path: Path, name: str = "config.toml") -> Path:
    path = tmp_path / name
    path.write_text(Path("config.example.toml").read_text())
    path.chmod(0o640)
    return path


def _patch(interval: float, *, colors: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "collection": {"interval_seconds": interval},
        "dashboard": {"colors": colors or {}},
    }


def _service(
    path: Path,
    applied: list[AppConfig],
    *,
    fail_interval: float | None = None,
) -> tuple[SettingsService, dict[str, bool]]:
    power = {"enabled": True}

    async def apply(config: AppConfig, _revision: int) -> None:
        applied.append(config)
        if config.collection.interval_seconds == fail_interval:
            raise RuntimeError("apply failed")

    return (
        SettingsService(
            load_config(path),
            apply,
            lambda enabled: power.__setitem__("enabled", enabled),
            lambda: power["enabled"],
            "controller-one",
        ),
        power,
    )


def test_persists_comment_preserving_patch_backup_and_consecutive_saves(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        path = _config_path(tmp_path)
        original = path.read_text()
        service, _power = _service(path, [])

        first = await service.save(_patch(2.5), 0)
        second = await service.save(_patch(3.0, colors={"temperature": "blue"}), 1)

        assert first.revision == 1 and second.revision == 2
        assert load_config(path).collection.interval_seconds == 3.0
        assert load_config(path).dashboard_colors.temperature == "blue"
        assert load_config(path).dashboard_colors.memory is None
        assert "# Extra attempts after the first DCGM request" in path.read_text()
        assert path.with_name("config.toml.bak").read_text() != original
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
        assert stat.S_IMODE(path.with_name("config.toml.bak").stat().st_mode) == 0o640

    asyncio.run(exercise())


def test_external_edit_and_symlink_retarget_are_conflicts(tmp_path: Path) -> None:
    async def exercise() -> None:
        target = _config_path(tmp_path, "target.toml")
        alternate = _config_path(tmp_path, "alternate.toml")
        link = tmp_path / "config.toml"
        link.symlink_to(target.name)
        service, _power = _service(link, [])

        target.write_text(target.read_text() + "\n# external edit\n")
        with pytest.raises(SettingsConflict, match="file changed"):
            await service.save(_patch(2.0), 0)

        # A new service sees the external edit, then rejects a retarget while
        # preserving the symlink and both target files.
        service, _power = _service(link, [])
        link.unlink()
        link.symlink_to(alternate.name)
        with pytest.raises(SettingsConflict, match="path changed|target changed"):
            await service.save(_patch(2.0), 0)
        assert link.is_symlink()
        assert load_config(target).collection.interval_seconds == 2.0
        assert load_config(alternate).collection.interval_seconds == 2.0

    asyncio.run(exercise())


def test_application_failure_rolls_back_and_allows_a_later_save(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = _config_path(tmp_path)
        original = path.read_bytes()
        applied: list[AppConfig] = []
        service, _power = _service(path, applied, fail_interval=2.5)

        with pytest.raises(SettingsApplicationError, match="previous configuration was restored"):
            await service.save(_patch(2.5), 0)
        assert path.read_bytes() == original
        assert service.effective.revision == 0

        result = await service.save(_patch(3.0), 0)
        assert result.revision == 1
        assert load_config(path).collection.interval_seconds == 3.0
        assert [item.collection.interval_seconds for item in applied] == [2.5, 2.0, 3.0]

    asyncio.run(exercise())


def test_directory_fsync_failure_after_replace_restores_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        path = _config_path(tmp_path)
        original = path.read_bytes()
        service, _power = _service(path, [])
        real_fsync = service._fsync_directory
        calls = 0

        def fail_target_fsync_once() -> None:
            nonlocal calls
            calls += 1
            # Backup fsync is first, target replacement fsync is second, and
            # rollback fsync is third.
            if calls == 2:
                raise OSError("directory fsync failed")
            real_fsync()

        monkeypatch.setattr(service, "_fsync_directory", fail_target_fsync_once)
        with pytest.raises(SettingsPersistenceError, match="previous file was restored"):
            await service.save(_patch(2.5), 0)
        assert path.read_bytes() == original
        assert service.effective.revision == 0

        monkeypatch.setattr(service, "_fsync_directory", real_fsync)
        assert (await service.save(_patch(3.0), 0)).revision == 1

    asyncio.run(exercise())


async def _request(path: Path, request: dict[str, object]) -> dict[str, object]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
        await writer.drain()
        response = json.loads(await asyncio.wait_for(reader.readline(), 2))
        assert isinstance(response, dict)
        return response
    finally:
        writer.close()
        await writer.wait_closed()


def test_command_socket_end_to_end_opt_in_power_save_and_duplicate_scope(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        path = _config_path(tmp_path)
        service, power = _service(path, [])
        socket_path = control_socket_path(tmp_path / "monitor.sock")
        server = SettingsCommandServer(socket_path, service, True)
        await server.start()
        try:
            assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
            read = await _request(
                socket_path,
                {"version": 1, "operation": "read-settings", "request_id": "read", "source_id": ""},
            )
            assert read["ok"] is True and read["revision"] == 0
            power_request = {
                "version": 1,
                "operation": "set-power",
                "request_id": "power",
                "source_id": "controller-one",
                "revision": 0,
                "enabled": False,
            }
            first = await _request(socket_path, power_request)
            duplicate = await _request(socket_path, power_request)
            assert first == duplicate and power["enabled"] is False
            conflicting_id = await _request(
                socket_path, {**power_request, "enabled": True}
            )
            assert conflicting_id["ok"] is False
            assert "different command" in str(conflicting_id["message"])

            saved = await _request(
                socket_path,
                {
                    "version": 1,
                    "operation": "save-settings",
                    "request_id": "save",
                    "source_id": "controller-one",
                    "revision": 0,
                    "patch": _patch(2.0),
                },
            )
            assert saved["ok"] is True and saved["revision"] == 1
            assert load_config(path).collection.interval_seconds == 2.0
        finally:
            await server.close()
        assert not socket_path.exists()

    asyncio.run(exercise())


def test_command_socket_rejects_mutations_when_control_is_disabled(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = _config_path(tmp_path)
        service, power = _service(path, [])
        socket_path = tmp_path / "control.sock"
        server = SettingsCommandServer(socket_path, service, False)
        await server.start()
        try:
            response = await _request(
                socket_path,
                {
                    "version": 1,
                    "operation": "set-power",
                    "request_id": "denied",
                    "source_id": "controller-one",
                    "revision": 0,
                    "enabled": False,
                },
            )
            assert response["ok"] is False
            assert response["error"] == "PermissionError"
            assert power["enabled"] is True
        finally:
            await server.close()

    asyncio.run(exercise())


def test_active_owned_command_socket_is_never_unlinked(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = _config_path(tmp_path)
        first_service, _power = _service(path, [])
        second_service, _power = _service(path, [])
        socket_path = tmp_path / "control.sock"
        first = SettingsCommandServer(socket_path, first_service, True)
        second = SettingsCommandServer(socket_path, second_service, True)
        await first.start()
        try:
            with pytest.raises(RuntimeError, match="already active"):
                await second.start()
            assert socket_path.exists()
        finally:
            await first.close()

    asyncio.run(exercise())


def test_command_socket_rejects_oversized_and_unknown_fields(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = _config_path(tmp_path)
        service, _power = _service(path, [])
        socket_path = tmp_path / "control.sock"
        server = SettingsCommandServer(socket_path, service, True)
        await server.start()
        try:
            response = await _request(
                socket_path,
                {
                    "version": 1,
                    "operation": "read-settings",
                    "request_id": "unknown",
                    "source_id": "",
                    "surprise": True,
                },
            )
            assert response["ok"] is False
            assert "unknown command key" in str(response["message"])

            reader, writer = await asyncio.open_unix_connection(str(socket_path))
            writer.write(b"x" * (SettingsCommandServer.MAX_BYTES + 1) + b"\n")
            await writer.drain()
            oversized = json.loads(await reader.readline())
            assert oversized["ok"] is False
            writer.close()
            await writer.wait_closed()
        finally:
            await server.close()

    asyncio.run(exercise())


def test_config_backup_pattern_is_ignored() -> None:
    result = os.popen("git check-ignore config.toml.bak").read().strip()
    assert result == "config.toml.bak"
