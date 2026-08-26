import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button

from dgx_fan.app import DGXFanApp
from dgx_fan.config import load_config
from dgx_fan.models import ControlSnapshot, EndpointSnapshot, FanReading, GPUStat
from dgx_fan.ui import DashboardHistory, FanAppUI, HistoryPoint


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
    graph = history.area("one", "GPU-a", "util", 60, 12, 100)[4][5:]
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
    assert history.area("one", "GPU-a", "util", 120, 1, 100)[4][5:] == "▁"
    assert history.area("one", "GPU-a", "util", 250, 1, 100)[4][5:] == " "


def test_area_renderer_locks_geometry_axis_gaps_and_bin_reducers() -> None:
    """Keep the renderer's nvitop-like plot geometry independent of Textual layout."""
    now = 120.0

    def point_for_bin(width: int, bin_index: int, value: float) -> HistoryPoint:
        return HistoryPoint((bin_index + 0.5) / width * 120, value)

    for width, expected_positions in ((72, (5, 38, 56, 73)), (113, (5, 58, 86, 113))):
        history = DashboardHistory()
        # Bin 1 is intentionally absent; bins 2..5 prove zero and area height.
        history.points[("one", "GPU-a", "util")] = [
            point_for_bin(width, 2, 0),
            point_for_bin(width, 3, 1),
            point_for_bin(width, 4, 50),
            point_for_bin(width, 5, 100),
        ]
        rendered = history.area("one", "GPU-a", "util", now, width, 100)

        assert len(rendered) == 6
        assert all(len(line) == width + 5 for line in rendered)
        assert [line[:5] for line in rendered[:5]] == [" 100 ", "     ", "  50 ", "     ", "   0 "]
        assert all(line[5 + 1] == " " for line in rendered[:5])
        assert [line[5 + 2] for line in rendered[:5]] == [" ", " ", " ", " ", "▁"]
        filled = [sum(line[5 + column] != " " for line in rendered[:5]) for column in (3, 4, 5)]
        assert filled[0] < filled[1] < filled[2]

        axis = rendered[-1]
        positions = tuple(axis.index(label) for label in ("120s", "60s", "30s", "now"))
        assert positions == expected_positions
        assert positions == tuple(sorted(positions))
        assert positions[0] == 5 and axis.rstrip().endswith("now")
        assert all(positions[index] + len(label) <= positions[index + 1] for index, label in enumerate(("120s", "60s", "30s")))

    reducers = DashboardHistory()
    width = 72
    reducers.points[("one", "GPU-a", "mem")] = [point_for_bin(width, 10, 10), point_for_bin(width, 10, 80)]
    reducers.points[("one", "GPU-a", "util")] = [point_for_bin(width, 10, 10), point_for_bin(width, 10, 90)]
    reducers.points[("one", "GPU-a", "temp")] = [point_for_bin(width, 10, 10), point_for_bin(width, 10, 90)]
    filled_rows = {
        metric: sum(line[5 + 10] != " " for line in reducers.area("one", "GPU-a", metric, now, width, 100)[:5])
        for metric in ("mem", "util", "temp")
    }
    assert filled_rows == {"mem": 4, "util": 3, "temp": 4}

    temp = reducers.area("one", "GPU-a", "temp", now, width, 75)
    assert [line[:5] for line in temp[:5]] == ["  75 ", "     ", "  38 ", "     ", "   0 "]


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


def test_dashboard_signature_gates_charts_but_not_fast_status_updates(monkeypatch) -> None:
    """Fast health/fan changes must not make cached chart panels look sampled."""
    app = _DashboardApp()
    gpu = GPUStat("GPU-a", "A100", memory_used_mib=50, memory_total_mib=100, utilization_percent=20, temperature_celsius=40)

    def snapshot(
        endpoints: tuple[EndpointSnapshot, ...], *, state: str = "AUTO ON", healthy: bool = True
    ) -> ControlSnapshot:
        adjusted = tuple(
            EndpointSnapshot(
                endpoint.endpoint_id,
                endpoint.name,
                healthy if endpoint.endpoint_id == "a" else endpoint.healthy,
                endpoint.age_seconds,
                error="poll failed" if endpoint.endpoint_id == "a" and not healthy else endpoint.error,
                gpus=endpoint.gpus,
                sample_revision=endpoint.sample_revision,
            )
            for endpoint in endpoints
        )
        return ControlSnapshot(20, "curve", state, 40, 0, (FanReading(1000, "RUNNING"), FanReading(1000, "RUNNING")), adjusted)

    a1 = EndpointSnapshot("a", "A", True, 0, gpus=(gpu,), sample_revision=1)
    b1 = EndpointSnapshot("b", "B", True, 0, gpus=(gpu,), sample_revision=1)
    a2 = EndpointSnapshot("a", "A", True, 0, gpus=(gpu,), sample_revision=2)
    b2 = EndpointSnapshot("b", "B", True, 0, gpus=(gpu,), sample_revision=2)

    async def exercise() -> None:
        async with app.run_test(size=(100, 24)) as pilot:
            ui = app.query_one(FanAppUI)
            renders: list[tuple[tuple[str, int], ...]] = []
            original = ui._render_dashboard

            def traced(rendered: ControlSnapshot, now: float) -> None:
                renders.append(tuple((endpoint.endpoint_id, endpoint.sample_revision) for endpoint in rendered.endpoint_snapshots))
                original(rendered, now)

            monkeypatch.setattr(ui, "_render_dashboard", traced)
            ui.update_snapshot(snapshot((a1, b1)), 1)
            assert renders == [(('a', 1), ('b', 1))]
            history_count = len(ui.history.points[("a", "GPU-a", "util")])

            ui.update_snapshot(snapshot((a1, b1), state="AUTO OFF", healthy=False), 1.25)
            assert len(renders) == 1
            assert len(ui.history.points[("a", "GPU-a", "util")]) == history_count
            assert "poll failed" in str(ui.query_one("#error-banner").render())
            assert "AUTO OFF" in str(ui.query_one("#fan-status").render())
            assert "HEALTHY" not in str(next(iter(ui.query(".dgx-panel"))).render())

            ui.update_snapshot(snapshot((a2, b1)), 2)
            ui.update_snapshot(snapshot((a2, b2)), 3)
            ui.update_snapshot(snapshot((b2, a2)), 4)
            ui.update_snapshot(snapshot((a2,)), 5)
            assert renders == [(('a', 1), ('b', 1)), (('a', 2), ('b', 1)), (('a', 2), ('b', 2)), (('b', 2), ('a', 2)), (('a', 2),)]

            before_resize_count = len(ui.history.points[("a", "GPU-a", "util")])
            before_resize_renders = len(renders)
            await pilot.resize_terminal(120, 24)
            await pilot.pause()
            assert len(renders) == before_resize_renders + 1
            ui.update_snapshot(snapshot((a2,)), 5.25)
            assert len(renders) == before_resize_renders + 1
            assert len(ui.history.points[("a", "GPU-a", "util")]) == before_resize_count

    asyncio.run(exercise())


def test_mounted_responsive_width_reuses_history() -> None:
    app = _DashboardApp()
    gpu = GPUStat("GPU-a", "A100", memory_used_mib=50, memory_total_mib=100, utilization_percent=20, temperature_celsius=40)
    snapshot = ControlSnapshot(20, "curve", "AUTO ON", 40, 0, (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")), (EndpointSnapshot("one", "One", True, 0, gpus=(gpu,), sample_revision=1),))
    async def exercise() -> None:
        async with app.run_test(size=(78, 24)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 1)
            scroll = ui.query_one("#dashboard-scroll", VerticalScroll)
            assert "78" in str(ui.query_one("#dashboard-warning").render()) and not scroll.display
            count = len(ui.history.points[("one", "GPU-a", "util")])
            await pilot.resize_terminal(79, 24); await pilot.pause()
            assert scroll.display and len(ui.query(".dgx-panel")) == 1
            narrow_lines = str(next(iter(ui.query(".dgx-panel"))).render()).splitlines()
            def assert_boxes(lines: list[str], expected_width: int) -> int:
                widths: list[int] = []
                for metric, value in (("MEM", "50/100 MiB (50%)"), ("UTIL", "20%"), ("TEMP", "40.0 C")):
                    top_index = next(index for index, line in enumerate(lines) if line.startswith(f"┌ {metric} "))
                    box = lines[top_index:top_index + 8]
                    assert len(box) == 8
                    assert box[0].startswith(f"┌ {metric} {value}") and box[0].endswith("┐")
                    assert all(row.startswith("│") and row.endswith("│") for row in box[1:7])
                    assert box[-1].startswith("└") and box[-1].endswith("┘")
                    assert len({len(row) for row in box}) == 1
                    assert all(len(row) <= expected_width for row in box)
                    widths.append(len(box[1]) - 7)
                assert len(set(widths)) == 1
                return widths[0]

            narrow = assert_boxes(narrow_lines, scroll.size.width)
            assert narrow > 36
            await pilot.resize_terminal(120, 24); await pilot.pause()
            wide_lines = str(next(iter(ui.query(".dgx-panel"))).render()).splitlines()
            wide = assert_boxes(wide_lines, scroll.size.width)
            assert wide > narrow and count == len(ui.history.points[("one", "GPU-a", "util")])
            await pilot.resize_terminal(78, 24); await pilot.resize_terminal(79, 24); await pilot.pause()
            assert scroll.display and len(ui.query(".dgx-panel")) == 1
            assert count == len(ui.history.points[("one", "GPU-a", "util")])
    asyncio.run(exercise())


def test_dashboard_scrolls_with_keyboard() -> None:
    app = _DashboardApp()
    gpus = tuple(GPUStat(f"GPU-{i}", "A100", memory_used_mib=50, memory_total_mib=100, utilization_percent=20, temperature_celsius=40) for i in range(5))
    snapshot = ControlSnapshot(20, "curve", "AUTO ON", 40, 0, (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")), (EndpointSnapshot("z", "Z", True, 0, gpus=gpus, sample_revision=1), EndpointSnapshot("a", "A", True, 0, gpus=gpus, sample_revision=1)))
    async def exercise() -> None:
        async with app.run_test(size=(100, 12)) as pilot:
            ui = app.query_one(FanAppUI); ui.update_snapshot(snapshot, 1); await pilot.pause()
            scroll = ui.query_one("#dashboard-scroll", VerticalScroll); scroll.focus()
            assert scroll.can_focus and scroll.max_scroll_y > 0
            before = scroll.scroll_y; await pilot.press("down"); await pilot.pause()
            assert scroll.scroll_y > before
    asyncio.run(exercise())
