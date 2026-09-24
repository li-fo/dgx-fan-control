"""A zero-to-one-hundred-percent Sparkline for Graph #2 utilization."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from rich.console import Console, ConsoleOptions
from rich.segment import Segment
from rich.style import Style
from textual.app import RenderResult
from textual.color import Color
from textual.renderables.sparkline import Sparkline as SparklineRenderable
from textual.widgets import Sparkline, Static


def time_columns(
    values: Sequence[float], times: Sequence[float], now: float, width: int,
    window_seconds: float = 120.0,
    collection_interval_seconds: float = 0.0,
) -> tuple[float | None, ...]:
    """Bin real observations, holding only the expected short polling interval."""
    columns: list[float | None] = [None] * max(0, width)
    if width <= 0 or window_seconds <= 0:
        return tuple(columns)
    cutoff = now - window_seconds
    latest_at: list[float | None] = [None] * width
    latest_value: list[float | None] = [None] * width
    for at, value in zip(times, values):
        if not cutoff <= at <= now:
            continue
        index = min(width - 1, int((at - cutoff) / window_seconds * width))
        previous = columns[index]
        columns[index] = value if previous is None else max(previous, value)
        latest = latest_at[index]
        if latest is None or at > latest:
            latest_at[index] = at
            latest_value[index] = value
        elif at == latest:
            previous_latest = latest_value[index]
            latest_value[index] = value if previous_latest is None else max(previous_latest, value)
    hold_seconds = max(0.0, collection_interval_seconds) * 1.5
    previous_value: float | None = None
    previous_at: float | None = None
    for index, current in enumerate(columns):
        if current is not None:
            previous_value, previous_at = latest_value[index], latest_at[index]
            continue
        # Require the entire empty bin to fit inside the short hold; never
        # extend backward into prehistory or bridge a longer outage.
        bin_end = cutoff + (index + 1) * window_seconds / width
        if (
            previous_value is not None
            and previous_at is not None
            and bin_end <= previous_at + hold_seconds
        ):
            columns[index] = previous_value
    return tuple(columns)


def time_axis(width: int) -> str:
    """Place labels on the same fixed 120-second pixel grid as the plot."""
    if width <= 0:
        return ""
    axis = [" "] * width
    for position, label in ((0, "120s"), (0.5, "60s"), (0.75, "30s"), (1, "now")):
        center = min(width - 1, int(position * width))
        start = max(0, min(width - len(label), center - len(label) // 2))
        if position == 0:
            start = 0
        if position == 1:
            start = max(0, width - len(label))
        for index, character in enumerate(label[:width]):
            if 0 <= start + index < width:
                axis[start + index] = character
    return "".join(axis)


class _FixedScaleRenderable(SparklineRenderable[float]):
    """Use native Sparkline buckets and glyphs with absolute percentage bounds."""

    columns: Sequence[float | None] | None = None

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> Iterator[Segment]:
        width = self.width or options.max_width
        height = self.height or 1
        if width <= 0:
            return
        if self.columns is None and not self.data:
            for row in range(height):
                yield Segment(" " * width)
                if row < height - 1:
                    yield Segment.line()
            return

        buckets = tuple(self._buckets(list(self.data), width)) if self.columns is None else ()
        bar_levels = len(self.BARS)
        max_level = bar_levels * height - 1
        min_color = Color.from_rich_color(self.min_color.color)
        max_color = Color.from_rich_color(self.max_color.color)
        for row in reversed(range(height)):
            for column in range(width):
                value = (
                    self.summary_function(buckets[column * len(buckets) // width])
                    if self.columns is None else self.columns[column]
                )
                if value is None:
                    yield Segment(" ")
                    continue
                ratio = max(0.0, min(1.0, value / 100.0))
                level = int(ratio * max_level)
                if level < row * bar_levels:
                    yield Segment(" ")
                else:
                    glyph = "█" if level >= (row + 1) * bar_levels else self.BARS[level % bar_levels]
                    color = min_color.blend(max_color, ratio).rich_color
                    yield Segment(glyph, Style.from_color(color))
            if row:
                yield Segment.line()


class FixedScaleSparkline(Sparkline):
    """Textual Sparkline with a fixed 0–100% scale and blank absent data."""

    sample_times: tuple[float, ...] = ()
    window_end: float = 0.0
    collection_interval_seconds: float = 0.0

    def render(self) -> RenderResult:
        _, base = self.background_colors
        min_color = base + (
            self.get_component_styles("sparkline--min-color").color
            if self.min_color is None else self.min_color
        )
        max_color = base + (
            self.get_component_styles("sparkline--max-color").color
            if self.max_color is None else self.max_color
        )
        renderable = _FixedScaleRenderable(
            self.data or (),
            width=self.size.width,
            height=self.size.height,
            min_color=min_color.rich_color,
            max_color=max_color.rich_color,
            summary_function=self.summary_function,
        )
        renderable.columns = time_columns(
            self.data or (), self.sample_times, self.window_end, self.size.width,
            collection_interval_seconds=self.collection_interval_seconds,
        )
        return renderable


class UtilTimeAxis(Static):
    """One row of time labels sized to its adjacent utilization plot."""

    def render(self) -> RenderResult:
        return time_axis(self.size.width)
