import asyncio
from pathlib import Path

from textual.widgets import Button

from dgx_fan.app import DGXFanApp
from dgx_fan.config import load_config
from dgx_fan.models import GPUStat
from dgx_fan.ui import DashboardHistory


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


def test_history_deduplicates_revisions_preserves_gaps_and_prunes_missing_gpu() -> None:
    history = DashboardHistory()
    gpu = GPUStat("GPU-a", "A100", memory_used_mib=40, utilization_percent=50, temperature_celsius=60)
    history.append("one", 1, (gpu,), 0)
    history.append("one", 1, (gpu,), 0.25)
    assert len(history.points[("one", "GPU-a", "util")]) == 1
    graph = history.graph("one", "GPU-a", "util", 60, 12, 100)
    assert graph.count(" ") == 11
    history.append("one", 2, (), 60)
    assert ("one", "GPU-a") in history.last_seen
    history.prune(121)
    assert ("one", "GPU-a") not in history.last_seen
