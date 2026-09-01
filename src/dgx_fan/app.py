from __future__ import annotations

import argparse
import copy
import os
import re
import signal
import sys
import threading
import time
from asyncio import CancelledError, Task, create_task, sleep
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from rich.terminal_theme import DEFAULT_TERMINAL_THEME
from textual import constants
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.driver import Driver
from textual.reactive import Reactive


def _create_no_kitty_linux_driver() -> type[Driver] | None:
    """Create the POSIX-only adapter without importing Linux modules on Windows."""
    if os.name != "posix":
        return None
    from textual.drivers.linux_driver import LinuxDriver

    class NoKittyLinuxDriver(LinuxDriver):
        """Keep Textual's mouse/terminal handling without owning Kitty CSI-u.

        Textual 8.2.8 emits the enable sequence only in Linux application mode
        but unconditionally emits its matching disable sequence.  This app
        needs neither sequence, so suppress this driver's exact standalone
        pair together.  Do not use this for a user-supplied custom driver.
        """

        _KITTY_ENABLE = re.compile(r"\x1b\[>[0-9]+u\Z")
        _KITTY_DISABLE = "\x1b[<u"

        def write(self, data: str) -> None:
            if self._KITTY_ENABLE.fullmatch(data) or data == self._KITTY_DISABLE:
                return
            super().write(data)

    return NoKittyLinuxDriver


_NO_KITTY_LINUX_DRIVER = _create_no_kitty_linux_driver()

from .config import AppConfig, ConfigError, EndpointConfig, load_config, resolve_config_path
from .controller import FanController
from .dcgm import DCGMCollector
from .hardware import FanHardware, create_hardware
from .models import ControlSnapshot, EndpointSnapshot, NodeMemorySnapshot
from .monitor import MonitorPublisher
from .node_exporter import NodeExporterCollector
from .ui import FanAppUI


@dataclass
class _TerminalState:
    """The exact POSIX state of an owned duplicate of interactive stdin."""

    fd: int | None
    attributes: list[Any]
    blocking: bool


def _capture_terminal_state() -> _TerminalState | None:
    """Capture stdin only when it is an interactive POSIX terminal.

    Textual owns normal terminal lifecycle.  This is deliberately only a
    best-effort fallback for an interrupted or emulator-specific teardown.
    """
    if os.name != "posix":
        return None
    if not all(hasattr(os, name) for name in ("close", "dup", "get_blocking", "set_blocking")):
        return None
    duplicate_fd: int | None = None
    try:
        import termios

        if not sys.stdin.isatty():
            return None
        duplicate_fd = os.dup(sys.stdin.fileno())
        return _TerminalState(
            duplicate_fd,
            copy.deepcopy(termios.tcgetattr(duplicate_fd)),
            os.get_blocking(duplicate_fd),
        )
    except (AttributeError, ImportError, OSError, ValueError):
        if duplicate_fd is not None:
            try:
                os.close(duplicate_fd)
            except OSError:
                pass
        return None


def _restore_terminal_state(state: _TerminalState | None) -> None:
    """Restore a captured terminal state without obscuring application errors."""
    if state is None or state.fd is None:
        return
    fd = state.fd
    try:
        import termios

        termios.tcsetattr(fd, termios.TCSANOW, state.attributes)
    except (ImportError, OSError, ValueError):
        pass
    finally:
        try:
            os.set_blocking(fd, state.blocking)
        except (OSError, ValueError, AttributeError):
            pass
        finally:
            try:
                os.close(fd)
            except (AttributeError, OSError):
                pass
            finally:
                state.fd = None


class DGXFanApp(App[None]):
    TITLE = "DGX Fan Controller"
    # Keep the physical tty keyboard exit explicit even if Textual defaults change.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True)
    ]
    CONTROL_TICK_SECONDS = 0.25
    # Textual normally maps Rich ANSI names through its Monokai/Alabaster
    # palettes.  On an 8-color Linux console that remapping changes the
    # intended ANSI slots (for example red can become magenta).  Use Rich's
    # canonical terminal palette for both modes so named chart colors retain
    # their standard SGR codes after Textual's conversion path.
    ansi_theme_dark = Reactive(DEFAULT_TERMINAL_THEME, init=False)
    ansi_theme_light = Reactive(DEFAULT_TERMINAL_THEME, init=False)

    def __init__(self, config: AppConfig) -> None:
        # Preserve Textual's Windows/headless paths and an explicit custom
        # driver.  Only the default POSIX Linux driver receives the balanced
        # CSI-u adapter above.
        driver_class: type[Driver] | None = None
        if constants.DRIVER is None and _NO_KITTY_LINUX_DRIVER is not None:
            driver_class = _NO_KITTY_LINUX_DRIVER
        super().__init__(driver_class=driver_class)
        self.config = config
        self.hardware: FanHardware | None = None
        self.controller = FanController(config.control, config.hardware)
        self.collector = DCGMCollector(
            config.endpoints,
            config.collection.timeout_seconds,
            config.collection.stale_after_seconds,
            config.collection.retry_count,
            config.collection.retry_delay_seconds,
        )
        self.node_endpoints = tuple(
            endpoint for endpoint in config.endpoints if endpoint.memory_source == "node-exporter"
        )
        self.node_collector = NodeExporterCollector(
            self.node_endpoints,
            config.collection.timeout_seconds,
            config.collection.stale_after_seconds,
            config.collection.retry_count,
            config.collection.retry_delay_seconds,
        )
        self.endpoints: tuple[EndpointSnapshot, ...] = ()
        self.latest: ControlSnapshot | None = None
        self._poll_tasks: tuple[Task[None], ...] = ()
        self._node_poll_tasks: tuple[Task[None], ...] = ()
        self._control_timer: Any | None = None
        self.monitor_publisher: MonitorPublisher | None = (
            MonitorPublisher(config.web.socket_path) if config.web.enabled and config.web.socket_path else None
        )
        self._monitor_signature: tuple[object, ...] | None = None

    def compose(self) -> ComposeResult:
        yield FanAppUI(
            str(self.config.path),
            self.toggle_power,
            self.config.control.emergency_temperature_celsius,
            self.config.collection.interval_seconds,
            self.config.dashboard_colors,
        )

    async def on_mount(self) -> None:
        self.hardware = create_hardware(self.config.hardware)
        self.hardware.set_duties(
            (self.config.control.fallback_speed_percent,) * 2
        )  # Application fallback before any external read.
        self._control_timer = self.set_interval(self.CONTROL_TICK_SECONDS, self.control_tick)
        self._poll_tasks = tuple(
            create_task(self._poll_loop(endpoint)) for endpoint in self.config.endpoints
        )
        self._node_poll_tasks = tuple(
            create_task(self._node_poll_loop(endpoint)) for endpoint in self.node_endpoints
        )
        if self.monitor_publisher is not None:
            try:
                await self.monitor_publisher.start()
            except (OSError, RuntimeError):
                # Browser publication is optional display output. It must not
                # prevent or alter the fan controller's safety lifecycle.
                self.monitor_publisher = None
        self.control_tick()

    async def _poll_loop(self, endpoint: EndpointConfig) -> None:
        while True:
            await self._poll_once(endpoint)
            await sleep(self.config.collection.interval_seconds)

    async def _poll_once(self, endpoint: EndpointConfig, now: float | None = None) -> None:
        try:
            await self.collector.collect_endpoint(endpoint, now)
        except CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - a supervisor must convert unexpected poll failures to fail-safe state.
            self.collector.mark_endpoint_unhealthy(endpoint, error)

    async def _node_poll_loop(self, endpoint: EndpointConfig) -> None:
        while True:
            await self._node_poll_once(endpoint)
            await sleep(self.config.collection.interval_seconds)

    async def _node_poll_once(self, endpoint: EndpointConfig, now: float | None = None) -> None:
        try:
            await self.node_collector.collect_endpoint(endpoint, now)
        except CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - node memory is display-only.
            self.node_collector.mark_endpoint_unhealthy(endpoint, error)

    def control_tick(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        if self.hardware is None:
            return
        memory_by_endpoint = {
            snapshot.endpoint_id: snapshot for snapshot in self.node_collector.snapshots(current)
        }
        self.endpoints = tuple(
            self._merge_memory_snapshot(snapshot, memory_by_endpoint.get(snapshot.endpoint_id))
            for snapshot in self.collector.snapshots(current)
        )
        fans = self.hardware.readings(current)
        self.latest = self.controller.update(self.endpoints, fans, current)
        self.hardware.set_duties(self.latest.duty_percents)
        ui = self.query_one(FanAppUI)
        ui.update_snapshot(self.latest, current)
        monitor_signature = (
            self.latest.duty_percents,
            self.latest.state,
            self.latest.reason,
            tuple(fan.state for fan in self.latest.fans),
            tuple(
                (endpoint.endpoint_id, endpoint.sample_revision, endpoint.memory_sample_revision)
                for endpoint in self.latest.endpoint_snapshots
            ),
        )
        if self.monitor_publisher is not None and monitor_signature != self._monitor_signature:
            self.monitor_publisher.publish(self.latest, ui.history, current)
            self._monitor_signature = monitor_signature

    @staticmethod
    def _merge_memory_snapshot(
        endpoint: EndpointSnapshot, memory: NodeMemorySnapshot | None
    ) -> EndpointSnapshot:
        if endpoint.memory_source != "node-exporter":
            return endpoint
        if memory is None:
            return replace(endpoint, memory_healthy=False, memory_error="awaiting first sample")
        # Deliberately do not alter DCGM health: the controller must remain
        # dependent only on temperature telemetry.
        return replace(
            endpoint,
            uma_memory=memory.memory,
            memory_healthy=memory.healthy,
            memory_age_seconds=memory.age_seconds,
            memory_stale=memory.stale,
            memory_error=memory.error,
            memory_sample_revision=memory.sample_revision,
            memory_retrying=memory.retrying,
            memory_retry_attempt=memory.retry_attempt,
            memory_retry_count=memory.retry_count,
            memory_failed_attempts=memory.failed_attempts,
        )

    def toggle_power(self) -> None:
        self.controller.set_power(not self.controller.power)

    async def on_unmount(self) -> None:
        if self._control_timer is not None:
            self._control_timer.stop()
            self._control_timer = None
        for task in (*self._poll_tasks, *self._node_poll_tasks):
            task.cancel()
        for task in (*self._poll_tasks, *self._node_poll_tasks):
            try:
                await task
            except CancelledError:
                pass
        self._poll_tasks = ()
        self._node_poll_tasks = ()
        if self.monitor_publisher is not None:
            await self.monitor_publisher.close()
            self.monitor_publisher = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DGX DCGM fan controller TUI")
    parser.add_argument(
        "--config", help="TOML config path; overrides DGX_FAN_CONFIG and ./config.toml"
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(resolve_config_path(args.config))
    except ConfigError as error:
        raise SystemExit(f"dgx-fan: configuration error: {error}") from error
    app = DGXFanApp(config)
    terminal_state = _capture_terminal_state()
    run_completed = False
    clean_stop_requested = False

    def restore_signal_handlers() -> None:
        return None

    if threading.current_thread() is threading.main_thread():
        previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
        clean_stop_signal = getattr(signal, "SIGUSR1", None)

        def _terminate_non_cleanly(signum: int, _frame: object) -> None:
            # Ordinary SIGTERM is an abnormal path. Exit through the finally
            # block so PWM is released with the existing fail-safe behavior.
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, _terminate_non_cleanly)
        if clean_stop_signal is not None:
            previous_clean_stop_handler = signal.getsignal(clean_stop_signal)

            def _terminate_cleanly(signum: int, _frame: object) -> None:
                nonlocal clean_stop_requested
                clean_stop_requested = True
                raise SystemExit(0)

            signal.signal(clean_stop_signal, _terminate_cleanly)

            def restore_signal_handlers() -> None:
                signal.signal(signal.SIGTERM, previous_sigterm_handler)
                signal.signal(clean_stop_signal, previous_clean_stop_handler)
        else:

            def restore_signal_handlers() -> None:
                signal.signal(signal.SIGTERM, previous_sigterm_handler)
    try:
        app.run()
        # Textual catches some internal fatal exceptions and returns with a
        # non-zero public code, so return alone is not a clean fan-off exit.
        run_completed = getattr(app, "return_code", None) == 0
    finally:
        try:
            _restore_terminal_state(terminal_state)
        finally:
            try:
                if app.hardware is not None:
                    app.hardware.release(normal_shutdown=run_completed or clean_stop_requested)
            finally:
                restore_signal_handlers()
