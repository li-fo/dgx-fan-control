"""Read-only Textual companion used by the browser monitor service."""

from __future__ import annotations

import argparse
from asyncio import CancelledError, Task, create_task, open_unix_connection, sleep
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType

from .config import AppConfig, ConfigError, load_config, resolve_config_path
from .monitor import MAX_MESSAGE_BYTES, MonitorProtocolError, decode_state
from .ui import FanAppUI


class DGXFanMonitorApp(App[None]):
    """A browser/terminal view that cannot own or mutate fan hardware."""

    TITLE = "DGX Fan Controller · Read Only"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True)
    ]
    RECONNECT_SECONDS = 1.0

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self._receive_task: Task[None] | None = None
        self._last_revision = -1
        self._source_id: str | None = None

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

    async def on_unmount(self) -> None:
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
                        if state.source_id != self._source_id:
                            self._source_id = state.source_id
                            self._last_revision = -1
                        if state.revision <= self._last_revision:
                            continue
                        self._last_revision = state.revision
                        ui.set_monitor_transport_status(None)
                        ui.update_monitor_snapshot(state.snapshot, state.captured_at, state.history)
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
