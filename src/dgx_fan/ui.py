from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.widget import WidgetError
from textual.widgets import Button, Footer, Header, Static, TabbedContent, TabPane

from .config import DashboardColors
from .models import ControlSnapshot, GPUStat, MemoryStat

HISTORY_SECONDS = 120.0
MIN_DASHBOARD_WIDTH = 79
PLOT_HEIGHT = 5
MAX_LAYOUT_CONVERGENCE_PASSES = 2
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
        def column(value: float | None, row: int) -> str:
            if value is None:
                return " "
            clamped = min(max(value, 0), maximum)
            if clamped == 0:
                return "." if row == plot_height - 1 else " "

            # Clamp first so malformed over-range telemetry cannot draw beyond
            # the plot. ceil preserves a visible positive half-row minimum.
            occupied = max(1, ceil(clamped / maximum * plot_height * 2))
            full_rows, partial = divmod(occupied, 2)
            rows_from_bottom = plot_height - 1 - row
            if rows_from_bottom < full_rows:
                return ":"
            if rows_from_bottom == full_rows and partial:
                return "."
            return " "

        rows: list[str] = []
        label_rows = {0, plot_height // 2, plot_height - 1}
        for row in range(plot_height):
            threshold = (plot_height - 1 - row) / max(1, plot_height - 1) * maximum
            line = "".join(column(value, row) for value in values)
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
        read_only: bool = False,
    ) -> None:
        super().__init__()
        self.config_path, self.toggle, self.emergency_temperature = (
            config_path,
            toggle,
            emergency_temperature,
        )
        self.dashboard_colors = dashboard_colors or DashboardColors()
        self.settings = settings
        self.read_only = read_only
        self.snapshot: ControlSnapshot | None = None
        self.history = DashboardHistory(collection_interval_seconds)
        self.panels: dict[str, Static] = {}
        self.last_render_time: float | None = None
        self.last_signature: tuple[tuple[str, int, int, bool, bool, str | None], ...] | None = None
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
    ) -> None:
        """Apply effective presentation settings without discarding chart history."""
        self.emergency_temperature = emergency_temperature
        self.dashboard_colors = dashboard_colors
        self.history.collection_interval_seconds = collection_interval_seconds
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
            rendered.append(value, style=Style(color=color))

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
