from __future__ import annotations

import os
import sys
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from math import atan2, ceil, pi, sqrt

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.color import Color
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.renderables.digits import Digits
from textual.widget import WidgetError
from textual.widgets import Button, Footer, Header, Static, TabbedContent, TabPane

from .config import DashboardColors, EndpointConfig
from .history_ui import HistoryPanel, HistoryQuery
from .models import ControlSnapshot, EndpointSnapshot, GPUStat, MemoryStat
from .ui_sparkline import FixedScaleSparkline, UtilTimeAxis

HISTORY_SECONDS = 120.0
MIN_DASHBOARD_WIDTH = 79
PLOT_HEIGHT = 5
MAX_LAYOUT_CONVERGENCE_PASSES = 2
_GRAPH_TWO_METRIC_LABELS = ("UTIL", "MEM", "TEMP", "POWER")
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
            if gpus:
                powers = [gpu.power_watts for gpu in gpus]
                if all(value is not None for value in powers):
                    self.points[(endpoint_id, "__power__", "power")].append(
                        HistoryPoint(now, sum(value for value in powers if value is not None))
                    )
                if memory_source == "dcgm":
                    used = [gpu.memory_used_mib for gpu in gpus]
                    totals = [gpu.memory_total_mib for gpu in gpus]
                    if all(value is not None for value in used) and all(
                        value is not None and value > 0 for value in totals
                    ):
                        total = sum(value for value in totals if value is not None)
                        self.points[(endpoint_id, "__weighted__", "mem")].append(
                            HistoryPoint(now, sum(value for value in used if value is not None) / total * 100)
                        )
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

    def endpoint_values(self, endpoint_id: str, metric: str, now: float) -> tuple[float, ...]:
        """Return observed 120-second per-endpoint maxima without fabricating gaps."""
        return tuple(point.value for point in self.endpoint_samples(endpoint_id, metric, now))

    def endpoint_samples(
        self, endpoint_id: str, metric: str, now: float,
        gpu_keys: frozenset[str] | None = None,
    ) -> tuple[HistoryPoint, ...]:
        """Return observed 120-second per-endpoint maxima with their sample times."""
        cutoff = now - HISTORY_SECONDS
        values_by_time: dict[float, float] = {}
        for (source, gpu, source_metric), points in self.points.items():
            if source != endpoint_id or source_metric != metric or (
                gpu_keys is not None and gpu not in gpu_keys
            ):
                continue
            for point in points:
                if cutoff <= point.at <= now:
                    previous = values_by_time.get(point.at)
                    values_by_time[point.at] = point.value if previous is None else max(previous, point.value)
        return tuple(HistoryPoint(at, values_by_time[at]) for at in sorted(values_by_time))

    def chart_samples(
        self, endpoint_id: str, metric: str, now: float,
        gpu_keys: frozenset[str], memory_source: str,
    ) -> tuple[HistoryPoint, ...]:
        """Use current GPUs for maxima and source-exclusive endpoint aggregates."""
        if metric in ("util", "temp"):
            return self.endpoint_samples(endpoint_id, metric, now, gpu_keys)
        gpu = "__uma__" if metric == "mem" and memory_source == "node-exporter" else (
            "__weighted__" if metric == "mem" else "__power__"
        )
        cutoff = now - HISTORY_SECONDS
        return tuple(
            point for point in self.points.get((endpoint_id, gpu, metric), ())
            if cutoff <= point.at <= now
        )


class FanGauge(Static):
    """Display independent PWM demand and measured fan tach state."""

    SEGMENTS = 12
    RING_WIDTH = 15
    DENSE_WIDTH = 17
    DENSE_HEIGHT = 9
    _BRAILLE_BITS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))
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
        self.duty_percent: int | None = None
        self.rpm: float | None = None
        self.fan_state = "WAITING"
        self.overall_state: str | None = None
        super().__init__(self._content(number, None, None, "WAITING"), id=f"fan-{number}-gauge")

    @classmethod
    def _content(
        cls, number: int, duty_percent: int | None, rpm: float | None, state: str,
        *, override: bool = False,
    ) -> Text:
        """Keep the original simple ring for a real Linux virtual console."""
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
            + f"\nRPM: {rpm_text} | {state}{' | OVERRIDE' if override else ''}"
        )

    @classmethod
    @lru_cache(maxsize=1)
    def _dense_geometry(cls) -> tuple[tuple[int, int, int, float], ...]:
        """Raster one bounded ellipse into Braille dots, ordered from 12 o'clock."""
        dots: list[tuple[int, int, int, float]] = []
        for row in range(cls.DENSE_HEIGHT):
            for column in range(cls.DENSE_WIDTH):
                for dot_x in range(2):
                    for dot_y in range(4):
                        x = (column * 2 + dot_x + 0.5 - cls.DENSE_WIDTH) / (cls.DENSE_WIDTH - 1)
                        y = (row * 4 + dot_y + 0.5 - cls.DENSE_HEIGHT * 2) / (cls.DENSE_HEIGHT * 2 - 1)
                        radius = sqrt(x * x + y * y)
                        if not 0.79 <= radius <= 1.13:
                            continue
                        progress = (atan2(x, -y) % (2 * pi)) / (2 * pi)
                        dots.append((row, column, cls._BRAILLE_BITS[dot_x][dot_y], progress))
        return tuple(dots)

    @classmethod
    def _dense_content(
        cls, number: int, duty_percent: int | None, rpm: float | None, state: str,
        *, override: bool = False,
    ) -> Text:
        """A nine-row ring with native digits, retaining separate measured RPM."""
        percentage = "--" if duty_percent is None else f"{duty_percent}%"
        level = None if duty_percent is None else max(0, min(100, duty_percent)) / 100
        accent = (
            "red" if state == "STALLED" else "yellow" if state == "NO TACH"
            else "bright_black" if state in ("WAITING", "STOPPED") else "cyan"
        )
        track = [[0] * cls.DENSE_WIDTH for _ in range(cls.DENSE_HEIGHT)]
        filled = [[False] * cls.DENSE_WIDTH for _ in range(cls.DENSE_HEIGHT)]
        center = [[False] * cls.DENSE_WIDTH for _ in range(cls.DENSE_HEIGHT)]
        for row, column, bit, progress in cls._dense_geometry():
            track[row][column] |= bit
            if level is not None and progress < level:
                filled[row][column] = True
        glyphs = [[chr(0x2800 | mask) if mask else " " for mask in row] for row in track]
        if duty_percent is None:
            digit_lines = ("", "--", "")
        else:
            digit_lines = FanAppUI._native_digit_lines(str(duty_percent))
        digit_start = (cls.DENSE_HEIGHT - len(digit_lines)) // 2
        for digit_row, line in enumerate(digit_lines):
            row = digit_start + digit_row
            value = line + ("%" if digit_row == 1 and duty_percent is not None else "")
            start = (cls.DENSE_WIDTH - len(value)) // 2
            for offset, character in enumerate(value):
                glyphs[row][start + offset] = character
                filled[row][start + offset] = False
                center[row][start + offset] = character != " "
        result = Text(f"Fan {number} · PWM {percentage}\n")
        for row in range(cls.DENSE_HEIGHT):
            for column, glyph in enumerate(glyphs[row]):
                color = accent if filled[row][column] or center[row][column] else "bright_black"
                result.append(glyph, style=Style(color=color))
            result.append("\n")
        rpm_text = "N/A" if rpm is None else f"{rpm:.0f} RPM"
        result.append(f"RPM: {rpm_text} | ")
        result.append(state, style=Style(color=accent))
        if override:
            result.append(" | OVERRIDE", style=Style(color="red"))
        return result

    @staticmethod
    def _compact_content(
        number: int, duty_percent: int | None, rpm: float | None, state: str,
        *, override: bool = False,
    ) -> Text:
        percentage = "--" if duty_percent is None else f"{duty_percent}%"
        rpm_text = "N/A" if rpm is None else f"{rpm:.0f} RPM"
        return Text(
            f"F{number} PWM {percentage}\nRPM {rpm_text}\n"
            + state + (" OVR" if override else "")
        )

    def _refresh_display(self) -> None:
        override = self.overall_state == "SAFETY OVERRIDE"
        if self.content_size.width < self.RING_WIDTH or self.content_size.height < 7:
            content = self._compact_content
        elif (
            _is_linux_virtual_console()
            or self.content_size.width < self.DENSE_WIDTH
            or self.content_size.height < self.DENSE_HEIGHT + 2
        ):
            content = self._content
        else:
            content = self._dense_content
        self.update(content(self.number, self.duty_percent, self.rpm, self.fan_state, override=override))

    def on_mount(self) -> None:
        self._refresh_display()

    def on_resize(self) -> None:
        self._refresh_display()

    def set_reading(
        self, duty_percent: int | None, rpm: float | None, state: str,
        overall_state: str | None = None,
    ) -> None:
        reading = (duty_percent, rpm, state, overall_state)
        if reading == (self.duty_percent, self.rpm, self.fan_state, self.overall_state):
            return
        self.duty_percent, self.rpm, self.fan_state, self.overall_state = reading
        self._refresh_display()


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
    #graph-two-metric-row { width: 1fr; }
    .graph-two-endpoint-card { width: 1fr; }
    .graph-two-metric-card { width: 1fr; }
    .graph-two-metric-label { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    .graph-two-metric-sparkline { width: 1fr; }
    .graph-two-metric-axis { height: 1; width: 1fr; }
    .graph-two-metric-gutter { width: 2; }
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
        self.graph_two_metric_row: Horizontal | None = None
        self.graph_two_endpoint_cards: dict[str, Vertical] = {}
        self.graph_two_metric_cards: dict[tuple[str, str], Vertical] = {}
        self.graph_two_util_labels: dict[str, Static] = {}
        self.graph_two_metric_labels: dict[tuple[str, str], Static] = {}
        self.graph_two_sparklines: dict[tuple[str, str], FixedScaleSparkline] = {}
        self.graph_two_endpoint_ids: tuple[str, ...] = ()
        self._fan_gauge_height = 9
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
        elif self.graph_view == "graph-2":
            for sparkline in self.graph_two_sparklines.values():
                sparkline.window_end = now
                sparkline.refresh()
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
                snapshot.duty_percents[number - 1], fan.rpm, fan.state, snapshot.state,
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
        if event.pane.id == "fan-control":
            self.call_after_refresh(self._size_fan_gauges)
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

    def _size_fan_gauges(self) -> None:
        """Use the taller graphical ring only when the fan pane can show it all."""
        try:
            active = self.query_one(TabbedContent).active
        except NoMatches:
            return
        if active != "fan-control":
            return
        pane = self.query_one("#fan-control", TabPane)
        top = self.query_one("#fan-top-row", Horizontal)
        row = self.query_one("#fan-gauge-row", Horizontal)
        gauges = tuple(row.query(FanGauge))
        if len(gauges) != 2:
            return
        height = 13 if (
            not _is_linux_virtual_console()
            and pane.content_size.height - top.size.height >= 13
            and all(gauge.content_size.width >= FanGauge.DENSE_WIDTH for gauge in gauges)
        ) else 9
        if self._fan_gauge_height != height:
            self._fan_gauge_height = height
            row.styles.height = height
            for gauge in gauges:
                gauge.styles.height = height
        self.call_after_refresh(self._refresh_fan_gauges)

    def _refresh_fan_gauges(self) -> None:
        for gauge in self.query(FanGauge):
            gauge._refresh_display()

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
        self._clear_graph_two_widgets()
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
        for key, stale_panel in list(self.panels.items()):
            if key != "__graph_two__":
                stale_panel.remove()
                del self.panels[key]
        available = max(30, scroll.scrollable_content_region.width)
        endpoints = snapshot.endpoint_snapshots[:2]
        gap = 2 if len(endpoints) == 2 else 0
        card_width = max(30, (available - gap) // max(1, len(endpoints)))
        self._ensure_graph_two_widgets(scroll, panel, endpoints)
        cards: list[tuple[list[str], tuple[tuple[tuple[int, int, int], ...], ...]]] = []
        for endpoint in endpoints:
            gpus = endpoint.gpus if endpoint.healthy and not endpoint.stale and endpoint.error is None else ()
            temperatures = [gpu.temperature_celsius for gpu in gpus if gpu.temperature_celsius is not None]
            utilization = [gpu.utilization_percent for gpu in gpus if gpu.utilization_percent is not None]
            powers = [gpu.power_watts for gpu in gpus]
            temp = "N/A" if not temperatures else f"{max(temperatures):.0f} C"
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
            metric_rows, metric_spans = self._large_metric_rows((util, mem, temp, power), card_width)
            cards.append(([
                _compact_display_text(f"┌ {endpoint.name}", card_width).ljust(card_width, "─"),
                *metric_rows,
            ], metric_spans))
            gpu_keys = frozenset(gpu.key for gpu in gpus)
            memory_ready = (
                endpoint.memory_healthy and not endpoint.memory_stale
                and endpoint.memory_error is None
            )
            dcgm_memory_ready = bool(gpus) and all(
                gpu.memory_used_mib is not None
                and gpu.memory_total_mib is not None and gpu.memory_total_mib > 0
                for gpu in gpus
            )
            metric_specs = (
                ("util", 100.0, self.dashboard_colors.utilization, util, "%", bool(gpus)),
                ("mem", 100.0, self.dashboard_colors.memory, "", "%", memory_ready and (
                    (
                        endpoint.memory_source == "node-exporter"
                        and endpoint.uma_memory is not None and endpoint.uma_memory.total_mib > 0
                    ) or (endpoint.memory_source == "dcgm" and dcgm_memory_ready)
                )),
                ("temp", max(100.0, self.emergency_temperature), self.dashboard_colors.temperature, temp, " C", bool(gpus)),
                ("power", 240.0, self.dashboard_colors.power, power, " W", bool(gpus) and power != "N/A"),
            )
            for metric, maximum, configured_color, current, unit, available_metric in metric_specs:
                samples = self.history.chart_samples(
                    endpoint.endpoint_id, metric, now, gpu_keys, endpoint.memory_source,
                ) if available_metric else ()
                if metric == "mem":
                    current = "N/A" if not samples else f"{samples[-1].value:.0f}%"
                sparkline = self.graph_two_sparklines[(endpoint.endpoint_id, metric)]
                sparkline.collection_interval_seconds = self.collection_interval_seconds
                sparkline.sample_times = tuple(sample.at for sample in samples)
                sparkline.window_end = now
                sparkline.data = tuple(sample.value for sample in samples)
                sparkline.maximum = maximum
                sparkline.min_color = self._sparkline_color(configured_color)
                sparkline.max_color = self._sparkline_color(configured_color)
                sparkline.refresh()
                scale = f"0–{maximum:.0f}{unit}"
                label = Text(_compact_display_text(f"{metric.upper()} {current} · {scale}", card_width))
                if configured_color is not None:
                    label.stylize(Style(color=_rich_color(configured_color)), 0, len(metric))
                self.graph_two_metric_labels[(endpoint.endpoint_id, metric)].update(
                    label
                )
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
                        self.dashboard_colors.utilization,
                        self.dashboard_colors.memory,
                        self.dashboard_colors.temperature,
                        self.dashboard_colors.power,
                    )
                    for metric_index, offset, width in metric_spans[row - 1]:
                        color = colors[metric_index]
                        line.stylize(Style(color=_rich_color(color)), start + offset, start + offset + width)
                if index < len(cards) - 1:
                    line.append(" " * gap)
            rendered.append(line)
            if row < height - 1:
                rendered.append("\n")
        panel.update(rendered)
        # The summary is above four label/plot/axis cards. Give any leftover
        # viewport rows to the first plots instead of leaving a bottom gap.
        viewport_height = scroll.scrollable_content_region.height
        plot_rows = max(0, viewport_height - height - 4 * 2)
        base_height, extra_rows = divmod(plot_rows, 4)
        plot_heights = {
            metric: max(1, base_height + (index < extra_rows))
            for index, metric in enumerate(("util", "mem", "temp", "power"))
        }
        row_height = sum(plot_heights.values()) + 4 * 2
        assert self.graph_two_metric_row is not None
        self.graph_two_metric_row.styles.height = row_height
        for endpoint_card in self.graph_two_endpoint_cards.values():
            endpoint_card.styles.height = row_height
        for (_endpoint_id, metric), metric_card in self.graph_two_metric_cards.items():
            metric_card.styles.height = plot_heights[metric] + 2
        for (_endpoint_id, metric), sparkline in self.graph_two_sparklines.items():
            sparkline.styles.height = plot_heights[metric]
        for gutter in self.graph_two_metric_row.query(".graph-two-metric-gutter"):
            gutter.styles.height = row_height
        self._dashboard_layout_signature = self._layout_signature(scroll)
        self._schedule_layout_check()

    def _ensure_graph_two_widgets(
        self, scroll: VerticalScroll, panel: Static, endpoints: tuple[EndpointSnapshot, ...]
    ) -> None:
        """Mount four timestamped metric charts in each Graph #2 endpoint column."""
        endpoint_ids = tuple(endpoint.endpoint_id for endpoint in endpoints)
        if self.graph_two_endpoint_ids == endpoint_ids and self.graph_two_metric_row is not None:
            return
        self._clear_graph_two_widgets()
        endpoint_cards: list[Vertical] = []
        for index, endpoint in enumerate(endpoints):
            metric_cards: list[Vertical] = []
            for metric in ("util", "mem", "temp", "power"):
                key = (endpoint.endpoint_id, metric)
                label = Static(
                    classes="graph-two-metric-label" + (" graph-two-util-label" if metric == "util" else ""),
                    markup=False,
                )
                sparkline = FixedScaleSparkline(
                    (), id=f"graph-two-{metric}-{index}",
                    classes="graph-two-metric-sparkline" + (
                        " graph-two-util-sparkline" if metric == "util" else ""
                    ),
                )
                metric_card = Vertical(
                    label, sparkline, UtilTimeAxis(classes="graph-two-metric-axis"),
                    classes="graph-two-metric-card",
                )
                self.graph_two_metric_labels[key] = label
                self.graph_two_sparklines[key] = sparkline
                self.graph_two_metric_cards[key] = metric_card
                if metric == "util":
                    self.graph_two_util_labels[endpoint.endpoint_id] = label
                metric_cards.append(metric_card)
            endpoint_card = Vertical(*metric_cards, classes="graph-two-endpoint-card")
            endpoint_cards.append(endpoint_card)
            self.graph_two_endpoint_cards[endpoint.endpoint_id] = endpoint_card
        util_children: list[Vertical | Static] = []
        for card in endpoint_cards:
            if util_children:
                util_children.append(Static("  ", classes="graph-two-metric-gutter", markup=False))
            util_children.append(card)
        self.graph_two_metric_row = Horizontal(*util_children, id="graph-two-metric-row")
        self.graph_two_endpoint_ids = endpoint_ids
        if panel.parent is None:
            scroll.mount(panel, self.graph_two_metric_row)
        else:
            scroll.mount(self.graph_two_metric_row, after=panel)

    def _clear_graph_two_widgets(self) -> None:
        """Remove Graph #2-only mounted widgets before graph mode or endpoint changes."""
        if self.graph_two_metric_row is not None:
            self.graph_two_metric_row.remove()
        self.graph_two_metric_row = None
        self.graph_two_endpoint_cards.clear()
        self.graph_two_metric_cards.clear()
        self.graph_two_util_labels.clear()
        self.graph_two_metric_labels.clear()
        self.graph_two_sparklines.clear()
        self.graph_two_endpoint_ids = ()

    @staticmethod
    def _sparkline_color(color: str | None) -> Color | None:
        """Use the configured terminal-safe color for the native widget."""
        return None if color is None else Color.parse(_rich_color(color))

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
        numeric_values = tuple(self._numeric_value(value) for value in values)
        if card_width < 39 or any(not value.isdecimal() or len(value) > 3 for value in numeric_values):
            return self._stacked_metric_rows(values)
        labels = tuple(
            self._metric_label(label, value)
            for label, value in zip(_GRAPH_TWO_METRIC_LABELS, values, strict=True)
        )
        prepared = [self._digit_value(value) for value in numeric_values]
        gap = max(1, (card_width - 36) // 3)
        positions = tuple((index, index * (9 + gap), 9) for index in range(4))
        separator = " " * gap
        rows = [separator.join(label.center(9) for label in labels)]
        for row in range(len(prepared[0][1])):
            rows.append(separator.join(value[1][row].center(9) for value in prepared))
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
        self._size_fan_gauges()
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
