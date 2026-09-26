"""Textual companion used by the browser monitor and opt-in control service."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from asyncio import CancelledError, Task, create_task, open_unix_connection, sleep
from dataclasses import replace
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.css.query import NoMatches

from .config import AppConfig, ConfigError, load_config, resolve_config_path
from .monitor import (
    MAX_MESSAGE_BYTES,
    MIN_PUBLISH_INTERVAL_SECONDS,
    MonitorProtocolError,
    MonitorState,
    decode_state,
)
from .settings import MAX_CONTROL_MESSAGE_BYTES, control_socket_path
from .settings_ui import SettingsScreen
from .ui import FanAppUI


class DGXFanMonitorApp(App[None]):
    """A browser/terminal view that never owns or directly mutates fan hardware."""

    TITLE = "DGX Fan Controller · Read Only"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True)
    ]
    RECONNECT_SECONDS = 1.0
    WATCHDOG_SECONDS = 0.25
    COMMAND_CONNECT_TIMEOUT = 3.0
    COMMAND_RESPONSE_TIMEOUT = 4.0
    COMMAND_HISTORY_RESPONSE_TIMEOUT = 20.0
    COMMAND_RECONCILE_TIMEOUT = 20.0
    COMMAND_RETRY_SECONDS = 0.1

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self._receive_task: Task[None] | None = None
        self._last_revision = -1
        self._source_id: str | None = None
        self._last_fresh_received_at: float | None = None
        self._last_captured_at: float | None = None
        self._watchdog_timer: Any | None = None
        self._phase = "DISCONNECTED"
        self._shutting_down = False
        self._settings_source_id: str | None = None
        self._stream_settings_source_id: str | None = None
        self._settings_revision: int | None = None
        self._requested_power: bool | None = None
        self._control_stream_fresh = False
        if config.web.allow_control:
            self.title = "DGX Fan Controller · Control Enabled"

    def compose(self) -> ComposeResult:
        yield FanAppUI(
            str(self.config.path),
            self.toggle_power,
            self.config.control.emergency_temperature_celsius,
            self.config.collection.interval_seconds,
            self.config.dashboard_colors,
            self.open_settings,
            graph_view=self.config.graph_view,
            dashboard_ranges=self.config.dashboard_ranges,
            history_query=self.query_history,
            history_endpoints=self.config.endpoints,
            # Opt-in alone is insufficient: a compatible, fresh controller
            # frame must prove that the command endpoint is authoritative.
            read_only=True,
        )

    def on_mount(self) -> None:
        self._receive_task = create_task(self._receive_loop())
        self._watchdog_timer = self.set_interval(self.WATCHDOG_SECONDS, self._watchdog)

    async def on_unmount(self) -> None:
        self._shutting_down = True
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
                self._set_phase(ui, "CONNECTING")
                reader, writer = await open_unix_connection(
                    str(socket_path), limit=MAX_MESSAGE_BYTES
                )
                try:
                    self._phase = "CONNECTED"
                    while line := await reader.readline():
                        try:
                            state = decode_state(line, self.config.collection.interval_seconds)
                        except MonitorProtocolError:
                            ui.set_monitor_transport_status("Monitor stream: INVALID MESSAGE")
                            continue
                        self._accept_state(ui, state)
                    self._set_phase(ui, "DISCONNECTED")
                finally:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except ConnectionError:
                        pass
            except CancelledError:
                raise
            except (ConnectionError, OSError, ValueError):
                self._set_phase(ui, "DISCONNECTED")
            await sleep(self.RECONNECT_SECONDS)

    def _accept_state(
        self, ui: FanAppUI, state: MonitorState, received_at: float | None = None
    ) -> bool:
        """Accept one ordered replacement state; return whether it was fresh."""
        if self._shutting_down:
            return False
        # Keep the socket loop narrow while making source-reset and watchdog
        # semantics directly testable without a real browser connection.
        if state.source_id != self._source_id:
            self._clear_control_state(ui)
            self._source_id = state.source_id
            self._last_revision = -1
        if state.revision <= self._last_revision:
            return False
        self._last_revision = state.revision
        self._phase = "CONNECTED"
        self._last_fresh_received_at = time.monotonic() if received_at is None else received_at
        self._last_captured_at = state.captured_at
        if state.transport_error is not None:
            self._clear_control_state(ui)
            ui.set_monitor_transport_status(f"Monitor stream: {state.transport_error}")
            return True
        assert state.snapshot is not None and state.history is not None
        if (
            isinstance(state.settings_source_id, str)
            and bool(state.settings_source_id)
            and isinstance(state.settings_revision, int)
            and not isinstance(state.settings_revision, bool)
            and isinstance(state.power_enabled, bool)
            and state.control_available is True
        ):
            self._settings_source_id = state.settings_source_id
            self._stream_settings_source_id = state.settings_source_id
            self._settings_revision = state.settings_revision
            self._requested_power = state.power_enabled
            self._control_stream_fresh = True
        else:
            self._clear_control_state(ui)
        interval = state.collection_interval_seconds
        emergency = state.emergency_temperature_celsius
        colors = state.dashboard_colors
        graph_view = state.graph_view
        ranges = state.dashboard_ranges
        if interval is not None:
            self.config = replace(
                self.config,
                collection=replace(self.config.collection, interval_seconds=interval),
            )
            state.history.collection_interval_seconds = interval
        if emergency is not None:
            self.config = replace(
                self.config,
                control=replace(
                    self.config.control, emergency_temperature_celsius=emergency
                ),
            )
        if colors is not None or graph_view is not None:
            self.config = replace(
                self.config,
                dashboard_colors=colors or self.config.dashboard_colors,
                graph_view=graph_view or self.config.graph_view,
            )
        # The primary's replacement frame owns Graph #2 scale. An old frame
        # without this additive field decodes to defaults, never local overrides.
        self.config = replace(self.config, dashboard_ranges=ranges)
        ui.reconfigure_display(
            self.config.control.emergency_temperature_celsius,
            self.config.collection.interval_seconds,
            self.config.dashboard_colors,
            self.config.graph_view,
            self.config.dashboard_ranges,
        )
        ui.set_monitor_transport_status(None)
        ui.update_monitor_snapshot(state.snapshot, state.captured_at, state.history)
        self._sync_control_availability(ui)
        return True

    def toggle_power(self) -> None:
        """Browser action; never imports or owns controller/hardware."""
        if self._control_available:
            create_task(self._set_requested_power())

    def open_settings(self) -> None:
        if not self._control_available:
            return

        async def save(patch: dict[str, object], revision: int, source_id: str) -> dict[str, object]:
            if not self._control_available:
                raise RuntimeError("controller connection was lost; draft retained")
            if source_id != self._settings_source_id:
                raise RuntimeError("controller identity changed; copy the draft and reopen settings")
            response = await self._command(
                "save-settings",
                {"patch": patch, "revision": revision, "source_id": source_id},
            )
            self._accept_settings_response(response)
            return response

        async def show() -> None:
            try:
                initial = await self._command("read-settings", {})
                self._accept_settings_response(initial)
                self.push_screen(SettingsScreen(initial, save))
            except (ConnectionError, OSError, RuntimeError, TypeError, ValueError) as error:
                self.query_one(FanAppUI).set_monitor_transport_status(
                    f"Settings unavailable: {error}"
                )

        create_task(show())

    async def query_history(
        self, endpoint_id: str, start: float, end: float, width: int
    ) -> dict[str, object]:
        """Read one bounded history window without enabling browser control."""
        return await self._command(
            "read-history",
            {"endpoint_id": endpoint_id, "start": start, "end": end, "width": width},
        )

    async def _set_requested_power(self) -> None:
        try:
            if self._requested_power is None or self._settings_revision is None:
                self._accept_settings_response(await self._command("read-settings", {}))
            assert self._requested_power is not None
            assert self._settings_revision is not None
            assert self._settings_source_id is not None
            response = await self._command(
                "set-power",
                {
                    "enabled": not self._requested_power,
                    "revision": self._settings_revision,
                    "source_id": self._settings_source_id,
                },
            )
            self._accept_settings_response(response)
        except (ConnectionError, OSError, RuntimeError, TypeError, ValueError) as error:
            self.query_one(FanAppUI).set_monitor_transport_status(
                f"Power command failed: {error}"
            )

    def _accept_settings_response(self, response: dict[str, object]) -> None:
        source_id = response.get("source_id")
        revision = response.get("revision")
        power = response.get("power_enabled")
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("settings response has no controller identity")
        if not isinstance(revision, int) or isinstance(revision, bool):
            raise TypeError("settings response has no revision")
        if not isinstance(power, bool):
            raise TypeError("settings response has no requested power state")
        self._settings_source_id = source_id
        self._settings_revision = revision
        self._requested_power = power
        self._sync_control_availability()

    async def _command(self, operation: str, payload: dict[str, object]) -> dict[str, object]:
        if not self.config.web.allow_control and operation not in {"read-settings", "read-history"}:
            raise RuntimeError("browser control is disabled by web.allow_control")
        request_id = uuid.uuid4().hex
        request = {
            "version": 1,
            "source_id": self._settings_source_id or "",
            "request_id": request_id,
            "operation": operation,
            **payload,
        }
        mutation = operation in {"save-settings", "set-power"}
        deadline = asyncio.get_running_loop().time() + self.COMMAND_RECONCILE_TIMEOUT
        last_error: BaseException | None = None
        while True:
            try:
                response = await self._send_request(request)
            except (ConnectionError, OSError, TimeoutError, RuntimeError, TypeError, ValueError) as error:
                last_error = error
                if not mutation or asyncio.get_running_loop().time() >= deadline:
                    break
            else:
                if response.get("pending") is not True:
                    if not response.get("ok"):
                        raise RuntimeError(
                            str(response.get("message", "control command rejected"))
                        )
                    return response
                if not mutation or asyncio.get_running_loop().time() >= deadline:
                    last_error = RuntimeError(
                        str(response.get("message", "control command is still pending"))
                    )
                    break
            await asyncio.sleep(self.COMMAND_RETRY_SECONDS)

        if mutation:
            recovered = await self._recover_authoritative_state()
            if recovered is not None:
                raise RuntimeError(
                    f"{operation} outcome is still uncertain; controller state was refreshed "
                    f"at revision {recovered['revision']}. Keep this draft and verify before retrying"
                ) from last_error
            self._clear_control_state()
            raise RuntimeError(
                f"{operation} outcome is uncertain and the controller disconnected; "
                "keep this draft and reconnect before retrying"
            ) from last_error
        self._clear_control_state()
        raise RuntimeError(f"controller command failed: {last_error}") from last_error

    async def _send_request(self, request: dict[str, object]) -> dict[str, object]:
        socket_path = self.config.web.socket_path
        assert socket_path is not None
        reader, writer = await asyncio.wait_for(
            open_unix_connection(
                str(control_socket_path(socket_path)), limit=MAX_CONTROL_MESSAGE_BYTES + 1
            ),
            self.COMMAND_CONNECT_TIMEOUT,
        )
        try:
            writer.write(
                (json.dumps(request, separators=(",", ":"), allow_nan=False) + "\n").encode()
            )
            await writer.drain()
            raw = await asyncio.wait_for(
                reader.readline(),
                (
                    self.COMMAND_HISTORY_RESPONSE_TIMEOUT
                    if request.get("operation") == "read-history"
                    else self.COMMAND_RESPONSE_TIMEOUT
                ),
            )
            if not raw:
                raise RuntimeError("controller closed the command connection")
            if len(raw) > MAX_CONTROL_MESSAGE_BYTES or not raw.endswith(b"\n"):
                raise RuntimeError("controller returned an invalid response size")
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise TypeError("controller returned an invalid response")
            return response
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def _recover_authoritative_state(self) -> dict[str, object] | None:
        request = {
            "version": 1,
            "source_id": self._settings_source_id or "",
            "request_id": uuid.uuid4().hex,
            "operation": "read-settings",
        }
        try:
            response = await self._send_request(request)
            if not response.get("ok"):
                return None
            self._accept_settings_response(response)
            return response
        except (ConnectionError, OSError, TimeoutError, RuntimeError, TypeError, ValueError):
            return None

    @property
    def _control_available(self) -> bool:
        return (
            self.config.web.allow_control
            and self._phase == "CONNECTED"
            and self._control_stream_fresh
            and self._settings_source_id is not None
            and self._settings_source_id == self._stream_settings_source_id
            and self._settings_revision is not None
            and self._requested_power is not None
        )

    def _sync_control_availability(self, ui: FanAppUI | None = None) -> None:
        if ui is None:
            if not self.is_running:
                return
            try:
                ui = self.query_one(FanAppUI)
            except NoMatches:  # Textual tree may not be mounted yet.
                return
        ui.set_control_available(self._control_available)

    def _clear_control_state(self, ui: FanAppUI | None = None) -> None:
        self._settings_source_id = None
        self._stream_settings_source_id = None
        self._settings_revision = None
        self._requested_power = None
        self._control_stream_fresh = False
        self._sync_control_availability(ui)

    def _set_phase(self, ui: FanAppUI, phase: str) -> None:
        if self._shutting_down:
            return
        self._phase = phase
        if phase != "CONNECTED":
            self._clear_control_state(ui)
        ui.set_monitor_transport_status(f"Monitor stream: {phase}")

    def _watchdog(self, now: float | None = None) -> None:
        """Expose a stalled publisher even while the Unix socket stays open."""
        if self._phase != "CONNECTED" or self._last_fresh_received_at is None:
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
            ui = self.query_one(FanAppUI)
            self._clear_control_state(ui)
            ui.set_monitor_transport_status("Monitor stream: STALE")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DGX fan browser monitor")
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
