from __future__ import annotations

import os
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.renderables.digits import Digits
from textual.widget import WidgetError
from textual.widgets import Button, Footer, Header, Static, TabbedContent, TabPane

from .config import DashboardColors, EndpointConfig
from .history_ui import HistoryPanel, HistoryQuery
from .models import ControlSnapshot, EndpointSnapshot, GPUStat, MemoryStat

HISTORY_SECONDS = 120.0
MIN_DASHBOARD_WIDTH = 79
PLOT_HEIGHT = 5
MAX_LAYOUT_CONVERGENCE_PASSES = 2
_GRAPH_TWO_METRIC_LABELS = ("TEMP", "MEM", "UTIL", "POWER")
_TTY8_FONT_MARKER = "DGX_FAN_TEXTUAL_TTY8_FONT"


def _is_linux_virtual_console(stream: object | None = None) -> bool:
    """Identify /dev/ttyN without trusting an inherited TERM value."""
    candidate = sys.stdin if stream is None else stream
    try:
        if not candidate.isatty():  # type: ignore[union-attr]
            return False
        path = os.ttyname(candidate.fileno())  # type: ignore[union-attr]
    except (AttributeError, OSError):
        return False
    suffix = path.removeprefix("/dev/tty")
    return path != suffix and suffix.isdecimal()


def _safe_display_text(value: object) -> str:
    """Prevent external text from carrying terminal control sequences into Rich."""
    return "".join(
        "\N{REPLACEMENT CHARACTER}" if ord(char) < 32 or 127 <= ord(char) <= 159 else char
        for char in str(value)
    )


def _compact_display_text(value: object, maximum_cells: int) -> str:
    """Sanitize and truncate an external identifier without splitting wide cells."""
    safe = _safe_display_text(value)
    if cell_len(safe) <= maximum_cells:
        return safe
    result = ""
    for char in safe:
        if cell_len(result + char + "…") > maximum_cells:
            return result + "…"
        result += char
    return result


def _rich_color(color: str | None) -> str | None:
    """Translate persisted terminal-safe aliases before Rich parses a style."""
    return color.removeprefix("ansi_") if color else None


@dataclass(frozen=True)
class HistoryPoint:
    at: float
    value: float


class DashboardHistory:
    """Revision-deduplicated, bounded in-memory metric history."""

    def __init__(self, collection_interval_seconds: float) -> None:
        self.collection_interval_seconds = collection_interval_seconds
        self.points: dict[tuple[str, str, str], list[HistoryPoint]] = defaultdict(list)
        self.last_seen: dict[tuple[str, str], float] = {}
        self.revisions: dict[str, int] = {}
        self.memory_revisions: dict[str, int] = {}

    def append(
        self,
        endpoint_id: str,
        revision: int,
        gpus: tuple[GPUStat, ...],
        now: float,
        memory_source: str = "dcgm",
        uma_memory: MemoryStat | None = None,
        memory_revision: int = 0,
    ) -> None:
        if self.revisions.get(endpoint_id) != revision:
            self.revisions[endpoint_id] = revision
            for gpu in gpus:
                identity = (endpoint_id, gpu.key)
                self.last_seen[identity] = now
                memory_percent = None
                if (
                    gpu.memory_used_mib is not None
                    and gpu.memory_total_mib is not None
                    and gpu.memory_total_mib > 0
                ):
                    memory_percent = gpu.memory_used_mib / gpu.memory_total_mib * 100
                for metric, value in (
                    ("mem", memory_percent),
                    ("util", gpu.utilization_percent),
                    ("temp", gpu.temperature_celsius),
                ):
                    if value is not None:
                        self.points[(*identity, metric)].append(HistoryPoint(now, value))
        if memory_source == "node-exporter" and self.memory_revisions.get(endpoint_id) != memory_revision:
            self.memory_revisions[endpoint_id] = memory_revision
            if uma_memory is not None and uma_memory.total_mib > 0:
                self.points[(endpoint_id, "__uma__", "mem")].append(
                    HistoryPoint(now, uma_memory.used_mib / uma_memory.total_mib * 100)
                )
        self.prune(now)

    def prune(self, now: float) -> None:
        cutoff = now - HISTORY_SECONDS
        for key in list(self.points):
            retained = [point for point in self.points[key] if point.at >= cutoff]
            if retained:
                self.points[key] = retained
            else:
                del self.points[key]
        for identity, seen in list(self.last_seen.items()):
            if seen < cutoff:
                del self.last_seen[identity]

    def area(
        self,
        endpoint_id: str,
        gpu: str,
        metric: str,
        now: float,
        width: int,
        maximum: float,
        plot_height: int = PLOT_HEIGHT,
    ) -> list[str]:
        if not 1 <= plot_height <= PLOT_HEIGHT:
            raise ValueError(f"plot_height must be between 1 and {PLOT_HEIGHT}")
        bins: list[list[float]] = [[] for _ in range(width)]
        latest_at: list[float | None] = [None for _ in range(width)]
        cutoff = now - HISTORY_SECONDS
        for point in self.points.get((endpoint_id, gpu, metric), []):
            if cutoff <= point.at <= now:
                index = min(width - 1, int((point.at - cutoff) / HISTORY_SECONDS * width))
                bins[index].append(point.value)
                latest = latest_at[index]
                latest_at[index] = point.at if latest is None else max(latest, point.at)
        values = [
            None
            if not group
            else (
                max(group)
                if metric == "temp"
                else sum(group) / len(group)
                if metric == "util"
                else group[-1]
            )
            for group in bins
        ]
        # A DCGM sample represents the preceding interval. Step-fill only the
        # expected short wait for its successor; longer holes stay observable.
        bin_seconds = HISTORY_SECONDS / width
        hold_seconds = self.collection_interval_seconds * 1.5
        previous_value: float | None = None
        previous_at: float | None = None
        for index, value in enumerate(values):
            if value is not None:
                previous_value, previous_at = value, latest_at[index]
                continue
            # The whole represented bin must fit in the bounded hold. Checking
            # only its start would paint [3s, 4s) from a t=0 sample with a 3s
            # hold, hiding the outage before a t=4 successor.
            bin_end = cutoff + (index + 1) * bin_seconds
            if (
                previous_value is not None
                and previous_at is not None
                and bin_end <= previous_at + hold_seconds
            ):
                values[index] = previous_value
        # Stack only portable ASCII levels. A dot occupies half a terminal row
        # and a colon occupies a complete row, keeping compact plots legible on
        # both Linux virtual consoles and PTYs.
        columns: list[str] = []
        for value in values:
            if value is None:
                columns.append(" " * plot_height)
                continue
            clamped = min(max(value, 0), maximum)
            if clamped == 0:
                columns.append(" " * (plot_height - 1) + ".")
                continue

            # Clamp first so malformed over-range telemetry cannot draw beyond
            # the plot. ceil preserves a visible positive half-row minimum.
            occupied = max(1, ceil(clamped / maximum * plot_height * 2))
            full_rows, partial = divmod(occupied, 2)
            columns.append(
                " " * (plot_height - full_rows - partial) + "." * partial + ":" * full_rows
            )

        rows: list[str] = []
        label_rows = {0, plot_height // 2, plot_height - 1}
        for row in range(plot_height):
            threshold = (plot_height - 1 - row) / max(1, plot_height - 1) * maximum
            line = "".join(column[row] for column in columns)
            label = f"{threshold:>4.0f} " if row in label_rows else "     "
            rows.append(label + line)
        axis = (
            "     120s"
            + " " * max(0, width // 2 - 7)
            + "60s"
            + " " * max(0, width // 4 - 3)
            + "30s"
            + " " * max(0, width // 4 - 4)
            + "now"
        )
        rows.append(axis[: width + 5].ljust(width + 5))
        return rows

    def endpoint_area(
        self, endpoint_id: str, metric: str, now: float, width: int, maximum: float, plot_height: int
    ) -> list[str]:
        """Draw a per-endpoint physical-GPU maximum without changing stored samples."""
        cutoff = now - HISTORY_SECONDS
        bins: list[float | None] = [None] * width
        for (source, _gpu, source_metric), points in self.points.items():
            if source != endpoint_id or source_metric != metric:
                continue
            for point in points:
                if cutoff <= point.at <= now:
                    index = min(width - 1, int((point.at - cutoff) / HISTORY_SECONDS * width))
                    previous = bins[index]
                    bins[index] = point.value if previous is None else max(previous, point.value)
        aggregate = DashboardHistory(self.collection_interval_seconds)
        aggregate.points[(endpoint_id, "__endpoint__", metric)] = [
            HistoryPoint(cutoff + (index + 0.5) / width * HISTORY_SECONDS, value)
            for index, value in enumerate(bins) if value is not None
        ]
        return aggregate.area(endpoint_id, "__endpoint__", metric, now, width, maximum, plot_height)


class FanGauge(Static):
    """A compact terminal-safe ring for one fan's independent PWM and tach state."""

    SEGMENTS = 12
    RING_WIDTH = 15
    RING_POSITIONS = (
        (0, 6),
        (0, 7),
        (0, 8),
        (1, 11),
        (2, 12),
        (3, 11),
        (4, 8),
        (4, 7),
        (4, 6),
        (3, 3),
        (2, 2),
        (1, 3),
    )

    def __init__(self, number: int) -> None:
        self.number = number
        super().__init__(self._content(number, None, None, "WAITING"), id=f"fan-{number}-gauge")

    @classmethod
    def _content(cls, number: int, duty_percent: int | None, rpm: float | None, state: str) -> Text:
        percentage = "--" if duty_percent is None else f"{duty_percent}%"
        filled = (
            0
            if duty_percent is None
            else round(max(0, min(100, duty_percent)) / 100 * cls.SEGMENTS)
        )
        ring = [[" " for _ in range(cls.RING_WIDTH)] for _ in range(5)]
        for index, (row, column) in enumerate(cls.RING_POSITIONS):
            ring[row][column] = "●" if index < filled else "○"
        ring[2][5:10] = list(f"{percentage:^5}")
        rpm_text = "N/A" if rpm is None else f"{rpm:.0f} RPM"
        return Text(
            f"Fan {number}\n"
            + "\n".join("".join(row) for row in ring)
            + f"\nRPM: {rpm_text} | {state}"
        )

    def set_reading(self, duty_percent: int, rpm: float | None, state: str) -> None:
        self.update(self._content(self.number, duty_percent, rpm, state))


class FanAppUI(Static):
    DEFAULT_CSS = """
    #dashboard-status-row { height: 1; }
    #error-banner {
        width: 1fr;
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    #dashboard-fan-1-rpm, #dashboard-fan-2-rpm { width: 15; height: 1; }
    #dashboard-fan-separator { width: 3; height: 1; }
    #read-only-indicator { width: 11; height: 1; }
    #fan-top-row { height: 5; }
    #fan-status { width: 1fr; height: 5; border: round $primary; }
    #power-toggle, #fan-settings { width: 15; height: 3; }
    #fan-gauge-row { height: 9; }
    #fan-1-gauge, #fan-2-gauge {
        width: 1fr;
        height: 9;
        border: round $primary;
        content-align: center middle;
    }
    """

    def __init__(
        self,
        config_path: str,
        toggle: Callable[[], None],
        emergency_temperature: float,
        collection_interval_seconds: float,
        dashboard_colors: DashboardColors | None = None,
        settings: Callable[[], None] | None = None,
        *,
        graph_view: str = "graph-1",
        read_only: bool = False,
        history_query: HistoryQuery | None = None,
        history_endpoints: tuple[EndpointConfig, ...] = (),
    ) -> None:
        super().__init__()
        self.config_path, self.toggle, self.emergency_temperature = (
            config_path,
            toggle,
            emergency_temperature,
        )
        self.dashboard_colors = dashboard_colors or DashboardColors()
        self.graph_view = graph_view
        self.collection_interval_seconds = collection_interval_seconds
        self.settings = settings
        self.read_only = read_only
        self.history_query = history_query
        self.history_endpoints = history_endpoints
        self.snapshot: ControlSnapshot | None = None
        self.history = DashboardHistory(collection_interval_seconds)
        self.panels: dict[str, Static] = {}
        self.last_render_time: float | None = None
        self.last_signature: tuple[tuple[object, ...], ...] | None = None
        self.monitor_transport_status: str | None = None
        self._resize_redraw_pending = False
        self._dashboard_layout_signature: tuple[int, int, int, int, int, int] | None = None
        self._layout_convergence_passes = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="dashboard"):
            with TabPane("DGX Dashboard", id="dashboard"):
                with Horizontal(id="dashboard-status-row"):
                    if self.read_only:
                        yield Static("READ ONLY", id="read-only-indicator", markup=False)
                    yield Static("Waiting for DCGM metrics…", id="error-banner", markup=False)
                    yield Static("Fan 1: N/A", id="dashboard-fan-1-rpm", markup=False)
                    yield Static(" | ", id="dashboard-fan-separator", markup=False)
                    yield Static("Fan 2: N/A", id="dashboard-fan-2-rpm", markup=False)
                yield Static(id="dashboard-warning")
                yield VerticalScroll(id="dashboard-scroll")
            with TabPane("Fan Control", id="fan-control"), Vertical():
                with Horizontal(id="fan-top-row"):
                    yield Static(
                        "Fan Status / Control · WAITING\nF1: --\nF2: --",
                        id="fan-status",
                        markup=False,
                    )
                    yield Button("Turn On / Off", id="power-toggle", disabled=self.read_only)
                    yield Button("Setting", id="fan-settings", disabled=self.read_only)
                with Horizontal(id="fan-gauge-row"):
                    yield FanGauge(1)
                    yield FanGauge(2)
            with TabPane("History", id="history"):
                yield HistoryPanel(
                    self.history_endpoints,
                    self.history_query,
                    self.dashboard_colors,
                    self.collection_interval_seconds,
                )
        yield Footer()

    def update_snapshot(
        self, snapshot: ControlSnapshot, now: float, *, append_history: bool = True
    ) -> None:
        self.snapshot = snapshot
        self.last_render_time = now
        if append_history:
            for endpoint in snapshot.endpoint_snapshots:
                self.history.append(
                    endpoint.endpoint_id,
                    endpoint.sample_revision,
                    endpoint.gpus,
                    now,
                    endpoint.memory_source,
                    endpoint.uma_memory,
                    endpoint.memory_sample_revision,
                )
        errors = []
        for endpoint in snapshot.endpoint_snapshots:
            age = "N/A" if endpoint.age_seconds is None else f"{endpoint.age_seconds:.1f}s"
            name = _safe_display_text(endpoint.name)
            if endpoint.retrying:
                status = "RETRYING · STALE" if endpoint.stale else (
                    f"RETRYING {endpoint.retry_attempt}/{endpoint.retry_count}"
                )
                errors.append(f"{name}: {status} (sample age: {age})")
            elif endpoint.error and endpoint.error != "awaiting first sample":
                attempts = endpoint.failed_attempts or endpoint.retry_count + 1
                errors.append(
                    f"{name}: FAILED after {attempts} attempts: "
                    f"{_safe_display_text(endpoint.error)} (sample age: {age})"
                )
            elif not endpoint.healthy:
                errors.append(f"{name}: WAITING (sample age: {age})")
            if endpoint.memory_source == "node-exporter" and (
                endpoint.memory_retrying or not endpoint.memory_healthy
            ):
                memory_age = "N/A" if endpoint.memory_age_seconds is None else f"{endpoint.memory_age_seconds:.1f}s"
                if endpoint.memory_retrying:
                    memory_status = "RETRYING · STALE" if endpoint.memory_stale else (
                        f"RETRYING {endpoint.memory_retry_attempt}/{endpoint.memory_retry_count}"
                    )
                elif endpoint.memory_error == "awaiting first sample":
                    memory_status = "WAITING"
                elif endpoint.memory_stale:
                    memory_status = "STALE"
                elif endpoint.memory_error and endpoint.memory_error != "awaiting first sample":
                    attempts = endpoint.memory_failed_attempts or endpoint.memory_retry_count + 1
                    memory_status = f"FAILED after {attempts} attempts: {_safe_display_text(endpoint.memory_error)}"
                else:
                    memory_status = "WAITING"
                errors.append(f"{name}: UMA MEM {memory_status} (sample age: {memory_age})")
        status_message = " | ".join(errors) if errors else "All configured DGX endpoints are healthy."
        if self.monitor_transport_status is not None:
            status_message = f"{self.monitor_transport_status} | {status_message}"
        self.query_one("#error-banner", Static).update(
            Text(status_message, no_wrap=True, overflow="ellipsis")
        )
        for number in (1, 2):
            rpm = snapshot.fans[number - 1].rpm
            rpm_text = "N/A" if rpm is None else f"{rpm:.0f} RPM"
            self.query_one(f"#dashboard-fan-{number}-rpm", Static).update(
                f"Fan {number}: {rpm_text}"
            )
        signature = tuple(
            (
                endpoint.endpoint_id,
                endpoint.sample_revision,
                endpoint.memory_sample_revision,
                endpoint.memory_healthy,
                endpoint.memory_stale,
                endpoint.memory_error,
                # Graph #2 substitutes N/A for stale/failed values, so its
                # renderer must react even before a new collector revision.
                endpoint.healthy if self.graph_view == "graph-2" else None,
                endpoint.stale if self.graph_view == "graph-2" else None,
                endpoint.error if self.graph_view == "graph-2" else None,
                endpoint.memory_healthy if self.graph_view == "graph-2" else None,
            )
            for endpoint in snapshot.endpoint_snapshots
        )
        if signature != self.last_signature:
            self._layout_convergence_passes = 0
            self._render_dashboard(snapshot, now)
            self.last_signature = signature
        def fan_summary(index: int) -> str:
            temperature = snapshot.fan_temperatures_celsius[index]
            stage = snapshot.active_stages[index]
            temperature_text = "N/A" if temperature is None else f"{temperature:.1f} C"
            return (
                f"F{index + 1} {_compact_display_text(snapshot.fan_endpoint_ids[index], 14)} "
                f"{snapshot.duty_percents[index]}% {temperature_text} S{stage if stage is not None else '-'}"
            )

        self.query_one("#fan-status", Static).update(
            f"Fan Status / Control{' · READ ONLY' if self.read_only else ''} · {snapshot.state}\n"
            f"{fan_summary(0)}\n{fan_summary(1)}"
        )
        for number in (1, 2):
            fan = snapshot.fans[number - 1]
            self.query_one(f"#fan-{number}-gauge", FanGauge).set_reading(
                snapshot.duty_percents[number - 1], fan.rpm, fan.state
            )

    def update_monitor_snapshot(
        self, snapshot: ControlSnapshot, now: float, history: DashboardHistory
    ) -> None:
        """Apply a full replacement received from the read-only local monitor stream."""
        self.history = history
        self.last_signature = None
        try:
            self.update_snapshot(snapshot, now, append_history=False)
        except (NoMatches, WidgetError):
            # Ignore a final frame after renderer child widgets start unmounting.
            pass

    def reconfigure_display(
        self,
        emergency_temperature: float,
        collection_interval_seconds: float,
        dashboard_colors: DashboardColors,
        graph_view: str = "graph-1",
    ) -> None:
        """Apply effective presentation settings without discarding chart history."""
        self.emergency_temperature = emergency_temperature
        self.dashboard_colors = dashboard_colors
        self.graph_view = graph_view
        self.history.collection_interval_seconds = collection_interval_seconds
        self.collection_interval_seconds = collection_interval_seconds
        try:
            self.query_one(HistoryPanel).reconfigure(dashboard_colors, collection_interval_seconds)
        except NoMatches:
            pass
        self.last_signature = None
        if self.snapshot is not None and self.last_render_time is not None:
            try:
                self.update_snapshot(self.snapshot, self.last_render_time, append_history=False)
            except (NoMatches, WidgetError):
                # A monitor frame may race the renderer's child teardown.
                pass

    def set_monitor_transport_status(self, status: str | None) -> None:
        """Show monitor connection state without changing controller data."""
        self.monitor_transport_status = status
        if self.snapshot is not None and self.last_render_time is not None:
            try:
                self.update_snapshot(self.snapshot, self.last_render_time, append_history=False)
            except (NoMatches, WidgetError):
                # Renderer shutdown must not turn a final socket event into an app error.
                pass
        elif status is not None:
            try:
                self.query_one("#error-banner", Static).update(
                    Text(status, no_wrap=True, overflow="ellipsis")
                )
            except NoMatches:
                pass

    def set_history_query(self, query: HistoryQuery | None) -> None:
        """Set the controller-owned bounded history reader for the shared History tab."""
        self.history_query = query
        try:
            self.query_one(HistoryPanel).set_query(query)
        except NoMatches:
            pass

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Load history only while its pane is selected; cancel it on other tabs."""
        try:
            panel = self.query_one(HistoryPanel)
        except NoMatches:
            return
        if event.pane.id == "history":
            panel.activate()
        else:
            panel.deactivate()

    def set_control_available(self, available: bool) -> None:
        """Enable browser mutations only while a compatible controller is fresh."""
        self.read_only = not available
        try:
            self.query_one("#power-toggle", Button).disabled = not available
            self.query_one("#fan-settings", Button).disabled = not available
            indicator = self.query_one("#read-only-indicator", Static)
            indicator.display = not available
        except NoMatches:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button
        if button.id == "power-toggle" and not button.disabled and button.display:
            self.toggle()
        if button.id == "fan-settings" and not button.disabled and self.settings is not None:
            self.settings()

    def _render_dashboard(self, snapshot: ControlSnapshot, now: float) -> None:
        if self.graph_view == "graph-2":
            self._render_graph_two(snapshot, now)
            return
        scroll = self.query_one("#dashboard-scroll", VerticalScroll)
        terminal_width = self.screen.size.width
        warning = self.query_one("#dashboard-warning", Static)
        if terminal_width < MIN_DASHBOARD_WIDTH:
            warning.update(
                f"Dashboard width {terminal_width}; at least {MIN_DASHBOARD_WIDTH} columns required for charts."
            )
            scroll.display = False
            return
        warning.update("")
        scroll.display = True
        active_ids = {endpoint.endpoint_id for endpoint in snapshot.endpoint_snapshots}
        for endpoint_id, stale_panel in list(self.panels.items()):
            if endpoint_id not in active_ids:
                stale_panel.remove()
                del self.panels[endpoint_id]
        current = {
            (endpoint.endpoint_id, gpu.key): gpu
            for endpoint in snapshot.endpoint_snapshots
            for gpu in endpoint.gpus
        }
        keys_by_endpoint = {
            endpoint.endpoint_id: sorted(
                key for source, key in self.history.last_seen if source == endpoint.endpoint_id
            )
            for endpoint in snapshot.endpoint_snapshots
        }
        plot_height = self._plot_height(
            scroll.size.height,
            len(snapshot.endpoint_snapshots),
            sum(len(keys) for keys in keys_by_endpoint.values()),
        )
        available = max(1, scroll.scrollable_content_region.width)
        previous: Static | None = None
        for endpoint in snapshot.endpoint_snapshots:
            # Health changes on the fast control tick. Keep them in the banner so
            # a changed health state doesn't imply that cached charts were sampled
            # again or need a costly panel redraw.
            rendered = Text(_safe_display_text(endpoint.name))
            keys = keys_by_endpoint[endpoint.endpoint_id]
            for gpu_index, key in enumerate(keys):
                gpu = current.get((endpoint.endpoint_id, key))
                mem = "N/A"
                if (
                    gpu is not None
                    and gpu.memory_used_mib is not None
                    and gpu.memory_total_mib is not None
                    and gpu.memory_total_mib > 0
                ):
                    mem = f"{gpu.memory_used_mib:.0f}/{gpu.memory_total_mib:.0f} MiB ({gpu.memory_used_mib / gpu.memory_total_mib * 100:.0f}%)"
                util = (
                    "N/A"
                    if gpu is None or gpu.utilization_percent is None
                    else f"{gpu.utilization_percent:.0f}%"
                )
                temp = (
                    "N/A"
                    if gpu is None or gpu.temperature_celsius is None
                    else f"{gpu.temperature_celsius:.1f} C"
                )
                left_width = max(7, (available - 1) // 2)
                right_width = max(7, available - 1 - left_width)
                temperature_lines, temperature_color = self._chart_box(
                    endpoint.endpoint_id,
                    key,
                    "TEMP",
                    temp,
                    now,
                    right_width,
                    max(100, self.emergency_temperature),
                    plot_height,
                )
                rendered.append("\n")
                if endpoint.memory_source == "node-exporter" and gpu_index > 0:
                    temperature_lines, temperature_color = self._chart_box(
                        endpoint.endpoint_id,
                        key,
                        "TEMP",
                        temp,
                        now,
                        available,
                        max(100, self.emergency_temperature),
                        plot_height,
                    )
                    for index, temperature_line in enumerate(temperature_lines):
                        self._append_colored(rendered, temperature_line, temperature_color)
                        if index < len(temperature_lines) - 1:
                            rendered.append("\n")
                else:
                    memory_key = "__uma__" if endpoint.memory_source == "node-exporter" else key
                    memory_label = "UMA MEM" if endpoint.memory_source == "node-exporter" else "MEM"
                    if endpoint.memory_source == "node-exporter" and endpoint.uma_memory is not None:
                        usage = endpoint.uma_memory
                        mem = f"{usage.used_mib:.0f}/{usage.total_mib:.0f} MiB ({usage.used_mib / usage.total_mib * 100:.0f}%)"
                    memory_lines, memory_color = self._chart_box(
                        endpoint.endpoint_id, memory_key, memory_label, mem, now, left_width, 100, plot_height
                    )
                    for index, (memory_line, temperature_line) in enumerate(
                        zip(memory_lines, temperature_lines, strict=True)
                    ):
                        self._append_colored(rendered, memory_line, memory_color)
                        rendered.append(" ")
                        self._append_colored(rendered, temperature_line, temperature_color)
                        if index < len(memory_lines) - 1:
                            rendered.append("\n")
                rendered.append("\n")
                utilization_lines, utilization_color = self._chart_box(
                    endpoint.endpoint_id, key, "UTIL", util, now, available, 100, plot_height
                )
                for index, utilization_line in enumerate(utilization_lines):
                    self._append_colored(rendered, utilization_line, utilization_color)
                    if index < len(utilization_lines) - 1:
                        rendered.append("\n")
            panel = self.panels.get(endpoint.endpoint_id)
            if panel is None:
                # Endpoint/GPU names originate outside the UI. Rendering the panel
                # as plain text keeps them from becoming Rich markup.
                panel = Static(classes="dgx-panel", markup=False)
                self.panels[endpoint.endpoint_id] = panel
                scroll.mount(panel)
            assert panel is not None
            panel.update(rendered)
            if previous is None:
                scroll.move_child(panel, before=0)
            else:
                scroll.move_child(panel, after=previous)
            previous = panel
        self._dashboard_layout_signature = self._layout_signature(scroll)
        self._schedule_layout_check()

    def _render_graph_two(self, snapshot: ControlSnapshot, now: float) -> None:
        """Compact endpoint summary using the same live samples and 120s history.

        This intentionally lives behind the mode switch: Graph #1 retains its
        exact renderer and chart layout when the new setting is absent.
        """
        scroll = self.query_one("#dashboard-scroll", VerticalScroll)
        warning = self.query_one("#dashboard-warning", Static)
        if self.screen.size.width < MIN_DASHBOARD_WIDTH:
            warning.update(f"Dashboard width {self.screen.size.width}; at least {MIN_DASHBOARD_WIDTH} columns required for charts.")
            scroll.display = False
            return
        warning.update("")
        scroll.display = True
        panel = self.panels.get("__graph_two__")
        if panel is None:
            panel = Static(classes="dgx-panel", markup=False)
            self.panels["__graph_two__"] = panel
            scroll.mount(panel)
        for key, stale_panel in list(self.panels.items()):
            if key != "__graph_two__":
                stale_panel.remove()
                del self.panels[key]
        available = max(30, scroll.scrollable_content_region.width)
        card_width = max(30, (available - 1) // max(1, min(2, len(snapshot.endpoint_snapshots))))
        cards: list[tuple[list[str], tuple[tuple[tuple[int, int, int], ...], ...]]] = []
        for endpoint in snapshot.endpoint_snapshots[:2]:
            gpus = endpoint.gpus if endpoint.healthy and not endpoint.stale and endpoint.error is None else ()
            temperatures = [gpu.temperature_celsius for gpu in gpus if gpu.temperature_celsius is not None]
            utilization = [gpu.utilization_percent for gpu in gpus if gpu.utilization_percent is not None]
            powers = [gpu.power_watts for gpu in gpus]
            temp = "N/A" if not temperatures else f"{max(temperatures):.1f} C"
            util = "N/A" if not utilization else f"{max(utilization):.0f}%"
            if endpoint.memory_source == "node-exporter":
                memory = (
                    endpoint.uma_memory
                    if gpus and endpoint.memory_healthy and not endpoint.memory_stale and endpoint.memory_error is None
                    else None
                )
                # The dashboard is deliberately used-only.  Node-exporter can
                # report a valid used counter even while its total is absent or
                # zero, and that should remain useful to an operator.
                mem = "N/A" if memory is None else f"{int(memory.used_mib // 1024)} GiB"
            else:
                used = [gpu.memory_used_mib for gpu in gpus if gpu.memory_used_mib is not None]
                mem = "N/A" if not gpus or len(used) != len(gpus) else f"{int(sum(used) // 1024)} GiB"
            power = "N/A" if not gpus or any(value is None for value in powers) else f"{sum(value for value in powers if value is not None):.0f} W"
            util_chart = self.history.endpoint_area(endpoint.endpoint_id, "util", now, max(1, card_width - 7), 100, 2)
            util_box = self._chart_lines("UTIL", util, util_chart)
            metric_rows, metric_spans = self._large_metric_rows((temp, mem, util, power), card_width)
            cards.append(([
                _compact_display_text(f"┌ {endpoint.name}", card_width).ljust(card_width, "─"),
                *metric_rows,
                *util_box,
            ], metric_spans))
        rendered = Text()
        height = max((len(card[0]) for card in cards), default=0)
        for row in range(height):
            metric_height = max((len(spans) for _card, spans in cards), default=0)
            line = Text(style=Style(bold=1 <= row <= metric_height))
            for index, (card, metric_spans) in enumerate(cards):
                value = card[row].ljust(card_width) if row < len(card) else " " * card_width
                start = len(line.plain)
                line.append(value)
                if 1 <= row <= len(metric_spans):
                    colors = (
                        self.dashboard_colors.temperature,
                        self.dashboard_colors.memory,
                        self.dashboard_colors.utilization,
                        self.dashboard_colors.power,
                    )
                    for metric_index, offset, width in metric_spans[row - 1]:
                        color = colors[metric_index]
                        line.stylize(Style(color=_rich_color(color)), start + offset, start + offset + width)
                elif row > len(metric_spans):
                    line.stylize(
                        Style(color=_rich_color(self.dashboard_colors.utilization)),
                        start,
                        start + len(value.rstrip()),
                    )
                if index < len(cards) - 1:
                    line.append(" ")
            rendered.append(line)
            rendered.append("\n")
        labels = [f"{index + 1}:{_compact_display_text(endpoint.name, 15)}" for index, endpoint in enumerate(snapshot.endpoint_snapshots[:2])]
        rendered.append(_compact_display_text("TEMP 120s · " + " · ".join(labels) + " · X: overlap", available) + "\n")
        rendered.append(
            "\n".join(self._shared_temperature_lines(snapshot.endpoint_snapshots[:2], now, available)) + "\n",
            style=Style(color=_rich_color(self.dashboard_colors.temperature)),
        )
        panel.update(rendered)
        scroll.move_child(panel, before=0)
        self._dashboard_layout_signature = self._layout_signature(scroll)
        self._schedule_layout_check()

    def _large_metric_rows(
        self,
        values: tuple[str, str, str, str], card_width: int
    ) -> tuple[list[str], tuple[tuple[tuple[int, int, int], ...], ...]]:
        """Render a compact four-metric group, falling back before clipping.

        Native Textual digits are used in capable terminals and in managed
        tty8 only after its compatible font was activated.  An unmarked Linux
        virtual console gets plain full values instead of unreliable glyphs.
        """
        if _is_linux_virtual_console() and os.environ.get(_TTY8_FONT_MARKER) != "1":
            return self._stacked_metric_rows(values)
        labels = tuple(
            self._metric_label(label, value)
            for label, value in zip(_GRAPH_TWO_METRIC_LABELS, values, strict=True)
        )
        prepared = [self._digit_value(self._numeric_value(value)) for value in values]
        widths = [
            max(len(label), len(value[0]))
            for label, value in zip(labels, prepared, strict=True)
        ]
        spare = card_width - sum(widths)
        if spare < 0:
            return self._stacked_metric_rows(values)
        # A single spare cell still improves scanability.  Allocate it from
        # left to right instead of dividing it away to zero across all gaps.
        gaps = [1 if index < min(3, spare) else 0 for index in range(3)]
        positions: list[tuple[int, int, int]] = []
        offset = 0
        for index, width in enumerate(widths):
            positions.append((index, offset, width))
            if index < len(gaps):
                offset += width + gaps[index]

        def joined(parts: list[str]) -> str:
            return "".join(
                part + (" " * gaps[index] if index < len(gaps) else "")
                for index, part in enumerate(parts)
            )

        rows = [joined([label.center(width) for label, width in zip(labels, widths, strict=True)])]
        for row in range(len(prepared[0][1])):
            rows.append(joined([value[1][row].center(width) for value, width in zip(prepared, widths, strict=True)]))
        return rows, tuple(tuple(positions) for _ in rows)

    @staticmethod
    def _native_digit_lines(value: str) -> tuple[str, str, str]:
        """Extract the installed Textual Digits output for Static composition."""
        rows: list[str] = []
        line = ""
        for segment in Digits(value).render(Style()):
            if not isinstance(segment, Segment):
                continue
            if segment.text == "\n":
                rows.append(line)
                line = ""
            else:
                line += segment.text
        return rows[0], rows[1], rows[2]

    @staticmethod
    def _numeric_value(value: str) -> str:
        for suffix in (" GiB", " C", " W", "%"):
            if value.endswith(suffix):
                return value.removesuffix(suffix)
        return value

    @staticmethod
    def _metric_label(label: str, value: str) -> str:
        """Keep normal-size units beside their metric label in compact cards."""
        if value.endswith(" GiB"):
            return f"{label} GiB"
        if value.endswith(" C"):
            return f"{label} C"
        if value.endswith(" W"):
            return f"{label} W"
        if value.endswith("%"):
            return f"{label} %"
        return label

    @classmethod
    def _digit_value(cls, value: str) -> tuple[str, tuple[str, ...]]:
        if value == "N/A":
            blank = ("",) * 3
            middle = len(blank) // 2
            return value, (*blank[:middle], value, *blank[middle + 1:])
        lines = list(cls._native_digit_lines(value))
        width = max(len(line) for line in lines)
        return " " * width, tuple(line.ljust(width) for line in lines)

    @staticmethod
    def _stacked_metric_rows(
        values: tuple[str, str, str, str]
    ) -> tuple[list[str], tuple[tuple[tuple[int, int, int], ...], ...]]:
        rows = [f"{label} {value}" for label, value in zip(_GRAPH_TWO_METRIC_LABELS, values, strict=True)]
        return rows, tuple(((index, 0, len(row)),) for index, row in enumerate(rows))

    @staticmethod
    def _chart_lines(metric: str, value: str, chart: list[str]) -> list[str]:
        inner = max(len(row) for row in chart)
        return [
            f"┌ {metric} {value}"[: inner + 1].ljust(inner + 1, "─") + "┐",
            *(f"│{row.ljust(inner)}│" for row in chart),
            "└" + "─" * inner + "┘",
        ]

    def _shared_temperature_lines(
        self, endpoints: tuple[EndpointSnapshot, ...], now: float, total_width: int
    ) -> list[str]:
        width = max(1, total_width - 7)
        plots = [
            self.history.endpoint_area(endpoint.endpoint_id, "temp", now, width, max(100, self.emergency_temperature), 2)
            for endpoint in endpoints
        ]
        if not plots:
            return ["N/A"]
        lines: list[str] = []
        for row in range(len(plots[0]) - 1):
            prefix = plots[0][row][:5]
            marks = []
            for column in range(width):
                active = [index + 1 for index, plot in enumerate(plots) if plot[row][5 + column] != " "]
                marks.append("X" if len(active) > 1 else (str(active[0]) if active else " "))
            lines.append(prefix + "".join(marks))
        lines.append(plots[0][-1])
        return self._chart_lines("TEMP", "1/2 max; gaps blank", lines)

    @staticmethod
    def _plot_height(viewport_height: int, endpoint_count: int, gpu_count: int) -> int:
        """Reserve endpoint labels and complete borders before adding plot rows."""
        if gpu_count == 0:
            return PLOT_HEIGHT
        fixed_lines = endpoint_count + gpu_count * 6
        return min(PLOT_HEIGHT, max(1, (max(0, viewport_height) - fixed_lines) // (gpu_count * 2)))

    def _chart_box(
        self,
        endpoint_id: str,
        gpu_key: str,
        metric: str,
        value: str,
        now: float,
        total_width: int,
        maximum: float,
        plot_height: int,
    ) -> tuple[list[str], str | None]:
        chart = self.history.area(
            endpoint_id,
            gpu_key,
            "mem" if metric == "UMA MEM" else metric.lower(),
            now,
            max(1, total_width - 7),
            maximum,
            plot_height,
        )
        inner = max(len(row) for row in chart)
        box = "\n".join(
            (
                f"┌ {metric} {value}"[: inner + 1].ljust(inner + 1, "─") + "┐",
                *(f"│{row.ljust(inner)}│" for row in chart),
                "└" + "─" * inner + "┘",
            )
        )
        color = {
            "MEM": self.dashboard_colors.memory,
            "UMA MEM": self.dashboard_colors.memory,
            "UTIL": self.dashboard_colors.utilization,
            "TEMP": self.dashboard_colors.temperature,
        }[metric]
        return box.splitlines(), color

    @staticmethod
    def _append_colored(rendered: Text, value: str, color: str | None) -> None:
        if color is None:
            rendered.append(value)
        else:
            rendered.append(value, style=Style(color=_rich_color(color)))

    def on_resize(self) -> None:
        if self.snapshot is None or self.last_render_time is None:
            return
        self._layout_convergence_passes = 0
        if self._resize_redraw_pending:
            return
        self._schedule_after_refresh(self._redraw_after_resize)

    def _schedule_layout_check(self) -> None:
        if self.snapshot is None or self.last_render_time is None or self._resize_redraw_pending:
            return
        if (
            self._layout_convergence_passes < MAX_LAYOUT_CONVERGENCE_PASSES
            and self._schedule_after_refresh(self._converge_dashboard_layout)
        ):
            self._layout_convergence_passes += 1

    def _schedule_after_refresh(self, callback: Callable[[], None]) -> bool:
        self._resize_redraw_pending = True
        try:
            if not self.call_after_refresh(callback):
                self._resize_redraw_pending = False
                return False
        except RuntimeError:
            # A closing/unmounted message pump cannot accept deferred work.
            self._resize_redraw_pending = False
            return False
        return True

    def _redraw_after_resize(self) -> None:
        self._resize_redraw_pending = False
        if self.snapshot is not None and self.last_render_time is not None:
            self._render_dashboard(self.snapshot, self.last_render_time)

    def _converge_dashboard_layout(self) -> None:
        self._resize_redraw_pending = False
        if self.snapshot is None or self.last_render_time is None:
            return
        scroll = self.query_one("#dashboard-scroll", VerticalScroll)
        if self._dashboard_layout_signature != self._layout_signature(scroll):
            self._render_dashboard(self.snapshot, self.last_render_time)

    def _layout_signature(self, scroll: VerticalScroll) -> tuple[int, int, int, int, int, int]:
        region = scroll.scrollable_content_region
        return (
            self.screen.size.width,
            self.screen.size.height,
            scroll.size.width,
            scroll.size.height,
            region.width,
            region.height,
        )
