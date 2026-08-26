import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Button

from dgx_fan.app import DGXFanApp
from dgx_fan.config import load_config
from dgx_fan.models import ControlSnapshot, EndpointSnapshot, FanReading, GPUStat
from dgx_fan.ui import DashboardHistory, FanAppUI


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


def test_memory_history_is_normalized_and_zero_is_not_a_gap() -> None:
    history = DashboardHistory()
    gpu = GPUStat("GPU-a", "A100", memory_used_mib=50, memory_total_mib=100, utilization_percent=0, temperature_celsius=0)
    history.append("one", 1, (gpu,), 120)
    assert history.points[("one", "GPU-a", "mem")][0].value == 50
    assert history.graph("one", "GPU-a", "util", 120, 1, 100) == "▁"
    assert history.graph("one", "GPU-a", "util", 250, 1, 100) == " "


def test_area_renderer_has_five_rows_axis_gaps_zero_and_width_scaling() -> None:
    history = DashboardHistory()
    gpu = GPUStat("GPU-a", "A100", memory_used_mib=0, memory_total_mib=100, utilization_percent=1, temperature_celsius=100)
    history.append("one", 1, (gpu,), 120)
    rendered = history.area("one", "GPU-a", "util", 120, 40, 100)
    assert len(rendered) == 6
    assert all(len(line) == 45 for line in rendered)
    assert "█" in rendered[4]
    assert "▁" in history.area("one", "GPU-a", "mem", 120, 40, 100)[4]
    assert "120s" in rendered[-1] and "60s" in rendered[-1] and "now" in rendered[-1]
    assert len(history.area("one", "GPU-a", "util", 120, 80, 100)[0]) > len(rendered[0])


class _DashboardApp(App[None]):
    def compose(self) -> ComposeResult:
        yield FanAppUI("config.toml", lambda: None, 75)


def test_panels_are_ordered_retained_and_deduplicated() -> None:
    app = _DashboardApp()
    gpu = GPUStat("GPU-a", "A100", memory_used_mib=50, memory_total_mib=100, utilization_percent=20, temperature_celsius=40)
    incomplete = GPUStat("GPU-z", "H100", memory_used_mib=50, utilization_percent=0, temperature_celsius=0)
    first = ControlSnapshot(20, "curve", "AUTO ON", 40, 0, (FanReading(1000, "RUNNING"), FanReading(1000, "RUNNING")), (
        EndpointSnapshot("rack/1", "Z endpoint", True, 0, gpus=(incomplete,), sample_revision=1),
        EndpointSnapshot("dgx:one", "A endpoint", True, 0, gpus=(gpu,), sample_revision=1),
    ))
    second = ControlSnapshot(20, "curve", "AUTO ON", 40, 0, (FanReading(1000, "RUNNING"), FanReading(1000, "RUNNING")), (
        EndpointSnapshot("dgx:one", "A endpoint", True, 0, gpus=(), sample_revision=2),
        EndpointSnapshot("rack/1", "Z endpoint", True, 0, gpus=(incomplete,), sample_revision=1),
    ))

    async def exercise() -> None:
        async with app.run_test() as _:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(first, 1)
            await asyncio.sleep(0)
            present = [str(panel.render()) for panel in ui.query(".dgx-panel")]
            assert "50/100 MiB (50%)" in present[1] and "20%" in present[1] and "40.0 C" in present[1]
            ui.update_snapshot(second, 2)
            ui.update_snapshot(second, 2.25)
            await asyncio.sleep(0)
            rendered = [str(panel.render()) for panel in ui.query(".dgx-panel")]
            assert len(rendered) == 2
            assert "A endpoint" in rendered[0] and "Z endpoint" in rendered[1]
            assert "N/A" in rendered[1]
            assert "GPU-a (last seen)" in rendered[0]
            assert len(ui.history.points[("dgx:one", "GPU-a", "util")]) == 1

    asyncio.run(exercise())
