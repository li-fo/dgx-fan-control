from __future__ import annotations

import argparse
import copy
import os
import sys
import time
from asyncio import CancelledError, Task, create_task, sleep
from dataclasses import dataclass
from typing import Any

from textual.app import App, ComposeResult

from .config import AppConfig, ConfigError, load_config, resolve_config_path
from .controller import FanController
from .dcgm import DCGMCollector
from .hardware import FanHardware, create_hardware
from .models import ControlSnapshot, EndpointSnapshot
from .ui import FanAppUI


@dataclass(frozen=True)
class _TerminalState:
    """The exact POSIX state of the interactive stdin descriptor, if available."""

    fd: int
    attributes: list[Any]
    blocking: bool


def _capture_terminal_state() -> _TerminalState | None:
    """Capture stdin only when it is an interactive POSIX terminal.

    Textual owns normal terminal lifecycle.  This is deliberately only a
    best-effort fallback for an interrupted or emulator-specific teardown.
    """
    if os.name != "posix" or not sys.stdin.isatty():
        return None
    if not all(hasattr(os, name) for name in ("get_blocking", "set_blocking")):
        return None
    try:
        import termios

        fd = sys.stdin.fileno()
        return _TerminalState(fd, copy.deepcopy(termios.tcgetattr(fd)), os.get_blocking(fd))
    except (ImportError, OSError, ValueError):
        return None


def _restore_terminal_state(state: _TerminalState | None) -> None:
    """Restore a captured terminal state without obscuring application errors."""
    if state is None:
        return
    try:
        import termios

        termios.tcsetattr(state.fd, termios.TCSANOW, state.attributes)
    except (ImportError, OSError, ValueError):
        pass
    try:
        os.set_blocking(state.fd, state.blocking)
    except (OSError, ValueError, AttributeError):
        pass


class DGXFanApp(App[None]):
    TITLE = "DGX Fan Controller"
    CONTROL_TICK_SECONDS = 0.25

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.hardware: FanHardware | None = None
        self.controller = FanController(config.control, config.hardware)
        self.collector = DCGMCollector(config.endpoints, config.collection.timeout_seconds, config.collection.stale_after_seconds)
        self.endpoints: tuple[EndpointSnapshot, ...] = ()
        self.latest: ControlSnapshot | None = None
        self._poll_task: Task[None] | None = None

    def compose(self) -> ComposeResult:
        yield FanAppUI(str(self.config.path), self.toggle_power, self.config.control.emergency_temperature_celsius)

    async def on_mount(self) -> None:
        self.hardware = create_hardware(self.config.hardware)
        self.hardware.set_duty(100)  # Safe-full before any external read.
        self.set_interval(self.CONTROL_TICK_SECONDS, self.control_tick)
        self._poll_task = create_task(self._poll_loop())
        self.control_tick()

    async def _poll_loop(self) -> None:
        while True:
            await self._poll_once(time.monotonic())
            await sleep(self.config.collection.interval_seconds)

    async def _poll_once(self, now: float) -> None:
        try:
            await self.collector.collect(now)
        except CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - a supervisor must convert unexpected poll failures to fail-safe state.
            self.collector.mark_unhealthy(error)

    def control_tick(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        if self.hardware is None:
            return
        self.endpoints = self.collector.snapshots(current)
        fans = self.hardware.readings(current)
        self.latest = self.controller.update(self.endpoints, fans, current)
        self.hardware.set_duty(self.latest.duty_percent)
        self.query_one(FanAppUI).update_snapshot(self.latest, current)

    def toggle_power(self) -> None:
        self.controller.set_power(not self.controller.power)

    async def on_unmount(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except CancelledError:
                pass
        if self.hardware is not None:
            self.hardware.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DGX DCGM fan controller TUI")
    parser.add_argument("--config", help="TOML config path; overrides DGX_FAN_CONFIG and ./config.toml")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(resolve_config_path(args.config))
    except ConfigError as error:
        raise SystemExit(f"dgx-fan: configuration error: {error}") from error
    app = DGXFanApp(config)
    terminal_state = _capture_terminal_state()
    try:
        app.run()
    finally:
        try:
            _restore_terminal_state(terminal_state)
        finally:
            if app.hardware is not None:
                app.hardware.release()
