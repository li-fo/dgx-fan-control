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


@dataclass(frozen=True)
class HistoryPoint:
    at: float
    value: float


class DashboardHistory:
    """Revision-deduplicated, bounded in-memory metric history."""

    def __init__(self) -> None:
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

    def graph(self, endpoint_id: str, gpu: str, metric: str, now: float, width: int, maximum: float) -> str:
        width = max(1, width)
        bins: list[list[float]] = [[] for _ in range(width)]
        cutoff = now - HISTORY_SECONDS
        for point in self.points.get((endpoint_id, gpu, metric), []):
            if point.at >= cutoff:
                index = min(width - 1, int((point.at - cutoff) / HISTORY_SECONDS * width))
                bins[index].append(point.value)
        glyphs = "▁▂▃▄▅▆▇█"
        rendered: list[str] = []
        for values in bins:
            if not values:
                rendered.append(" ")
                continue
            value = max(values) if metric == "temp" else sum(values) / len(values) if metric == "util" else values[-1]
            rendered.append(glyphs[min(7, max(0, round(value / maximum * 7)))])
        return "".join(rendered)


class PowerButton(Button):
    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__("Toggle fan power", id="power-toggle")
        self.callback = callback

    def press(self) -> Self:
        self.callback()
        return super().press()


class FanAppUI(Static):
    def __init__(self, config_path: str, toggle: Callable[[], None], emergency_temperature: float) -> None:
        super().__init__()
        self.config_path, self.toggle, self.emergency_temperature = config_path, toggle, emergency_temperature
        self.snapshot: ControlSnapshot | None = None
        self.history = DashboardHistory()
        self.panels: dict[str, Static] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="dashboard"):
            with TabPane("DGX Dashboard", id="dashboard"):
                yield Static("Waiting for DCGM metrics…", id="error-banner")
                yield VerticalScroll(id="dashboard-scroll")
            with TabPane("Fan Control", id="fan-control"), Vertical():
                yield Static(id="fan-status")
                yield PowerButton(self.toggle)
                yield Static(f"Edit {self.config_path} and restart to change settings.", id="config-hint")
        yield Footer()

    def update_snapshot(self, snapshot: ControlSnapshot, now: float) -> None:
        self.snapshot = snapshot
        for endpoint in snapshot.endpoint_snapshots:
            self.history.append(endpoint.endpoint_id, endpoint.sample_revision, endpoint.gpus, now)
        errors = [f"{e.name}: {e.error or 'stale'} (sample age: {'N/A' if e.age_seconds is None else f'{e.age_seconds:.1f}s'})" for e in snapshot.endpoint_snapshots if not e.healthy]
        self.query_one("#error-banner", Static).update(" | ".join(errors) if errors else "All configured DGX endpoints are healthy.")
        self._render_dashboard(snapshot, now)
        fan_text = ", ".join(f"Fan {i + 1}: {fan.state} {fan.rpm or 0:.0f} RPM" for i, fan in enumerate(snapshot.fans))
        maximum = "N/A" if snapshot.max_temperature_celsius is None else f"{snapshot.max_temperature_celsius:.1f} C"
        self.query_one("#fan-status", Static).update(f"{snapshot.state} — {snapshot.duty_percent}% PWM ({snapshot.reason})\nMax GPU temp: {maximum}; stage: {snapshot.active_stage}\n{fan_text}")

    def _render_dashboard(self, snapshot: ControlSnapshot, now: float) -> None:
        scroll = self.query_one("#dashboard-scroll", VerticalScroll)
        active_ids = {endpoint.endpoint_id for endpoint in snapshot.endpoint_snapshots}
        for endpoint_id, stale_panel in list(self.panels.items()):
            if endpoint_id not in active_ids:
                stale_panel.remove()
                del self.panels[endpoint_id]
        current = {(endpoint.endpoint_id, gpu.key): gpu for endpoint in snapshot.endpoint_snapshots for gpu in endpoint.gpus}
        previous: Static | None = None
        for endpoint in snapshot.endpoint_snapshots:
            lines = [f"[b]{endpoint.name}[/b]  {'HEALTHY' if endpoint.healthy else 'UNHEALTHY'}"]
            keys = sorted(key for source, key in self.history.last_seen if source == endpoint.endpoint_id)
            for key in keys:
                gpu = current.get((endpoint.endpoint_id, key))
                label = f"{gpu.key} {gpu.name}" if gpu else f"{key} (last seen)"
                mem = "N/A"
                if gpu is not None and gpu.memory_used_mib is not None and gpu.memory_total_mib is not None and gpu.memory_total_mib > 0:
                    mem = f"{gpu.memory_used_mib:.0f}/{gpu.memory_total_mib:.0f} MiB ({gpu.memory_used_mib / gpu.memory_total_mib * 100:.0f}%)"
                util = "N/A" if gpu is None or gpu.utilization_percent is None else f"{gpu.utilization_percent:.0f}%"
                temp = "N/A" if gpu is None or gpu.temperature_celsius is None else f"{gpu.temperature_celsius:.1f} C"
                lines.extend((label, f"MEM  {mem:>9} {self.history.graph(endpoint.endpoint_id, key, 'mem', now, 36, 100)}", f"UTIL {util:>9} {self.history.graph(endpoint.endpoint_id, key, 'util', now, 36, 100)}", f"TEMP {temp:>9} {self.history.graph(endpoint.endpoint_id, key, 'temp', now, 36, max(100, self.emergency_temperature))}"))
            panel = self.panels.get(endpoint.endpoint_id)
            if panel is None:
                panel = Static(classes="dgx-panel")
                self.panels[endpoint.endpoint_id] = panel
                scroll.mount(panel)
            assert panel is not None
            panel.update("\n".join(lines))
            if previous is None:
                scroll.move_child(panel, before=0)
            else:
                scroll.move_child(panel, after=previous)
            previous = panel
