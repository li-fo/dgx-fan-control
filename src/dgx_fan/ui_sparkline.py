"""A zero-to-one-hundred-percent Sparkline for Graph #2 utilization."""

from __future__ import annotations

from collections.abc import Iterator

from rich.console import Console, ConsoleOptions
from rich.segment import Segment
from rich.style import Style
from textual.app import RenderResult
from textual.color import Color
from textual.renderables.sparkline import Sparkline as SparklineRenderable
from textual.widgets import Sparkline


class _FixedScaleRenderable(SparklineRenderable[float]):
    """Use native Sparkline buckets and glyphs with absolute percentage bounds."""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> Iterator[Segment]:
        width = self.width or options.max_width
        height = self.height or 1
        if width <= 0:
            return
        if not self.data:
            for row in range(height):
                yield Segment(" " * width)
                if row < height - 1:
                    yield Segment.line()
            return

        buckets = tuple(self._buckets(list(self.data), width))
        bar_levels = len(self.BARS)
        max_level = bar_levels * height - 1
        min_color = Color.from_rich_color(self.min_color.color)
        max_color = Color.from_rich_color(self.max_color.color)
        for row in reversed(range(height)):
            for column in range(width):
                bucket = buckets[column * len(buckets) // width]
                ratio = max(0.0, min(1.0, self.summary_function(bucket) / 100.0))
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
        return _FixedScaleRenderable(
            self.data or (),
            width=self.size.width,
            height=self.size.height,
            min_color=min_color.rich_color,
            max_color=max_color.rich_color,
            summary_function=self.summary_function,
        )
