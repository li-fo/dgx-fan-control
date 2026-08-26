import asyncio
from pathlib import Path

from textual.widgets import Button

from dgx_fan.app import DGXFanApp
from dgx_fan.config import load_config


def test_ui_has_tabs_and_power_toggle() -> None:
    config = load_config(Path("config.example.toml"))
    app = DGXFanApp(config)
    async def exercise() -> None:
        async with app.run_test() as pilot:
            assert app.query_one("#dashboard")
            assert app.query_one("#fan-control")
            app.query_one("#power-toggle", Button).press()
            await pilot.pause()
            assert not app.controller.power
    asyncio.run(exercise())


def test_control_tick_is_bounded_below_dcgm_poll_interval() -> None:
    config = load_config(Path("config.example.toml"))
    assert config.collection.interval_seconds > DGXFanApp.CONTROL_TICK_SECONDS
    assert DGXFanApp.CONTROL_TICK_SECONDS <= 0.25


def test_poll_exception_is_supervised_and_unmount_cleans_up(monkeypatch) -> None:
    config = load_config(Path("config.example.toml"))
    app = DGXFanApp(config)

    async def failing_collect(now: float):
        raise RuntimeError("poll boom")

    async def exercise() -> None:
        async with app.run_test() as pilot:
            monkeypatch.setattr(app.collector, "collect", failing_collect)
            await app._poll_once(0)
            app.control_tick(1)
            await pilot.pause()
            assert app.latest is not None and app.latest.duty_percent == 100
            assert not app.endpoints[0].healthy
        assert app.hardware is not None
        assert getattr(app.hardware, "released", False)

    asyncio.run(exercise())
