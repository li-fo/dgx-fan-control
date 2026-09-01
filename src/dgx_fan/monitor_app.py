"""Read-only Textual companion used by the browser monitor service."""

from __future__ import annotations

import argparse
import time
from asyncio import CancelledError, Task, create_task, open_unix_connection, sleep
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType

from .config import AppConfig, ConfigError, load_config, resolve_config_path
from .monitor import (
    MAX_MESSAGE_BYTES,
    MIN_PUBLISH_INTERVAL_SECONDS,
    MonitorProtocolError,
    MonitorState,
    decode_state,
)
from .ui import FanAppUI


class DGXFanMonitorApp(App[None]):
    """A browser/terminal view that cannot own or mutate fan hardware."""

    TITLE = "DGX Fan Controller · Read Only"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True)
    ]
    RECONNECT_SECONDS = 1.0
    WATCHDOG_SECONDS = 0.25

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self._receive_task: Task[None] | None = None
        self._last_revision = -1
        self._source_id: str | None = None
        self._last_fresh_received_at: float | None = None
        self._last_captured_at: float | None = None
        self._watchdog_timer: Any | None = None

    def compose(self) -> ComposeResult:
        yield FanAppUI(
            str(self.config.path),
            lambda: None,
            self.config.control.emergency_temperature_celsius,
            self.config.collection.interval_seconds,
            self.config.dashboard_colors,
            read_only=True,
        )

    def on_mount(self) -> None:
        self._receive_task = create_task(self._receive_loop())
        self._watchdog_timer = self.set_interval(self.WATCHDOG_SECONDS, self._watchdog)

    async def on_unmount(self) -> None:
        if self._watchdog_timer is not None:
            self._watchdog_timer.stop()
            self._watchdog_timer = None
        if self._receive_task is None:
            return
        self._receive_task.cancel()
        try:
            await self._receive_task
        except CancelledError:
            pass
        self._receive_task = None

    async def _receive_loop(self) -> None:
        socket_path = self.config.web.socket_path
        assert socket_path is not None
        ui = self.query_one(FanAppUI)
        while True:
            try:
                ui.set_monitor_transport_status("Monitor stream: CONNECTING")
                reader, writer = await open_unix_connection(
                    str(socket_path), limit=MAX_MESSAGE_BYTES
                )
                try:
                    ui.set_monitor_transport_status(None)
                    while line := await reader.readline():
                        try:
                            state = decode_state(line, self.config.collection.interval_seconds)
                        except MonitorProtocolError:
                            ui.set_monitor_transport_status("Monitor stream: INVALID MESSAGE")
                            continue
                        self._accept_state(ui, state)
                    ui.set_monitor_transport_status("Monitor stream: DISCONNECTED")
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except ConnectionError:
                        pass
            except CancelledError:
                raise
            except (ConnectionError, OSError, ValueError):
                ui.set_monitor_transport_status("Monitor stream: DISCONNECTED")
            await sleep(self.RECONNECT_SECONDS)

    def _accept_state(
        self, ui: FanAppUI, state: MonitorState, received_at: float | None = None
    ) -> bool:
        """Accept one ordered replacement state; return whether it was fresh."""
        # Keep the socket loop narrow while making source-reset and watchdog
        # semantics directly testable without a real browser connection.
        if state.source_id != self._source_id:
            self._source_id = state.source_id
            self._last_revision = -1
        if state.revision <= self._last_revision:
            return False
        self._last_revision = state.revision
        self._last_fresh_received_at = time.monotonic() if received_at is None else received_at
        self._last_captured_at = state.captured_at
        if state.transport_error is not None:
            ui.set_monitor_transport_status(f"Monitor stream: {state.transport_error}")
            return True
        assert state.snapshot is not None and state.history is not None
        ui.set_monitor_transport_status(None)
        ui.update_monitor_snapshot(state.snapshot, state.captured_at, state.history)
        return True

    def _watchdog(self, now: float | None = None) -> None:
        """Expose a stalled publisher even while the Unix socket stays open."""
        if self._last_fresh_received_at is None:
            return
        current = time.monotonic() if now is None else now
        timeout = max(
            MIN_PUBLISH_INTERVAL_SECONDS * 3,
            self.config.collection.interval_seconds * 3,
        )
        receive_stale = current - self._last_fresh_received_at > timeout
        captured_stale = (
            self._last_captured_at is not None
            and 0 <= current - self._last_captured_at > timeout
        )
        if receive_stale or captured_stale:
            self.query_one(FanAppUI).set_monitor_transport_status("Monitor stream: STALE")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only DGX fan browser monitor")
    parser.add_argument(
        "--config", help="TOML config path; overrides DGX_FAN_CONFIG and ./config.toml"
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(resolve_config_path(args.config))
    except ConfigError as error:
        raise SystemExit(f"dgx-fan-monitor: configuration error: {error}") from error
    if not config.web.enabled:
        raise SystemExit("dgx-fan-monitor: set web.enabled = true in config.toml first")
    DGXFanMonitorApp(config).run()
