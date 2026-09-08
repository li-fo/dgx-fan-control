from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Button, Input, Select

from dgx_fan.app import DGXFanApp
from dgx_fan.config import load_config
from dgx_fan.monitor_app import DGXFanMonitorApp
from dgx_fan.settings import SettingsCommandServer, SettingsService, control_socket_path
from dgx_fan.settings_ui import SettingsScreen
from dgx_fan.ui import FanAppUI


def _writable_config(tmp_path: Path) -> Path:
    source = Path("config.example.toml").read_text()
    source = source.replace("enabled = false", "enabled = true", 1)
    source = source.replace("allow_control = false", "allow_control = true", 1)
    source = source.replace("interval_seconds = 2.0", "interval_seconds = 0.2", 1)
    source = source.replace("timeout_seconds = 1.5", "timeout_seconds = 0.1", 1)
    source = source.replace("stale_after_seconds = 6.0", "stale_after_seconds = 0.4", 1)
    source = source.replace("retry_count = 3", "retry_count = 0", 1)
    path = tmp_path / "config.toml"
    path.write_text(source)
    return path


async def _wait_for_revision(service: SettingsService, revision: int) -> None:
    async with asyncio.timeout(2):
        while service.effective.revision != revision:
            await asyncio.sleep(0)


async def _wait_for_browser_control(app: DGXFanMonitorApp, pilot: Any) -> None:
    async with asyncio.timeout(3):
        while app.query_one("#fan-settings", Button).disabled:
            await pilot.pause(0.05)


def test_real_ipc_and_headless_browser_apply_settings_without_new_hardware(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        path = _writable_config(tmp_path)
        config = load_config(path)
        controller_app = DGXFanApp(config)
        monitor_app = DGXFanMonitorApp(config)
        socket_path = config.web.socket_path
        assert socket_path is not None
        poll_started = asyncio.Event()
        poll_cancelled = asyncio.Event()

        async def hold_collection(*_args: object, **_kwargs: object) -> object:
            poll_started.set()
            try:
                await asyncio.wait_for(asyncio.Event().wait(), 10)
            except asyncio.CancelledError:
                poll_cancelled.set()
                raise

        controller_app.collector.collect_endpoint = hold_collection  # type: ignore[method-assign, assignment]

        async with controller_app.run_test(size=(80, 24)) as controller_pilot:
            await controller_pilot.pause(0.3)
            await asyncio.wait_for(poll_started.wait(), 2)
            hardware = controller_app.hardware
            controller = controller_app.controller
            collector = controller_app.collector
            node_collector = controller_app.node_collector
            old_poll_tasks = (*controller_app._poll_tasks, *controller_app._node_poll_tasks)
            endpoint_id = config.endpoints[0].id
            collector._last_good[endpoint_id] = (10.0, ())
            collector._retrying[config.endpoints[0].id] = 1
            collector._errors[config.endpoints[0].id] = "prior failure"

            controller.set_power(False)

            async with monitor_app.run_test(size=(128, 37)) as browser_pilot:
                await _wait_for_browser_control(monitor_app, browser_pilot)
                assert monitor_app.query_one("#power-toggle", Button).disabled is False
                assert monitor_app.query_one("#fan-settings", Button).disabled is False

                initial = await monitor_app._command("read-settings", {})
                monitor_app._accept_settings_response(initial)
                revision = initial["revision"]
                source_id = initial["source_id"]
                assert isinstance(revision, int) and isinstance(source_id, str)
                response = await monitor_app._command(
                    "save-settings",
                    {
                        "source_id": source_id,
                        "revision": revision,
                        "patch": {
                            "collection": {
                                "interval_seconds": 0.3,
                                "timeout_seconds": 0.2,
                                "stale_after_seconds": 0.6,
                                "retry_count": 1,
                                "retry_delay_seconds": 0.1,
                            },
                            "control": {
                                "hysteresis_celsius": 3.0,
                                "emergency_temperature_celsius": 80.0,
                                "recovery_seconds": 12.0,
                            },
                            "hardware": {
                                "startup_boost_seconds": 2.0,
                                "stall_timeout_seconds": 6.0,
                                "shutdown_mode": "off",
                            },
                            "dashboard": {
                                "colors": {
                                    "memory": "green",
                                    "temperature": "blue",
                                }
                            },
                        },
                    },
                )
                monitor_app._accept_settings_response(response)

                assert response["revision"] == revision + 1
                assert controller_app.hardware is hardware
                assert controller_app.controller is controller
                assert controller_app.collector is collector
                assert controller_app.node_collector is node_collector
                assert collector._retrying == {}
                assert collector._errors[config.endpoints[0].id] == "prior failure"
                assert collector._last_good[endpoint_id] == (10.0, ())
                assert controller_app.controller.power is False
                assert controller_app.config.collection.interval_seconds == 0.3
                assert controller_app.config.control.hysteresis_celsius == 3.0
                assert controller_app.config.hardware.shutdown_mode == "off"
                assert controller_app.config.dashboard_colors.memory == "green"
                assert all(task.done() for task in old_poll_tasks)
                assert poll_cancelled.is_set()
                assert len(controller_app._poll_tasks) == len(config.endpoints)
                assert len(controller_app._node_poll_tasks) == len(controller_app.node_endpoints)
                assert not any(task in old_poll_tasks for task in controller_app._poll_tasks)
                local_ui = controller_app.query_one(FanAppUI)
                assert local_ui.history.collection_interval_seconds == 0.3
                assert local_ui.emergency_temperature == 80.0
                assert local_ui.dashboard_colors.temperature == "blue"

                await browser_pilot.pause(1.3)
                browser_ui = monitor_app.query_one(FanAppUI)
                assert monitor_app._settings_revision == revision + 1
                assert monitor_app._requested_power is False
                assert browser_ui.history.collection_interval_seconds == 0.3
                assert browser_ui.emergency_temperature == 80.0
                assert browser_ui.dashboard_colors.memory == "green"

                powered = await monitor_app._command(
                    "set-power",
                    {
                        "source_id": source_id,
                        "revision": revision + 1,
                        "enabled": True,
                    },
                )
                assert powered["power_enabled"] is True
                assert controller_app.controller.power is True
                assert controller_app.hardware is hardware

        assert not socket_path.exists()
        assert not control_socket_path(socket_path).exists()
        assert hardware.duties == (100, 100)  # type: ignore[attr-defined]
        hardware.release(normal_shutdown=True)
        assert hardware.duties == (0, 0)  # type: ignore[attr-defined]
        persisted = load_config(path)
        assert persisted.collection.interval_seconds == 0.3
        assert persisted.dashboard_colors.memory == "green"
        assert persisted.dashboard_colors.utilization is None

    asyncio.run(exercise())


def test_default_browser_process_remains_read_only() -> None:
    config = load_config(Path("config.example.toml"))
    assert config.web.allow_control is False
    app = DGXFanMonitorApp(replace(config, web=replace(config.web, enabled=True)))

    async def exercise() -> None:
        async with app.run_test(size=(80, 24)):
            assert app.query_one("#power-toggle", Button).disabled is True
            assert app.query_one("#fan-settings", Button).disabled is True

    asyncio.run(exercise())


def test_browser_reconciles_slow_command_and_two_clients_rehydrate_stale_revision(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        path = _writable_config(tmp_path)
        config = load_config(path)
        apply_started = asyncio.Event()
        apply_release = asyncio.Event()
        apply_count = 0

        async def apply(_config: object, _revision: int) -> None:
            nonlocal apply_count
            apply_count += 1
            if apply_count == 1:
                apply_started.set()
                await asyncio.wait_for(apply_release.wait(), 2)

        power = {"enabled": True}
        service = SettingsService(
            config,
            apply,  # type: ignore[arg-type]
            lambda enabled: power.__setitem__("enabled", enabled),
            lambda: power["enabled"],
            "settings-one",
        )
        socket_path = config.web.socket_path
        assert socket_path is not None
        server = SettingsCommandServer(control_socket_path(socket_path), service, True)
        server.ACK_TIMEOUT = 0.01
        await server.start()
        first = DGXFanMonitorApp(config)
        second = DGXFanMonitorApp(config)
        first.COMMAND_RESPONSE_TIMEOUT = second.COMMAND_RESPONSE_TIMEOUT = 0.1
        first.COMMAND_RECONCILE_TIMEOUT = second.COMMAND_RECONCILE_TIMEOUT = 1.0
        try:
            initial_first = await first._command("read-settings", {})
            first._accept_settings_response(initial_first)
            initial_second = await second._command("read-settings", {})
            second._accept_settings_response(initial_second)

            save_task = asyncio.create_task(
                first._command(
                    "save-settings",
                    {
                        "source_id": "settings-one",
                        "revision": 0,
                        "patch": {"collection": {"interval_seconds": 0.35}},
                    },
                )
            )
            await asyncio.wait_for(apply_started.wait(), 2)
            await asyncio.sleep(0.03)
            assert not save_task.done()
            apply_release.set()
            saved = await save_task
            first._accept_settings_response(saved)
            assert saved["revision"] == 1 and apply_count == 1

            with pytest.raises(RuntimeError, match="reload before saving"):
                await second._command(
                    "save-settings",
                    {
                        "source_id": "settings-one",
                        "revision": 0,
                        "patch": {"collection": {"interval_seconds": 0.4}},
                    },
                )
            hydrated = await second._command("read-settings", {})
            second._accept_settings_response(hydrated)
            assert second._settings_revision == 1
            saved_second = await second._command(
                "save-settings",
                {
                    "source_id": "settings-one",
                    "revision": 1,
                    "patch": {"collection": {"interval_seconds": 0.4}},
                },
            )
            assert saved_second["revision"] == 2
            assert load_config(path).collection.interval_seconds == 0.4
        finally:
            apply_release.set()
            await server.close()

    asyncio.run(exercise())


def test_browser_reports_uncertain_slow_save_after_authoritative_revision_recovery(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        path = _writable_config(tmp_path)
        config = load_config(path)
        apply_started = asyncio.Event()
        apply_release = asyncio.Event()

        async def apply(_config: object, _revision: int) -> None:
            apply_started.set()
            await asyncio.wait_for(apply_release.wait(), 2)

        service = SettingsService(
            config,
            apply,  # type: ignore[arg-type]
            lambda _enabled: None,
            lambda: True,
            "settings-one",
        )
        socket_path = config.web.socket_path
        assert socket_path is not None
        server = SettingsCommandServer(control_socket_path(socket_path), service, True)
        server.ACK_TIMEOUT = 0.005
        await server.start()
        browser = DGXFanMonitorApp(config)
        browser.COMMAND_RESPONSE_TIMEOUT = 0.1
        browser.COMMAND_RECONCILE_TIMEOUT = 0.03
        browser.COMMAND_RETRY_SECONDS = 0.005
        save = asyncio.create_task(
            browser._command(
                "save-settings",
                {
                    "source_id": "settings-one",
                    "revision": 0,
                    "patch": {"collection": {"interval_seconds": 0.25}},
                },
            )
        )
        try:
            await asyncio.wait_for(apply_started.wait(), 2)
            with pytest.raises(RuntimeError, match="outcome is still uncertain.*revision 0"):
                await save
            assert browser._settings_source_id == "settings-one"
            assert browser._settings_revision == 0
            apply_release.set()
            await _wait_for_revision(service, 1)
            assert service.effective.revision == 1
            assert load_config(path).collection.interval_seconds == 0.25
        finally:
            apply_release.set()
            await server.close()

    asyncio.run(exercise())


def test_local_and_browser_modals_save_and_browser_power_button_uses_real_socket(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        path = _writable_config(tmp_path)
        config = load_config(path)
        controller = DGXFanApp(config)
        browser = DGXFanMonitorApp(config)

        async with controller.run_test(size=(128, 37)) as controller_pilot:
            await controller_pilot.pause(0.3)
            async with browser.run_test(size=(128, 37)) as browser_pilot:
                await _wait_for_browser_control(browser, browser_pilot)
                browser.query_one("#fan-settings", Button).press()
                await browser_pilot.pause()
                assert isinstance(browser.screen, SettingsScreen)
                browser.screen.query_one("#setting-collection-interval_seconds", Input).value = "0.31"
                browser.screen.query_one("#setting-control-fan-mode", Select).value = "linked"
                browser.screen.query_one("#setting-save", Button).press()
                await browser_pilot.pause(0.5)
                assert not isinstance(browser.screen, SettingsScreen)
                assert controller.config.collection.interval_seconds == 0.31
                assert controller.config.control.fan_mode == "linked"

                controller.query_one("#fan-settings", Button).press()
                await controller_pilot.pause()
                assert isinstance(controller.screen, SettingsScreen)
                controller.screen.query_one("#setting-control-hysteresis_celsius", Input).value = "3.5"
                controller.screen.query_one("#setting-save", Button).press()
                await controller_pilot.pause(0.5)
                assert not isinstance(controller.screen, SettingsScreen)
                assert controller.config.control.hysteresis_celsius == 3.5

                before = controller.controller.power
                browser.query_one("#power-toggle", Button).press()
                await browser_pilot.pause(0.5)
                assert controller.controller.power is not before

    asyncio.run(exercise())


def test_browser_modal_retains_draft_when_fresh_control_connection_is_lost(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        path = _writable_config(tmp_path)
        config = load_config(path)
        controller = DGXFanApp(config)
        browser = DGXFanMonitorApp(config)
        async with controller.run_test(size=(128, 37)) as controller_pilot:
            await controller_pilot.pause(0.3)
            async with browser.run_test(size=(128, 37)) as browser_pilot:
                await _wait_for_browser_control(browser, browser_pilot)
                browser.query_one("#fan-settings", Button).press()
                await browser_pilot.pause()
                assert isinstance(browser.screen, SettingsScreen)
                field = browser.screen.query_one(
                    "#setting-control-max_speed_percent", Input
                )
                field.value = "81"
                assert browser._receive_task is not None
                browser._receive_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await browser._receive_task
                browser._receive_task = None
                browser._set_phase(browser.query_one(FanAppUI), "DISCONNECTED")
                browser.screen.query_one("#setting-save", Button).press()
                await browser_pilot.pause()
                assert isinstance(browser.screen, SettingsScreen)
                assert field.value == "81"
                assert "connection was lost" in str(
                    browser.screen.query_one("#settings-error").render()
                )

    asyncio.run(exercise())


def test_shutdown_commands_safe_full_before_slow_save_reconciliation(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        path = _writable_config(tmp_path)
        config = load_config(path)
        app = DGXFanApp(config)
        app.SHUTDOWN_RECONCILE_SECONDS = 0.02
        entered = threading.Event()
        release = threading.Event()
        real_persist = app.settings._persist

        def slow_persist(patch: dict[str, object], fingerprint: str) -> object:
            entered.set()
            release.wait(2)
            return real_persist(patch, fingerprint)

        app.settings._persist = slow_persist  # type: ignore[method-assign, assignment]
        save: asyncio.Task[object] | None = None
        hardware = None
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.2)
            hardware = app.hardware
            assert hardware is not None
            hardware.set_duties((20, 30))
            save = asyncio.create_task(
                app.settings.save(
                    {"hardware": {"shutdown_mode": "off"}},
                    app.settings.effective.revision,
                )
            )
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0)
        assert hardware.duties == (100, 100)  # type: ignore[attr-defined]
        assert app._settings_reconciled_on_close is False
        release.set()
        assert save is not None
        await save
        assert hardware.duties == (100, 100)  # type: ignore[attr-defined]
        hardware.release(normal_shutdown=True)
        assert hardware.duties == (0, 0)  # type: ignore[attr-defined]

    asyncio.run(exercise())
