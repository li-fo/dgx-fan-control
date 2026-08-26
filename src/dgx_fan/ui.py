from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Self

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Footer, Header, Static, TabbedContent, TabPane

from .models import ControlSnapshot, GPUStat

HISTORY_SECONDS = 120.0
MIN_DASHBOARD_WIDTH = 79
PLOT_HEIGHT = 5


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

    def append(self, endpoint_id: str, revision: int, gpus: tuple[GPUStat, ...], now: float) -> None:
        if self.revisions.get(endpoint_id) == revision:
            self.prune(now)
            return
        self.revisions[endpoint_id] = revision
        for gpu in gpus:
            identity = (endpoint_id, gpu.key)
            self.last_seen[identity] = now
            memory_percent = None
            if gpu.memory_used_mib is not None and gpu.memory_total_mib is not None and gpu.memory_total_mib > 0:
                memory_percent = gpu.memory_used_mib / gpu.memory_total_mib * 100
            for metric, value in (("mem", memory_percent), ("util", gpu.utilization_percent), ("temp", gpu.temperature_celsius)):
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

    def area(self, endpoint_id: str, gpu: str, metric: str, now: float, width: int, maximum: float) -> list[str]:
        bins: list[list[float]] = [[] for _ in range(width)]
        latest_at: list[float | None] = [None for _ in range(width)]
        cutoff = now - HISTORY_SECONDS
        for point in self.points.get((endpoint_id, gpu, metric), []):
            if point.at >= cutoff:
                index = min(width - 1, int((point.at - cutoff) / HISTORY_SECONDS * width))
                bins[index].append(point.value)
                latest = latest_at[index]
                latest_at[index] = point.at if latest is None else max(latest, point.at)
        values = [None if not group else (max(group) if metric == "temp" else sum(group) / len(group) if metric == "util" else group[-1]) for group in bins]
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
            bin_start = cutoff + index * bin_seconds
            if previous_value is not None and previous_at is not None and bin_start - previous_at <= hold_seconds:
                values[index] = previous_value
        rows: list[str] = []
        for row in range(PLOT_HEIGHT):
            threshold = (PLOT_HEIGHT - 1 - row) / (PLOT_HEIGHT - 1) * maximum
            line = "".join(" " if value is None else "▁" if value == 0 and row == PLOT_HEIGHT - 1 else "█" if value > 0 and (row == PLOT_HEIGHT - 1 or value >= threshold) else " " for value in values)
            label = f"{threshold:>4.0f} " if row in {0, 2, 4} else "     "
            rows.append(label + line)
        axis = "     120s" + " " * max(0, width // 2 - 7) + "60s" + " " * max(0, width // 4 - 3) + "30s" + " " * max(0, width // 4 - 4) + "now"
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
    def __init__(self, config_path: str, toggle: Callable[[], None], emergency_temperature: float, collection_interval_seconds: float) -> None:
        super().__init__()
        self.config_path, self.toggle, self.emergency_temperature = config_path, toggle, emergency_temperature
        self.snapshot: ControlSnapshot | None = None
        self.history = DashboardHistory(collection_interval_seconds)
        self.panels: dict[str, Static] = {}
        self.last_render_time: float | None = None
        self.last_signature: tuple[tuple[str, int], ...] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="dashboard"):
            with TabPane("DGX Dashboard", id="dashboard"):
                yield Static("Waiting for DCGM metrics…", id="error-banner")
                yield Static(id="dashboard-warning")
                yield VerticalScroll(id="dashboard-scroll")
            with TabPane("Fan Control", id="fan-control"), Vertical():
                yield Static(id="fan-status")
                yield PowerButton(self.toggle)
                yield Static(f"Edit {self.config_path} and restart to change settings.", id="config-hint")
        yield Footer()

    def update_snapshot(self, snapshot: ControlSnapshot, now: float) -> None:
        self.snapshot = snapshot
        self.last_render_time = now
        for endpoint in snapshot.endpoint_snapshots:
            self.history.append(endpoint.endpoint_id, endpoint.sample_revision, endpoint.gpus, now)
        errors = [f"{e.name}: {e.error or 'stale'} (sample age: {'N/A' if e.age_seconds is None else f'{e.age_seconds:.1f}s'})" for e in snapshot.endpoint_snapshots if not e.healthy]
        self.query_one("#error-banner", Static).update(" | ".join(errors) if errors else "All configured DGX endpoints are healthy.")
        signature = tuple((endpoint.endpoint_id, endpoint.sample_revision) for endpoint in snapshot.endpoint_snapshots)
        if signature != self.last_signature:
            self._render_dashboard(snapshot, now)
            self.last_signature = signature
        fan_text = ", ".join(f"Fan {i + 1}: {fan.state} {fan.rpm or 0:.0f} RPM" for i, fan in enumerate(snapshot.fans))
        maximum = "N/A" if snapshot.max_temperature_celsius is None else f"{snapshot.max_temperature_celsius:.1f} C"
        self.query_one("#fan-status", Static).update(f"{snapshot.state} — {snapshot.duty_percent}% PWM ({snapshot.reason})\nMax GPU temp: {maximum}; stage: {snapshot.active_stage}\n{fan_text}")

    def _render_dashboard(self, snapshot: ControlSnapshot, now: float) -> None:
        scroll = self.query_one("#dashboard-scroll", VerticalScroll)
        terminal_width = self.screen.size.width
        available = scroll.size.width
        warning = self.query_one("#dashboard-warning", Static)
        if terminal_width < MIN_DASHBOARD_WIDTH:
            warning.update(f"Dashboard width {terminal_width}; at least {MIN_DASHBOARD_WIDTH} columns required for charts.")
            scroll.display = False
            return
        warning.update("")
        scroll.display = True
        active_ids = {endpoint.endpoint_id for endpoint in snapshot.endpoint_snapshots}
        for endpoint_id, stale_panel in list(self.panels.items()):
            if endpoint_id not in active_ids:
                stale_panel.remove()
                del self.panels[endpoint_id]
        current = {(endpoint.endpoint_id, gpu.key): gpu for endpoint in snapshot.endpoint_snapshots for gpu in endpoint.gpus}
        previous: Static | None = None
        for endpoint in snapshot.endpoint_snapshots:
            # Health changes on the fast control tick. Keep them in the banner so
            # a changed health state doesn't imply that cached charts were sampled
            # again or need a costly panel redraw.
            lines = [endpoint.name]
            keys = sorted(key for source, key in self.history.last_seen if source == endpoint.endpoint_id)
            for key in keys:
                gpu = current.get((endpoint.endpoint_id, key))
                mem = "N/A"
                if gpu is not None and gpu.memory_used_mib is not None and gpu.memory_total_mib is not None and gpu.memory_total_mib > 0:
                    mem = f"{gpu.memory_used_mib:.0f}/{gpu.memory_total_mib:.0f} MiB ({gpu.memory_used_mib / gpu.memory_total_mib * 100:.0f}%)"
                util = "N/A" if gpu is None or gpu.utilization_percent is None else f"{gpu.utilization_percent:.0f}%"
                temp = "N/A" if gpu is None or gpu.temperature_celsius is None else f"{gpu.temperature_celsius:.1f} C"
                plot_width = max(1, available - 7)
                for metric, value, maximum in (("MEM", mem, 100), ("UTIL", util, 100), ("TEMP", temp, max(100, self.emergency_temperature))):
                    chart = self.history.area(endpoint.endpoint_id, key, metric.lower(), now, max(1, plot_width - 2), maximum)
                    inner = max(len(row) for row in chart)
                    lines.append(f"┌ {metric} {value}"[:inner + 1].ljust(inner + 1, "─") + "┐")
                    lines.extend(f"│{row.ljust(inner)}│" for row in chart)
                    lines.append("└" + "─" * inner + "┘")
            panel = self.panels.get(endpoint.endpoint_id)
            if panel is None:
                # Endpoint/GPU names originate outside the UI. Rendering the panel
                # as plain text keeps them from becoming Rich markup.
                panel = Static(classes="dgx-panel", markup=False)
                self.panels[endpoint.endpoint_id] = panel
                scroll.mount(panel)
            assert panel is not None
            panel.update("\n".join(lines))
            if previous is None:
                scroll.move_child(panel, before=0)
            else:
                scroll.move_child(panel, after=previous)
            previous = panel

    def on_resize(self) -> None:
        if self.snapshot is not None and self.last_render_time is not None:
            self._render_dashboard(self.snapshot, self.last_render_time)
