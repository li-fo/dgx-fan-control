"""Shared, bounded History tab widgets for the local and browser TUI.

The widget deliberately knows nothing about SQLite, collectors, or the monitor
transport.  Its only input is a bounded, already-aggregated query callback.
That keeps the history reader owned by the controller process and lets the
terminal and browser render exactly the same timeline controls.
"""

from __future__ import annotations

from asyncio import CancelledError, Task, create_task
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from math import isfinite
from time import time
from typing import ClassVar, Final

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import Button, Static

from .config import DashboardColors, EndpointConfig

HistoryQuery = Callable[[str, float, float, int], Awaitable[dict[str, object]]]

MAX_HISTORY_SECONDS: Final = 8 * 24 * 60 * 60
MAX_QUERY_WIDTH: Final = 1024
QUERY_DEBOUNCE_SECONDS: Final = 0.08
ZOOM_SPANS: Final[tuple[int, ...]] = (
    8 * 24 * 60 * 60,
    24 * 60 * 60,
    6 * 60 * 60,
    60 * 60,
    10 * 60,
    60,
)
METRICS: Final[tuple[tuple[str, str, float, str, str], ...]] = (
    ("memory", "Memory mean %", 100.0, "memory", "%"),
    ("utilization", "UTIL max %", 100.0, "utilization", "%"),
    ("temperature", "Temp max C", 100.0, "temperature", " C"),
    ("power", "Power max · fixed 240 W", 240.0, "power", " W"),
)


def _safe_text(value: object) -> str:
    return "".join(
        "\N{REPLACEMENT CHARACTER}" if ord(char) < 32 or 127 <= ord(char) <= 159 else char
        for char in str(value)
    )


def _local_time(timestamp: float) -> datetime:
    """Convert UTC epoch samples for display in the Pi's configured local timezone."""
    return datetime.fromtimestamp(timestamp, tz=UTC).astimezone()


def _rich_color(color: str | None) -> str | None:
    """Translate the config's terminal-safe ``ansi_*`` aliases for Rich text."""
    return color.removeprefix("ansi_") if color else None


class HistoryChart(Static):
    """Compact portable ASCII chart for a single history series."""

    DEFAULT_CSS = """
    HistoryChart {
        height: 1fr;
        min-height: 3;
        width: 1fr;
        border: round $primary;
        padding: 0;
    }
    """

    def __init__(
        self, metric: str, label: str, maximum: float, color: str | None, unit: str
    ) -> None:
        super().__init__(id=f"history-{metric}-chart", markup=False)
        self.metric, self.label, self.maximum, self.color, self.unit = (
            metric,
            label,
            maximum,
            color,
            unit,
        )
        self.base_maximum = maximum
        self.values: list[float | None] = []
        self.start = 0.0
        self.end = 0.0
        self.status = "Waiting for history…"

    def update_series(
        self,
        values: list[float | None],
        start: float,
        end: float,
        status: str,
    ) -> None:
        self.values, self.start, self.end, self.status = values, start, end, status
        observed = [value for value in values if value is not None and isfinite(value)]
        # Power intentionally stays at a fixed 240 W display scale. Values
        # remain unchanged in ``self.values`` and are only clamped while drawn.
        self.maximum = (
            self.base_maximum
            if self.metric == "power"
            else max(self.base_maximum, max(observed, default=self.base_maximum))
        )
        self._refresh_render()

    def _refresh_render(self) -> None:
        width = max(1, self.content_size.width)
        values = self._fit_values(width)
        rows = max(1, self.content_size.height)
        self.border_title = self.label
        content = Text()
        color = _rich_color(self.color)
        style = Style(color=color) if color else None
        for row in range(rows):
            line = "".join(self._glyph(value, row, rows) for value in values)
            content.append(line, style=style)
            if row < rows - 1:
                content.append("\n")
        self.update(content)

    def _fit_values(self, width: int) -> list[float | None]:
        if not self.values:
            return [None] * width
        if len(self.values) == width:
            return self.values
        result: list[float | None] = []
        for column in range(width):
            left = int(column / width * len(self.values))
            right = max(left + 1, int((column + 1) / width * len(self.values)))
            group = [value for value in self.values[left:right] if value is not None]
            result.append(None if not group else max(group))
        return result

    def _glyph(self, value: float | None, row: int, rows: int) -> str:
        if value is None or not isfinite(value):
            return " "
        clamped = min(max(value, 0.0), self.maximum)
        half_rows = max(1, round(clamped / self.maximum * rows * 2)) if clamped else 0
        full_rows, partial = divmod(half_rows, 2)
        top = rows - full_rows - partial
        if row < top:
            return " "
        return "." if row == top and partial else ":"

    def on_resize(self) -> None:
        self._refresh_render()


class HistoryPanel(Vertical):
    """Endpoint selector, shared timeline, and four bounded history charts."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left", "pan_left", "Pan left", show=False),
        Binding("right", "pan_right", "Pan right", show=False),
    ]
    DEFAULT_CSS = """
    HistoryPanel { height: 1fr; width: 1fr; }
    #history-controls { height: 1; }
    #history-controls Button {
        height: 1; min-height: 1; border: none !important; padding: 0;
    }
    Button.history-endpoint { width: auto; min-width: 8; margin-right: 1; }
    #history-spacer { width: 1fr; }
    #history-zoom-out, #history-zoom-in { width: 3; margin-left: 1; }
    #history-controls Button.history-endpoint.-primary, #history-controls Button:focus {
        text-style: bold reverse;
        background: ansi_default !important;
        border: none !important;
    }
    #history-controls Button:hover, #history-controls Button.-active {
        border: none !important;
    }
    #history-range { width: 8; content-align: center middle; }
    #history-status { height: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #history-plots { height: 1fr; }
    #history-timeline { height: 3; min-height: 3; border: round $primary; padding: 0; }
    """

    def __init__(
        self,
        endpoints: tuple[EndpointConfig, ...],
        query: HistoryQuery | None = None,
        colors: DashboardColors | None = None,
        interval_seconds: float = 2.0,
    ) -> None:
        super().__init__(id="history-panel")
        self.endpoints = endpoints
        self.history_query = query
        self.dashboard_colors = colors or DashboardColors()
        self.interval_seconds = interval_seconds
        self.endpoint_id = endpoints[0].id if endpoints else None
        self._endpoint_button_ids = tuple(
            f"history-endpoint-{index}" for index in range(len(endpoints))
        )
        self.span_index = ZOOM_SPANS.index(60 * 60)
        self.end = time()
        self.live = True
        self._request: Task[None] | None = None
        self._request_generation = 0
        self._drag_x: float | None = None
        self._mounted_active = False
        self._live_timer: Timer | None = None
        self._debounce_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="history-controls"):
            for index, endpoint in enumerate(self.endpoints):
                yield Button(
                    Text(_safe_text(endpoint.name)),
                    id=self._endpoint_button_ids[index],
                    classes="history-endpoint",
                )
            yield Static(id="history-spacer")
            yield Button("-", id="history-zoom-out")
            yield Static("1 hour", id="history-range")
            yield Button("+", id="history-zoom-in")
        yield Static("History waiting", id="history-status", markup=False)
        with Vertical(id="history-plots"):
            for metric, label, maximum, color_name, unit in METRICS:
                color = getattr(self.dashboard_colors, color_name, None)
                if metric == "power" and color is None:
                    color = "ansi_green"
                yield HistoryChart(metric, label, maximum, color, unit)
        yield Static("Timeline · select History to load", id="history-timeline", markup=False)

    def set_query(self, query: HistoryQuery | None) -> None:
        self.history_query = query
        if self._mounted_active:
            self._queue_refresh()

    def reconfigure(self, colors: DashboardColors, interval_seconds: float) -> None:
        self.dashboard_colors, self.interval_seconds = colors, interval_seconds
        for metric, _, _, color_name, _ in METRICS:
            chart = self.query_one(f"#history-{metric}-chart", HistoryChart)
            chart.color = getattr(colors, color_name, None)
            if metric == "power" and chart.color is None:
                chart.color = "ansi_green"
            chart._refresh_render()
        if self._mounted_active:
            self._start_live_timer()
            self._queue_refresh()

    def activate(self) -> None:
        self._mounted_active = True
        self.live = True
        self.end = time()
        self._start_live_timer()
        self.refresh_history()

    def deactivate(self) -> None:
        self._mounted_active = False
        self._stop_timers()
        self._cancel_request()

    def on_mount(self) -> None:
        self._update_controls()

    def on_unmount(self) -> None:
        self._stop_timers()
        self._cancel_request()

    def on_resize(self) -> None:
        if self._mounted_active:
            self._queue_refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        identifier = event.button.id or ""
        if identifier in self._endpoint_button_ids:
            index = self._endpoint_button_ids.index(identifier)
            self.endpoint_id = self.endpoints[index].id
            self._queue_refresh()
        elif identifier == "history-zoom-in":
            self._zoom(1)
        elif identifier == "history-zoom-out":
            self._zoom(-1)
        event.stop()

    def action_pan_left(self) -> None:
        self._pan(-0.25)

    def action_pan_right(self) -> None:
        self._pan(0.25)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.widget is self.query_one("#history-timeline", Static) and event.button == 1:
            self._drag_x = self._screen_x(event)
            self.capture_mouse()
            event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_x is None:
            return
        width = max(1, self.query_one("#history-timeline", Static).content_size.width)
        delta = self._screen_x(event) - self._drag_x
        if abs(delta) >= 1:
            self._pan(-delta / width, refresh=False)
            self._drag_x = self._screen_x(event)
            self._queue_refresh()
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._drag_x is not None:
            self._drag_x = None
            self.capture_mouse(False)
            event.stop()

    def _zoom(self, direction: int) -> None:
        self.span_index = min(max(0, self.span_index + direction), len(ZOOM_SPANS) - 1)
        # Zoom is a fresh live-range selection; dragging/arrow pan is the
        # deliberate path for historical inspection.
        self.live = True
        self.end = time()
        self._clamp_window()
        self._queue_refresh()

    def _pan(self, proportion: float, *, refresh: bool = True) -> None:
        self.live = False
        self.end += ZOOM_SPANS[self.span_index] * proportion
        self._clamp_window()
        if refresh:
            self._queue_refresh()

    def _clamp_window(self) -> None:
        now = time()
        span = ZOOM_SPANS[self.span_index]
        self.end = min(now, max(now - MAX_HISTORY_SECONDS + span, self.end))

    def _update_controls(self) -> None:
        span = ZOOM_SPANS[self.span_index]
        labels = {8 * 86400: "8 days", 86400: "1 day", 21600: "6 hours", 3600: "1 hour", 600: "10 min", 60: "1 min"}
        try:
            self.query_one("#history-range", Static).update(labels[span])
            timeline = self.query_one("#history-timeline", Static)
            start = self.end - span
            state = "live" if self.live else "past"
            timeline.update(
                f"Timeline · {state} · {_local_time(start).strftime('%m-%d %H:%M')}"
                f" — {_local_time(self.end).strftime('%m-%d %H:%M')} · drag or ←/→"
            )
            for index, endpoint in enumerate(self.endpoints):
                self.query_one(f"#{self._endpoint_button_ids[index]}", Button).variant = (
                    "primary" if endpoint.id == self.endpoint_id else "default"
                )
        except NoMatches:
            return

    def refresh_history(self) -> None:
        if not self._mounted_active or self.history_query is None or self.endpoint_id is None:
            return
        if self.live:
            self.end = time()
        self._clamp_window()
        self._update_controls()
        self._set_status("Loading history…")
        try:
            chart = self.query_one("#history-memory-chart", HistoryChart)
        except NoMatches:
            return
        width = min(MAX_QUERY_WIDTH, max(1, chart.content_size.width))
        self._request_generation += 1
        generation = self._request_generation
        self._cancel_request()
        self._request = create_task(self._load(generation, self.endpoint_id, self.end - ZOOM_SPANS[self.span_index], self.end, width))

    def _queue_refresh(self) -> None:
        """Collapse bursty resize, pan, and endpoint interactions into one query."""
        if not self._mounted_active:
            return
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
        self._debounce_timer = self.set_timer(QUERY_DEBOUNCE_SECONDS, self._run_queued_refresh)

    def _run_queued_refresh(self) -> None:
        self._debounce_timer = None
        self.refresh_history()

    def _start_live_timer(self) -> None:
        if self._live_timer is not None:
            self._live_timer.stop()
        self._live_timer = self.set_interval(
            max(0.1, self.interval_seconds), self._refresh_live_history
        )

    def _refresh_live_history(self) -> None:
        # A full eight-day query can legitimately outlive the collection
        # interval. Do not repeatedly cancel it from the live timer: that
        # would starve the first useful result forever under load. Explicit
        # operator navigation still supersedes an older request normally.
        if (
            self._mounted_active
            and self.live
            and self._debounce_timer is None
            and (self._request is None or self._request.done())
        ):
            self.refresh_history()

    def _stop_timers(self) -> None:
        if self._live_timer is not None:
            self._live_timer.stop()
            self._live_timer = None
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
            self._debounce_timer = None

    @staticmethod
    def _screen_x(event: events.MouseDown | events.MouseMove) -> float:
        """Use stable screen coordinates while the pointer is captured."""
        return float(event.screen_x if event.screen_x is not None else event.x)

    async def _load(self, generation: int, endpoint_id: str, start: float, end: float, width: int) -> None:
        assert self.history_query is not None
        try:
            result = await self.history_query(endpoint_id, start, end, width)
        except CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - non-fatal display-only query boundary.
            self._apply_unavailable(generation, start, end, f"History unavailable: {_safe_text(error)}")
            return
        if generation != self._request_generation or not self._mounted_active:
            return
        series = result.get("series")
        status = result.get("status")
        if not isinstance(series, dict):
            self._apply_unavailable(generation, start, end, "History unavailable: invalid response")
            return
        status_text = _safe_text(status) if isinstance(status, str) else "History ready"
        self._set_status(status_text)
        for metric, _, _, _, _ in METRICS:
            values = series.get(metric)
            normalized = values if isinstance(values, list) and len(values) == width else [None] * width
            safe_values = [
                float(value)
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                and isfinite(float(value))
                else None
                for value in normalized
            ]
            self.query_one(f"#history-{metric}-chart", HistoryChart).update_series(
                safe_values, start, end, status_text
            )
        self._update_controls()

    def _apply_unavailable(self, generation: int, start: float, end: float, status: str) -> None:
        if generation != self._request_generation or not self._mounted_active:
            return
        try:
            chart = self.query_one("#history-memory-chart", HistoryChart)
        except NoMatches:
            return
        width = max(1, chart.content_size.width)
        self._set_status(status)
        for metric, _, _, _, _ in METRICS:
            self.query_one(f"#history-{metric}-chart", HistoryChart).update_series([None] * width, start, end, status)

    def _set_status(self, status: str) -> None:
        """Keep reader failures visible independently of valid chart time axes."""
        try:
            self.query_one("#history-status", Static).update(
                Text(_safe_text(status), no_wrap=True, overflow="ellipsis")
            )
        except NoMatches:
            pass

    def _cancel_request(self) -> None:
        if self._request is not None and not self._request.done():
            self._request.cancel()
        self._request = None
