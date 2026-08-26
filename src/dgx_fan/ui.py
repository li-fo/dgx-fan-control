from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Self

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Footer, Header, Static, TabbedContent, TabPane

from .config import DashboardColors
from .models import ControlSnapshot, GPUStat

HISTORY_SECONDS = 120.0
MIN_DASHBOARD_WIDTH = 79
PLOT_HEIGHT = 5


def _safe_display_text(value: object) -> str:
    """Prevent external text from carrying terminal control sequences into Rich."""
    return "".join(
        "\N{REPLACEMENT CHARACTER}" if ord(char) < 32 or 127 <= ord(char) <= 159 else char
        for char in str(value)
    )


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

    def append(
        self, endpoint_id: str, revision: int, gpus: tuple[GPUStat, ...], now: float
    ) -> None:
        if self.revisions.get(endpoint_id) == revision:
            self.prune(now)
            return
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
        rows: list[str] = []
        label_rows = {0, plot_height // 2, plot_height - 1}
        for row in range(plot_height):
            threshold = (plot_height - 1 - row) / max(1, plot_height - 1) * maximum
            line = "".join(
                " "
                if value is None
                else "▁"
                if value == 0 and row == plot_height - 1
                else "█"
                if value > 0 and (row == plot_height - 1 or value >= threshold)
                else " "
                for value in values
            )
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


class PowerButton(Button):
    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__("Toggle fan power", id="power-toggle")
        self.callback = callback

    def press(self) -> Self:
        self.callback()
        return super().press()


class FanAppUI(Static):
    def __init__(
        self,
        config_path: str,
        toggle: Callable[[], None],
        emergency_temperature: float,
        collection_interval_seconds: float,
        dashboard_colors: DashboardColors | None = None,
    ) -> None:
        super().__init__()
        self.config_path, self.toggle, self.emergency_temperature = (
            config_path,
            toggle,
            emergency_temperature,
        )
        self.dashboard_colors = dashboard_colors or DashboardColors()
        self.snapshot: ControlSnapshot | None = None
        self.history = DashboardHistory(collection_interval_seconds)
        self.panels: dict[str, Static] = {}
        self.last_render_time: float | None = None
        self.last_signature: tuple[tuple[str, int], ...] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="dashboard"):
            with TabPane("DGX Dashboard", id="dashboard"):
                yield Static("Waiting for DCGM metrics…", id="error-banner", markup=False)
                yield Static(id="dashboard-warning")
                yield VerticalScroll(id="dashboard-scroll")
            with TabPane("Fan Control", id="fan-control"), Vertical():
                yield Static(id="fan-status")
                yield PowerButton(self.toggle)
                yield Static(
                    f"Edit {_safe_display_text(self.config_path)} and restart to change settings.",
                    id="config-hint",
                    markup=False,
                )
        yield Footer()

    def update_snapshot(self, snapshot: ControlSnapshot, now: float) -> None:
        self.snapshot = snapshot
        self.last_render_time = now
        for endpoint in snapshot.endpoint_snapshots:
            self.history.append(endpoint.endpoint_id, endpoint.sample_revision, endpoint.gpus, now)
        errors = [
            f"{_safe_display_text(e.name)}: {_safe_display_text(e.error or 'stale')} "
            f"(sample age: {'N/A' if e.age_seconds is None else f'{e.age_seconds:.1f}s'})"
            for e in snapshot.endpoint_snapshots
            if not e.healthy
        ]
        self.query_one("#error-banner", Static).update(
            " | ".join(errors) if errors else "All configured DGX endpoints are healthy."
        )
        signature = tuple(
            (endpoint.endpoint_id, endpoint.sample_revision)
            for endpoint in snapshot.endpoint_snapshots
        )
        if signature != self.last_signature:
            self._render_dashboard(snapshot, now)
            self.last_signature = signature
        fan_text = ", ".join(
            f"Fan {i + 1}: {fan.state} {fan.rpm or 0:.0f} RPM"
            for i, fan in enumerate(snapshot.fans)
        )
        maximum = (
            "N/A"
            if snapshot.max_temperature_celsius is None
            else f"{snapshot.max_temperature_celsius:.1f} C"
        )
        self.query_one("#fan-status", Static).update(
            f"{snapshot.state} — {snapshot.duty_percent}% PWM ({snapshot.reason})\nMax GPU temp: {maximum}; stage: {snapshot.active_stage}\n{fan_text}"
        )

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
        gpu_count = sum(len(keys) for keys in keys_by_endpoint.values())
        content_lines = len(snapshot.endpoint_snapshots) + gpu_count * (6 + plot_height * 2)
        # Textual reserves two cells for a visible vertical scrollbar. Reserve
        # the same width before composing when the minimum content genuinely
        # overflows, so chart rows never wrap underneath it.
        available = max(1, scroll.size.width - (2 if content_lines > scroll.size.height else 0))
        previous: Static | None = None
        for endpoint in snapshot.endpoint_snapshots:
            # Health changes on the fast control tick. Keep them in the banner so
            # a changed health state doesn't imply that cached charts were sampled
            # again or need a costly panel redraw.
            rendered = Text(_safe_display_text(endpoint.name))
            keys = keys_by_endpoint[endpoint.endpoint_id]
            for key in keys:
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
                memory_lines, memory_color = self._chart_box(
                    endpoint.endpoint_id, key, "MEM", mem, now, left_width, 100, plot_height
                )
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
                utilization_lines, utilization_color = self._chart_box(
                    endpoint.endpoint_id, key, "UTIL", util, now, available, 100, plot_height
                )
                rendered.append("\n")
                for index, (memory_line, temperature_line) in enumerate(
                    zip(memory_lines, temperature_lines, strict=True)
                ):
                    self._append_colored(rendered, memory_line, memory_color)
                    rendered.append(" ")
                    self._append_colored(rendered, temperature_line, temperature_color)
                    if index < len(memory_lines) - 1:
                        rendered.append("\n")
                rendered.append("\n")
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
            metric.lower(),
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
        if self.snapshot is not None and self.last_render_time is not None:
            self._render_dashboard(self.snapshot, self.last_render_time)
