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
    _fingerprint,
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


def _external_edit(path: Path, content: bytes, *, replacement: bool) -> None:
    if replacement:
        candidate = path.with_name("external-editor.toml")
        candidate.write_bytes(content)
        candidate.chmod(0o640)
        os.replace(candidate, path)
    else:
        path.write_bytes(content)


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


async def _wait_for_revision(service: SettingsService, revision: int) -> None:
    async with asyncio.timeout(2):
        while service.effective.revision != revision:
            await asyncio.sleep(0)


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


@pytest.mark.parametrize("replacement", [False, True], ids=["in-place", "replacement"])
def test_external_edit_at_atomic_commit_boundary_is_restored_as_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: bool
) -> None:
    async def exercise() -> None:
        path = _config_path(tmp_path)
        applied: list[AppConfig] = []
        service, _power = _service(path, applied)
        external = path.read_bytes() + b"\n# final-boundary external edit\n"

        def edit_after_last_check() -> None:
            if replacement:
                candidate = tmp_path / "external-editor.toml"
                candidate.write_bytes(external)
                candidate.chmod(0o640)
                os.replace(candidate, path)
            else:
                path.write_bytes(external)

        monkeypatch.setattr(service, "_before_exchange", edit_after_last_check)
        with pytest.raises(SettingsConflict, match="save boundary"):
            await service.save(_patch(3.0), 0)

        assert path.read_bytes() == external
        assert service.effective.revision == 0
        assert applied == []

    asyncio.run(exercise())


def test_unsupported_atomic_exchange_fails_without_replacing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        path = _config_path(tmp_path)
        original = path.read_bytes()
        service, _power = _service(path, [])

        def unsupported(_first: Path, _second: Path) -> None:
            raise SettingsPersistenceError(
                "atomic settings replacement is unsupported by this filesystem"
            )

        monkeypatch.setattr(service, "_exchange_paths", unsupported)
        with pytest.raises(SettingsPersistenceError, match="unsupported"):
            await service.save(_patch(3.0), 0)
        assert path.read_bytes() == original
        assert service.effective.revision == 0

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


@pytest.mark.parametrize("replacement", [False, True], ids=["in-place", "replacement"])
def test_external_edit_during_live_apply_is_not_blessed(
    tmp_path: Path, replacement: bool
) -> None:
    async def exercise() -> None:
        path = _config_path(tmp_path)
        apply_started = asyncio.Event()
        release_apply = asyncio.Event()

        async def apply(_config: AppConfig, revision: int) -> None:
            assert revision == 1
            apply_started.set()
            await asyncio.wait_for(release_apply.wait(), 2)

        service = SettingsService(
            load_config(path), apply, lambda _enabled: None, lambda: True, "controller-one"
        )
        save = asyncio.create_task(service.save(_patch(3.0), 0))
        try:
            await asyncio.wait_for(apply_started.wait(), 2)
            installed_fingerprint = _fingerprint(path)
            external = path.read_bytes() + b"\n# external edit during live apply\n"
            _external_edit(path, external, replacement=replacement)
            release_apply.set()

            with pytest.raises(SettingsApplicationError, match="applied.*changed.*restart"):
                await asyncio.wait_for(save, 2)

            assert path.read_bytes() == external
            assert service.effective.revision == 1
            assert service.effective.config.collection.interval_seconds == 3.0
            assert service.effective.fingerprint == installed_fingerprint
            assert service.effective.fingerprint != _fingerprint(path)
            with pytest.raises(SettingsConflict, match="file changed"):
                await service.save(_patch(3.5), 1)
        finally:
            release_apply.set()
            if not save.done():
                save.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await save

    asyncio.run(exercise())


@pytest.mark.parametrize("replacement", [False, True], ids=["in-place", "replacement"])
def test_external_edit_during_rollback_apply_is_not_blessed(
    tmp_path: Path, replacement: bool
) -> None:
    async def exercise() -> None:
        path = _config_path(tmp_path)
        rollback_started = asyncio.Event()
        release_rollback = asyncio.Event()

        async def apply(_config: AppConfig, revision: int) -> None:
            if revision == 1:
                raise RuntimeError("candidate application failed")
            assert revision == 0
            rollback_started.set()
            await asyncio.wait_for(release_rollback.wait(), 2)

        service = SettingsService(
            load_config(path), apply, lambda _enabled: None, lambda: True, "controller-one"
        )
        save = asyncio.create_task(service.save(_patch(3.0), 0))
        try:
            await asyncio.wait_for(rollback_started.wait(), 2)
            restored_fingerprint = _fingerprint(path)
            external = path.read_bytes() + b"\n# external edit during rollback apply\n"
            _external_edit(path, external, replacement=replacement)
            release_rollback.set()

            with pytest.raises(
                SettingsApplicationError,
                match="restored.*changed.*restart",
            ):
                await asyncio.wait_for(save, 2)

            assert path.read_bytes() == external
            assert service.effective.revision == 0
            assert service.effective.config.collection.interval_seconds == 2.0
            assert service.effective.fingerprint == restored_fingerprint
            assert service.effective.fingerprint != _fingerprint(path)
            with pytest.raises(SettingsConflict, match="file changed"):
                await service.save(_patch(3.5), 0)
        finally:
            release_rollback.set()
            if not save.done():
                save.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await save

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


def test_slow_accepted_save_returns_pending_then_same_request_gets_final_result(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        path = _config_path(tmp_path)
        started = asyncio.Event()
        release = asyncio.Event()
        applied = 0

        async def apply(_config: AppConfig, _revision: int) -> None:
            nonlocal applied
            applied += 1
            started.set()
            await asyncio.wait_for(release.wait(), 2)

        power = True
        service = SettingsService(
            load_config(path), apply, lambda _enabled: None, lambda: power, "controller-one"
        )
        socket_path = tmp_path / "control.sock"
        server = SettingsCommandServer(socket_path, service, True)
        server.ACK_TIMEOUT = 0.01
        await server.start()
        request = {
            "version": 1,
            "operation": "save-settings",
            "request_id": "slow-save",
            "source_id": "controller-one",
            "revision": 0,
            "patch": _patch(3.0),
        }
        try:
            pending = await _request(socket_path, request)
            assert pending["ok"] is False
            assert pending["pending"] is True
            assert pending["error"] == "SettingsPending"
            await asyncio.wait_for(started.wait(), 2)
            release.set()
            await _wait_for_revision(service, 1)
            final = await _request(socket_path, request)
            assert final["ok"] is True and final["revision"] == 1
            assert applied == 1
            assert load_config(path).collection.interval_seconds == 3.0
        finally:
            release.set()
            await server.close()

    asyncio.run(exercise())


def test_disconnected_slow_save_reconciles_and_duplicate_hydrates_final_result(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        path = _config_path(tmp_path)
        started = asyncio.Event()
        release = asyncio.Event()

        async def apply(_config: AppConfig, _revision: int) -> None:
            started.set()
            await asyncio.wait_for(release.wait(), 2)

        service = SettingsService(
            load_config(path), apply, lambda _enabled: None, lambda: True, "controller-one"
        )
        socket_path = tmp_path / "control.sock"
        server = SettingsCommandServer(socket_path, service, True)
        server.ACK_TIMEOUT = 0.01
        await server.start()
        request = {
            "version": 1,
            "operation": "save-settings",
            "request_id": "disconnected-save",
            "source_id": "controller-one",
            "revision": 0,
            "patch": _patch(3.5),
        }
        try:
            _reader, writer = await asyncio.open_unix_connection(str(socket_path))
            writer.write((json.dumps(request) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(started.wait(), 2)
            release.set()
            await _wait_for_revision(service, 1)
            final = await _request(socket_path, request)
            assert final["ok"] is True and final["revision"] == 1
            assert load_config(path).collection.interval_seconds == 3.5
        finally:
            release.set()
            await server.close()

    asyncio.run(exercise())


def test_command_response_is_bounded() -> None:
    path = Path("config.example.toml")
    service, _power = _service(path, [])
    server = SettingsCommandServer(Path("unused.sock"), service, True)
    encoded = server._encode_response({"ok": True, "settings": "x" * server.MAX_BYTES})
    assert len(encoded) <= server.MAX_BYTES
    assert json.loads(encoded)["error"] == "SettingsError"


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
