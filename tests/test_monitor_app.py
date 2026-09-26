from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from test_monitor import _state
from textual.widgets import Button, Static

from dgx_fan.config import DashboardRanges, MetricRange, WebConfig, load_config
from dgx_fan.monitor import MonitorPublisher, decode_state, encode_state
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
                ui = app.query_one(FanAppUI)
                async with asyncio.timeout(2):
                    while ui.snapshot is None:
                        await pilot.pause(delay=0.05)
                assert ui.read_only is True
                assert app.query_one("#read-only-indicator", Static).render().plain == "READ ONLY"
                assert app.query_one("#power-toggle", Button).disabled is True
                assert app.query_one("#fan-settings", Button).disabled is True
                assert ui.snapshot == state.snapshot
                assert ui.history.last_seen == {
                    ("dgx-1", "gpu-0"): 100.0,
                    ("dgx-1", "__weighted__"): 100.0,
                }
        finally:
            await publisher.close()

    asyncio.run(exercise())
def test_monitor_app_has_no_controller_or_hardware_import() -> None:
    source = Path("src/dgx_fan/monitor_app.py").read_text()
    assert "from .app import" not in source
    assert "from .hardware import" not in source
    assert "from .controller import" not in source
    assert "from .dcgm import" not in source


def test_monitor_app_source_ranges_replace_local_and_reset_on_old_frame(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = load_config(Path("config.example.toml"))
        config = replace(
            config,
            web=WebConfig(True, "127.0.0.1", 8000, tmp_path / "monitor.sock"),
            graph_view="graph-2",
            dashboard_ranges=DashboardRanges(power=MetricRange(0, 90)),
        )
        app = DGXFanMonitorApp(config)
        source = replace(
            _state(), dashboard_ranges=DashboardRanges(power=MetricRange(20, 120)),
        )
        async with app.run_test(size=(85, 25)) as pilot:
            ui = app.query_one(FanAppUI)
            assert app._accept_state(ui, decode_state(encode_state(source), 2), received_at=10)
            await pilot.pause()
            assert app.config.dashboard_ranges.power == MetricRange(20, 120)
            assert ui.graph_two_sparklines[("dgx-1", "power")].maximum == 120

            # An older publisher omits the additive field. Its source defaults
            # override both the prior source and this web process's local file.
            import json

            payload = json.loads(encode_state(source))
            del payload["dashboard_ranges"]
            payload["source_id"] = "new-source"
            payload["revision"] = 1
            old_frame = decode_state((json.dumps(payload) + "\n").encode(), 2)
            assert app._accept_state(ui, old_frame, received_at=11)
            await pilot.pause()
            assert app.config.dashboard_ranges == DashboardRanges()
            assert ui.graph_two_sparklines[("dgx-1", "power")].maximum == 240
            assert ui.graph_two_metric_labels[("dgx-1", "power")].render().plain.endswith("0–240 W")

    asyncio.run(exercise())


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


def test_writable_browser_controls_require_fresh_compatible_controller_state(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        config = load_config(Path("config.example.toml"))
        config = replace(
            config,
            web=WebConfig(
                enabled=True,
                host="127.0.0.1",
                port=8000,
                socket_path=tmp_path / "monitor.sock",
                allow_control=True,
            ),
        )
        app = DGXFanMonitorApp(config)
        async with app.run_test(size=(100, 30)):
            ui = app.query_one(FanAppUI)
            power = app.query_one("#power-toggle", Button)
            settings = app.query_one("#fan-settings", Button)
            assert power.disabled and settings.disabled

            # A legacy frame has telemetry but no compatible control metadata.
            assert app._accept_state(ui, _state(), received_at=10.0)
            assert power.disabled and settings.disabled

            compatible = replace(
                _state(2),
                settings_source_id="settings-one",
                settings_revision=4,
                power_enabled=True,
                control_available=True,
            )
            assert app._accept_state(ui, compatible, received_at=11.0)
            assert not power.disabled and not settings.disabled

            app._set_phase(ui, "DISCONNECTED")
            assert power.disabled and settings.disabled

            reconnected = replace(
                compatible,
                source_id="monitor-two",
                revision=1,
                settings_source_id="settings-two",
                settings_revision=0,
            )
            assert app._accept_state(ui, reconnected, received_at=12.0)
            assert not power.disabled and not settings.disabled

            app._watchdog(now=20.0)
            assert power.disabled and settings.disabled

            missing = replace(
                reconnected,
                revision=2,
                settings_source_id=None,
                settings_revision=None,
                power_enabled=None,
            )
            assert app._accept_state(ui, missing, received_at=21.0)
            assert power.disabled and settings.disabled

    asyncio.run(exercise())


def test_web_launcher_is_valid_shell_and_uses_user_transient_service() -> None:
    script = Path("scripts/web.sh")
    result = subprocess.run(["bash", "-n", str(script)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    source = script.read_text()
    assert "systemd-run --user --collect" in source
    assert "dgx_fan.web_server import RequestOriginServer" in source
    assert "display.sh" not in source and "start.sh" not in source


def test_web_launcher_stop_treats_only_collected_unit_absence_as_success(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    script = project / "scripts/web.sh"
    script.parent.mkdir()
    script.write_text(Path("scripts/web.sh").read_text())
    script.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$2\" >> \"$DGX_TEST_WEB_LOG\"\n"
        "case \"$2\" in\n"
        "  show) printf '%s\\n' \"${DGX_TEST_LOAD_STATE:-not-found}\"; exit \"${DGX_TEST_SHOW_STATUS:-0}\";;\n"
        "  stop) exit \"${DGX_TEST_STOP_STATUS:-0}\";;\n"
        "esac\n"
    )
    systemctl.chmod(0o755)
    command_log = tmp_path / "systemctl.log"
    environment = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "DGX_TEST_WEB_LOG": str(command_log)}

    absent = subprocess.run(
        ["bash", str(script), "stop"], cwd=project, env=environment, text=True, capture_output=True, check=False
    )
    assert absent.returncode == 0, absent.stderr
    assert command_log.read_text().splitlines() == ["show"]

    command_log.unlink()
    stopped = subprocess.run(
        ["bash", str(script), "stop"],
        cwd=project,
        env={**environment, "DGX_TEST_LOAD_STATE": "loaded"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert stopped.returncode == 0, stopped.stderr
    assert command_log.read_text().splitlines() == ["show", "stop"]

    command_log.unlink()
    show_failure = subprocess.run(
        ["bash", str(script), "stop"],
        cwd=project,
        env={**environment, "DGX_TEST_SHOW_STATUS": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert show_failure.returncode == 1
    assert "could not determine browser monitor service state" in show_failure.stderr
    assert command_log.read_text().splitlines() == ["show"]

    command_log.unlink()
    stop_failure = subprocess.run(
        ["bash", str(script), "stop"],
        cwd=project,
        env={**environment, "DGX_TEST_LOAD_STATE": "loaded", "DGX_TEST_STOP_STATUS": "5"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert stop_failure.returncode == 5
    assert command_log.read_text().splitlines() == ["show", "stop"]


def test_web_launcher_prints_configured_access_urls_only_after_service_start(tmp_path: Path) -> None:
    """The launcher reports browser-facing URLs without changing service arguments."""

    project = tmp_path / "project"
    project.mkdir()
    script = project / "scripts/web.sh"
    script.parent.mkdir()
    script.write_text(Path("scripts/web.sh").read_text())
    script.chmod(0o755)
    (project / "config.toml").write_text(
        '[web]\nenabled = true\nhost = "0.0.0.0"\nport = 8123\n'
    )
    virtual_bin = project / ".venv" / "bin"
    virtual_bin.mkdir(parents=True)
    for name, body in {
        "python": '#!/bin/sh\nprintf "%s\\n%s\\n" "${DGX_TEST_WEB_HOST:-0.0.0.0}" "${DGX_TEST_WEB_PORT:-8123}"\n',
        "dgx-fan-monitor": "#!/bin/sh\nexit 0\n",
    }.items():
        command = virtual_bin / name
        command.write_text(body)
        command.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in {
        "systemctl": '#!/bin/sh\nif [ "$2" = is-active ]; then exit 3; fi\nexit 0\n',
        "systemd-run": '#!/bin/sh\nexit "${DGX_TEST_SYSTEMD_RUN_STATUS:-0}"\n',
        "ip": "#!/bin/sh\nprintf '%s\\n' '2: eth0    inet 192.168.1.7/24 scope global eth0'\n",
    }.items():
        command = fake_bin / name
        command.write_text(body)
        command.chmod(0o755)

    environment = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    started = subprocess.run(
        ["bash", str(script), "start"],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert started.returncode == 0, started.stderr
    assert started.stdout == "Browser monitor: http://192.168.1.7:8123/\n"

    failed = subprocess.run(
        ["bash", str(script), "start"],
        cwd=project,
        env={**environment, "DGX_TEST_SYSTEMD_RUN_STATUS": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert failed.returncode == 1
    assert "Browser monitor:" not in failed.stdout


def test_web_launcher_formats_loopback_and_wildcard_fallback_urls(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    script = project / "scripts/web.sh"
    script.parent.mkdir()
    script.write_text(Path("scripts/web.sh").read_text())
    script.chmod(0o755)
    (project / "config.toml").write_text('[web]\nenabled = true\nhost = "::1"\nport = 9000\n')
    virtual_bin = project / ".venv" / "bin"
    virtual_bin.mkdir(parents=True)
    for name, body in {
        "python": '#!/bin/sh\nprintf "%s\\n%s\\n" "${DGX_TEST_WEB_HOST}" "${DGX_TEST_WEB_PORT}"\n',
        "dgx-fan-monitor": "#!/bin/sh\nexit 0\n",
    }.items():
        command = virtual_bin / name
        command.write_text(body)
        command.chmod(0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in {
        "systemctl": '#!/bin/sh\nif [ "$2" = is-active ]; then exit 3; fi\nexit 0\n',
        "systemd-run": "#!/bin/sh\nexit 0\n",
        "ip": "#!/bin/sh\nexit 0\n",
    }.items():
        command = fake_bin / name
        command.write_text(body)
        command.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DGX_TEST_WEB_HOST": "::1",
        "DGX_TEST_WEB_PORT": "9000",
    }
    loopback = subprocess.run(
        ["bash", str(script), "restart"],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert loopback.returncode == 0, loopback.stderr
    assert loopback.stdout == "Browser monitor: http://[::1]:9000/\n"

    wildcard = subprocess.run(
        ["bash", str(script), "start"],
        cwd=project,
        env={**environment, "DGX_TEST_WEB_HOST": "0.0.0.0", "DGX_TEST_WEB_PORT": "8123"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert wildcard.returncode == 0, wildcard.stderr
    assert wildcard.stdout == "Browser monitor: http://<this-pi-ip>:8123/\n"


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
