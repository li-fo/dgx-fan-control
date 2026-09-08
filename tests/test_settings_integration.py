from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from textual.widgets import Button

from dgx_fan.app import DGXFanApp
from dgx_fan.config import load_config
from dgx_fan.monitor_app import DGXFanMonitorApp
from dgx_fan.settings import control_socket_path
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

        async with controller_app.run_test(size=(80, 24)) as controller_pilot:
            await controller_pilot.pause(0.3)
            hardware = controller_app.hardware
            controller = controller_app.controller
            collector = controller_app.collector
            node_collector = controller_app.node_collector
            old_poll_tasks = (*controller_app._poll_tasks, *controller_app._node_poll_tasks)
            for task in old_poll_tasks:
                task.cancel()
            await asyncio.gather(*old_poll_tasks, return_exceptions=True)
            endpoint_id = config.endpoints[0].id
            collector._last_good[endpoint_id] = (10.0, ())
            collector._retrying[config.endpoints[0].id] = 1
            collector._errors[config.endpoints[0].id] = "prior failure"

            async def hold_collection(*_args: object, **_kwargs: object) -> object:
                await asyncio.Event().wait()

            collector.collect_endpoint = hold_collection  # type: ignore[method-assign, assignment]
            controller.set_power(False)

            async with monitor_app.run_test(size=(128, 37)) as browser_pilot:
                await browser_pilot.pause(0.5)
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
                                "shutdown_mode": "full",
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
                assert controller_app.config.hardware.shutdown_mode == "full"
                assert controller_app.config.dashboard_colors.memory == "green"
                assert all(task.done() for task in old_poll_tasks)
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
