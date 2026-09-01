from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from test_monitor import _state
from textual.widgets import Button, Static

from dgx_fan.config import WebConfig, load_config
from dgx_fan.monitor import MonitorPublisher
from dgx_fan.monitor_app import DGXFanMonitorApp
from dgx_fan.ui import FanAppUI


async def _stop_owned_app_service(service: Any, *, stop_timeout: float = 4, process_timeout: float = 2) -> None:
    """Bound textual-serve shutdown without touching processes it does not own."""
    stop_task = asyncio.create_task(service.stop())
    try:
        await asyncio.wait_for(asyncio.shield(stop_task), timeout=stop_timeout)
        return
    except TimeoutError:
        process = service._process
        assert process is not None
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=process_timeout)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=process_timeout)
        try:
            await asyncio.wait_for(asyncio.shield(stop_task), timeout=process_timeout)
        except TimeoutError:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)


def test_monitor_app_hydrates_read_only_ui_without_hardware(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = load_config(Path("config.example.toml"))
        socket_path = tmp_path / "monitor.sock"
        config = replace(config, web=WebConfig(True, "127.0.0.1", 8000, socket_path))
        publisher = MonitorPublisher(socket_path)
        await publisher.start()
        state = _state()
        publisher.publish(state.snapshot, state.captured_at)
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


def test_monitor_app_accepts_rpm_only_updates_and_recovers_from_stall(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = load_config(Path("config.example.toml"))
        config = replace(config, web=WebConfig(True, "127.0.0.1", 8000, tmp_path / "monitor.sock"))
        app = DGXFanMonitorApp(config)
        first = _state(1)
        assert first.snapshot is not None
        second_snapshot = replace(
            first.snapshot,
            fans=(replace(first.snapshot.fans[0], rpm=1234), first.snapshot.fans[1]),
        )
        second = replace(first, revision=2, captured_at=101.0, snapshot=second_snapshot)
        reset = replace(second, source_id="after-reconnect", revision=1, captured_at=102.0)
        async with app.run_test(size=(100, 30)) as pilot:
            ui = app.query_one(FanAppUI)
            assert app._accept_state(ui, first, received_at=10.0)
            assert app._accept_state(ui, second, received_at=11.0)
            assert ui.snapshot is not None and ui.snapshot.fans[0].rpm == 1234
            app._watchdog(now=20.0)
            assert ui.monitor_transport_status == "Monitor stream: STALE"
            # A source restart legitimately resets the revision sequence.
            assert app._accept_state(ui, reset, received_at=21.0)
            assert ui.monitor_transport_status is None
            await pilot.pause()

    asyncio.run(exercise())


def test_monitor_app_shows_retry_state_from_fresh_cached_snapshot(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = load_config(Path("config.example.toml"))
        config = replace(config, web=WebConfig(True, "127.0.0.1", 8000, tmp_path / "monitor.sock"))
        app = DGXFanMonitorApp(config)
        state = _state()
        assert state.snapshot is not None
        endpoint = replace(
            state.snapshot.endpoint_snapshots[0],
            age_seconds=2.5,
            retrying=True,
            retry_attempt=2,
            retry_count=3,
        )
        retry = replace(state, snapshot=replace(state.snapshot, endpoint_snapshots=(endpoint,)))
        async with app.run_test(size=(100, 30)):
            ui = app.query_one(FanAppUI)
            assert app._accept_state(ui, retry, received_at=10.0)
            assert "RETRYING 2/3" in app.query_one("#error-banner", Static).render().plain

    asyncio.run(exercise())


def test_monitor_watchdog_preserves_disconnected_phase(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = load_config(Path("config.example.toml"))
        app = DGXFanMonitorApp(replace(config, web=WebConfig(True, "127.0.0.1", 8000, tmp_path / "x.sock")))
        async with app.run_test():
            ui = app.query_one(FanAppUI)
            app._last_fresh_received_at = 0.0
            app._last_captured_at = 0.0
            app._set_phase(ui, "DISCONNECTED")
            app._watchdog(now=100.0)
            assert ui.monitor_transport_status == "Monitor stream: DISCONNECTED"
            assert app._accept_state(ui, _state(), received_at=101.0)
            assert ui.monitor_transport_status is None

    asyncio.run(exercise())


def test_web_launcher_is_valid_shell_and_uses_user_transient_service() -> None:
    script = Path("web.sh")
    result = subprocess.run(["bash", "-n", str(script)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    source = script.read_text()
    assert "systemd-run --user --collect" in source
    assert "textual_serve.server import Server" in source
    assert "display.sh" not in source and "start.sh" not in source


def test_textual_serve_app_service_receives_monitor_first_frame(tmp_path: Path) -> None:
    """Exercise the real textual-serve subprocess contract without a browser."""

    async def exercise() -> None:
        from textual_serve.app_service import AppService
        from textual_serve.download_manager import DownloadManager

        config_path = tmp_path / "config.toml"
        config_path.write_text(Path("config.example.toml").read_text().replace("enabled = false", "enabled = true"))
        packets: list[bytes] = []

        async def write_bytes(packet: bytes) -> None:
            packets.append(packet)

        async def write_str(_packet: str) -> None:
            return None

        async def close() -> None:
            return None

        program = "from dgx_fan.monitor_app import main; main()"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(program)} --config {shlex.quote(str(config_path))}"
        service = AppService(
            command,
            write_bytes=write_bytes,
            write_str=write_str,
            close=close,
            download_manager=DownloadManager(),
        )
        async def wait_for_first_frame() -> None:
            while b"DGX Dashboard" not in b"".join(packets):
                await asyncio.sleep(0.05)

        try:
            await asyncio.wait_for(service.start(100, 30), timeout=4)
            await asyncio.wait_for(wait_for_first_frame(), timeout=4)
            assert packets, "textual-serve did not receive a first monitor frame"
            rendered = b"".join(packets)
            assert b"DGX Dashboard" in rendered
            assert b"Fan Control" in rendered
            assert b"READ ONLY" in rendered
        finally:
            await _stop_owned_app_service(service)

    asyncio.run(exercise())


def test_app_service_timeout_cleanup_terminates_only_the_owned_child() -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminate_calls = 0
            self.kill_calls = 0

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1

        async def wait(self) -> int:
            if self.kill_calls == 0:
                await asyncio.Future()
            self.returncode = 9
            return self.returncode

    class HangingService:
        def __init__(self, process: FakeProcess) -> None:
            self._process = process

        async def stop(self) -> None:
            await asyncio.Future()

    async def exercise() -> None:
        owned = FakeProcess()
        unrelated = FakeProcess()
        await _stop_owned_app_service(HangingService(owned), stop_timeout=0.01, process_timeout=0.01)
        assert owned.terminate_calls == 1
        assert owned.kill_calls == 1
        assert owned.returncode == 9
        assert unrelated.terminate_calls == 0
        assert unrelated.kill_calls == 0

    asyncio.run(exercise())
