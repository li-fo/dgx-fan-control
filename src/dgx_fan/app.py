from __future__ import annotations

import argparse
import time

from textual.app import App, ComposeResult

from .config import AppConfig, ConfigError, load_config, resolve_config_path
from .controller import FanController
from .dcgm import DCGMCollector
from .hardware import FanHardware, create_hardware
from .models import ControlSnapshot, EndpointSnapshot
from .ui import FanAppUI


class DGXFanApp(App[None]):
    TITLE = "DGX Fan Controller"

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.hardware: FanHardware | None = None
        self.controller = FanController(config.control, config.hardware)
        self.collector = DCGMCollector(config.endpoints, config.collection.timeout_seconds, config.collection.stale_after_seconds)
        self.endpoints: tuple[EndpointSnapshot, ...] = ()
        self.latest: ControlSnapshot | None = None

    def compose(self) -> ComposeResult:
        yield FanAppUI(str(self.config.path), self.toggle_power)

    async def on_mount(self) -> None:
        self.hardware = create_hardware(self.config.hardware)
        self.hardware.set_duty(100)  # Safe-full before any external read.
        self.set_interval(self.config.collection.interval_seconds, self.refresh_state)
        await self.refresh_state()

    async def refresh_state(self) -> None:
        now = time.monotonic()
        self.endpoints = await self.collector.collect(now)
        if self.hardware is None:
            return
        fans = self.hardware.readings(now)
        self.latest = self.controller.update(self.endpoints, fans, now)
        self.hardware.set_duty(self.latest.duty_percent)
        self.query_one(FanAppUI).update_snapshot(self.latest)

    def toggle_power(self) -> None:
        self.controller.set_power(not self.controller.power)

    def on_unmount(self) -> None:
        if self.hardware is not None:
            self.hardware.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DGX DCGM fan controller TUI")
    parser.add_argument("--config", help="TOML config path; overrides DGX_FAN_CONFIG and ./config.toml")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(resolve_config_path(args.config))
    except ConfigError as error:
        raise SystemExit(f"dgx-fan: configuration error: {error}") from error
    app = DGXFanApp(config)
    try:
        app.run()
    finally:
        if app.hardware is not None:
            app.hardware.release()
