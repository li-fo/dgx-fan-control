from __future__ import annotations

from collections.abc import Callable
from typing import Self

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, DataTable, Footer, Header, Static, TabbedContent, TabPane

from .models import ControlSnapshot


class PowerButton(Button):
    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__("Toggle fan power", id="power-toggle")
        self.callback = callback

    def press(self) -> Self:
        self.callback()
        return super().press()


class FanAppUI(Static):
    def __init__(self, config_path: str, toggle: Callable[[], None]) -> None:
        super().__init__()
        self.config_path = config_path
        self.toggle = toggle
        self.snapshot: ControlSnapshot | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="dashboard"):
            with TabPane("DGX Dashboard", id="dashboard"):
                yield Static("Waiting for DCGM metrics…", id="error-banner")
                yield DataTable(id="gpu-table")
            with TabPane("Fan Control", id="fan-control"), Vertical():
                yield Static(id="fan-status")
                yield PowerButton(self.toggle)
                yield Static(f"Edit {self.config_path} and restart to change settings.", id="config-hint")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#gpu-table", DataTable)
        table.add_columns("DGX", "GPU", "Model", "GPU MEM", "GPU UTIL", "GPU Temp")

    def update_snapshot(self, snapshot: ControlSnapshot) -> None:
        self.snapshot = snapshot
        banner = self.query_one("#error-banner", Static)
        errors = [f"{endpoint.name}: {endpoint.error or 'stale'}" for endpoint in snapshot.endpoint_snapshots if not endpoint.healthy]
        banner.update(" | ".join(errors) if errors else "All configured DGX endpoints are healthy.")
        table = self.query_one("#gpu-table", DataTable)
        table.clear()
        for endpoint in snapshot.endpoint_snapshots:
            for gpu in endpoint.gpus:
                memory = "N/A" if gpu.memory_used_mib is None or gpu.memory_total_mib is None else f"{gpu.memory_used_mib:.0f}/{gpu.memory_total_mib:.0f} MiB"
                util = "N/A" if gpu.utilization_percent is None else f"{gpu.utilization_percent:.0f}%"
                temp = "N/A" if gpu.temperature_celsius is None else f"{gpu.temperature_celsius:.1f} C"
                table.add_row(endpoint.name, gpu.key, gpu.name, memory, util, temp)
        fan_text = ", ".join(f"Fan {index + 1}: {fan.state} {fan.rpm or 0:.0f} RPM" for index, fan in enumerate(snapshot.fans))
        maximum = "N/A" if snapshot.max_temperature_celsius is None else f"{snapshot.max_temperature_celsius:.1f} C"
        self.query_one("#fan-status", Static).update(f"{snapshot.state} — {snapshot.duty_percent}% PWM ({snapshot.reason})\nMax GPU temp: {maximum}; stage: {snapshot.active_stage}\n{fan_text}")
