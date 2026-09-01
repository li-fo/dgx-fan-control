from __future__ import annotations

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

from test_monitor import _state
from textual.widgets import Button, Static

from dgx_fan.config import WebConfig, load_config
from dgx_fan.monitor import MonitorPublisher
from dgx_fan.monitor_app import DGXFanMonitorApp
from dgx_fan.ui import FanAppUI


def test_monitor_app_hydrates_read_only_ui_without_hardware(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = load_config(Path("config.example.toml"))
        socket_path = tmp_path / "monitor.sock"
        config = replace(config, web=WebConfig(True, "127.0.0.1", 8000, socket_path))
        publisher = MonitorPublisher(socket_path)
        await publisher.start()
        state = _state()
        publisher.publish(state.snapshot, state.history, state.captured_at)
        app = DGXFanMonitorApp(config)
        try:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(delay=0.4)
                ui = app.query_one(FanAppUI)
                assert ui.read_only is True
                assert app.query_one("#read-only-indicator", Static).render().plain == "READ ONLY"
                assert app.query_one("#power-toggle", Button).disabled is True
                assert app.query_one("#fan-settings", Button).disabled is True
                assert ui.snapshot == state.snapshot
                assert ui.history.last_seen == {("dgx-1", "gpu-0"): 100.0}
        finally:
            await publisher.close()

    asyncio.run(exercise())


def test_monitor_app_has_no_controller_or_hardware_import() -> None:
    source = Path("src/dgx_fan/monitor_app.py").read_text()
    assert "from .app import" not in source
    assert "from .hardware import" not in source
    assert "from .controller import" not in source
    assert "from .dcgm import" not in source


def test_web_launcher_is_valid_shell_and_uses_user_transient_service() -> None:
    script = Path("web.sh")
    result = subprocess.run(["bash", "-n", str(script)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    source = script.read_text()
    assert "systemd-run --user --collect" in source
    assert "textual_serve.server import Server" in source
    assert "display.sh" not in source and "start.sh" not in source
