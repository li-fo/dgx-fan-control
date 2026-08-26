import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button

from dgx_fan.app import DGXFanApp
from dgx_fan.config import DashboardColors, load_config
from dgx_fan.models import ControlSnapshot, EndpointSnapshot, FanReading, GPUStat
from dgx_fan.ui import DashboardHistory, FanAppUI, HistoryPoint


def test_ui_has_tabs_and_power_toggle() -> None:
    config = load_config(Path("config.example.toml"))
    app = DGXFanApp(config)

    async def exercise() -> None:
        async with app.run_test() as pilot:
            assert app.query_one("#dashboard")
            assert app.query_one("#fan-control")
            assert (
                app.query_one(FanAppUI).history.collection_interval_seconds
                == config.collection.interval_seconds
            )
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
    history = DashboardHistory(2)
    gpu = GPUStat(
        "GPU-a", "A100", memory_used_mib=40, utilization_percent=50, temperature_celsius=60
    )
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
    history = DashboardHistory(2)
    gpu = GPUStat(
        "GPU-a",
        "A100",
        memory_used_mib=50,
        memory_total_mib=100,
        utilization_percent=0,
        temperature_celsius=0,
    )
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
        history = DashboardHistory(2)
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
        assert all(
            positions[index] + len(label) <= positions[index + 1]
            for index, label in enumerate(("120s", "60s", "30s"))
        )

    reducers = DashboardHistory(2)
    width = 72
    reducers.points[("one", "GPU-a", "mem")] = [
        point_for_bin(width, 10, 10),
        point_for_bin(width, 10, 80),
    ]
    reducers.points[("one", "GPU-a", "util")] = [
        point_for_bin(width, 10, 10),
        point_for_bin(width, 10, 90),
    ]
    reducers.points[("one", "GPU-a", "temp")] = [
        point_for_bin(width, 10, 10),
        point_for_bin(width, 10, 90),
    ]
    filled_rows = {
        metric: sum(
            line[5 + 10] != " "
            for line in reducers.area("one", "GPU-a", metric, now, width, 100)[:5]
        )
        for metric in ("mem", "util", "temp")
    }
    assert filled_rows == {"mem": 4, "util": 3, "temp": 4}

    temp = reducers.area("one", "GPU-a", "temp", now, width, 75)
    assert [line[:5] for line in temp[:5]] == ["  75 ", "     ", "  38 ", "     ", "   0 "]

    for height in range(1, 6):
        compact = reducers.area("one", "GPU-a", "temp", now, width, 75, height)
        assert len(compact) == height + 1
        assert compact[-1].rstrip().endswith("now")
        assert compact[0].startswith("   0 " if height == 1 else "  75 ")
        assert compact[-2].startswith("   0 ")


def test_compact_plot_heights_preserve_zero_missing_and_low_positive_baselines() -> None:
    """Every responsive height needs a visible bottom-row baseline."""
    now = 120.0
    width = 12
    zero = DashboardHistory(2)
    low_positive = DashboardHistory(2)
    zero.points[("one", "GPU-a", "util")] = [HistoryPoint(now, 0)]
    low_positive.points[("one", "GPU-a", "util")] = [HistoryPoint(now, 0.1)]

    for height in range(1, 6):
        zero_rows = zero.area("one", "GPU-a", "util", now, width, 100, height)
        missing_rows = DashboardHistory(2).area("one", "GPU-a", "util", now, width, 100, height)
        positive_rows = low_positive.area("one", "GPU-a", "util", now, width, 100, height)
        zero_glyphs = [row[5:] for row in zero_rows[:-1]]
        positive_glyphs = [row[5:] for row in positive_rows[:-1]]

        assert sum(line.count("▁") for line in zero_glyphs) == 1
        assert zero_glyphs[height - 1].endswith("▁")
        assert all(line == " " * width for line in (row[5:] for row in missing_rows[:-1]))
        assert sum(line.count("█") for line in positive_glyphs) == 1
        assert positive_glyphs[height - 1].endswith("█")


class _DashboardApp(App[None]):
    def compose(self) -> ComposeResult:
        yield FanAppUI("config.toml", lambda: None, 75, 2)


def _single_gpu_snapshot(name: str = "One") -> ControlSnapshot:
    gpu = GPUStat(
        "GPU-a",
        "A100",
        memory_used_mib=50,
        memory_total_mib=100,
        utilization_percent=20,
        temperature_celsius=40,
    )
    return ControlSnapshot(
        20,
        "curve",
        "AUTO ON",
        40,
        0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (EndpointSnapshot("one", name, True, 0, gpus=(gpu,), sample_revision=1),),
    )


def test_resize_redraw_is_deferred_coalesced_and_uses_latest_state(monkeypatch) -> None:
    ui = FanAppUI("config.toml", lambda: None, 75, 2)
    first = _single_gpu_snapshot("First")
    latest = _single_gpu_snapshot("Latest")
    ui.snapshot, ui.last_render_time = first, 1
    callbacks: list[object] = []
    renders: list[tuple[ControlSnapshot, float]] = []

    monkeypatch.setattr(
        FanAppUI, "call_after_refresh", lambda _self, callback: callbacks.append(callback)
    )
    monkeypatch.setattr(
        ui, "_render_dashboard", lambda snapshot, now: renders.append((snapshot, now))
    )

    ui.on_resize()
    ui.on_resize()
    assert renders == [] and len(callbacks) == 1 and ui._resize_redraw_pending
    ui.snapshot, ui.last_render_time = latest, 2
    callback = callbacks[0]
    assert callable(callback)
    callback()
    assert renders == [(latest, 2)] and not ui._resize_redraw_pending


def test_resize_redraw_clears_pending_when_scheduling_is_unavailable(monkeypatch) -> None:
    ui = FanAppUI("config.toml", lambda: None, 75, 2)
    ui.snapshot, ui.last_render_time = _single_gpu_snapshot(), 1

    def unavailable(_self, _callback) -> None:
        raise RuntimeError("closing")

    monkeypatch.setattr(FanAppUI, "call_after_refresh", unavailable)
    ui.on_resize()
    assert not ui._resize_redraw_pending


def test_panels_are_ordered_retained_and_deduplicated() -> None:
    app = _DashboardApp()
    gpu = GPUStat(
        "GPU-a",
        "A100",
        memory_used_mib=50,
        memory_total_mib=100,
        utilization_percent=20,
        temperature_celsius=40,
    )
    incomplete = GPUStat(
        "GPU-z", "H100", memory_used_mib=50, utilization_percent=0, temperature_celsius=0
    )
    first = ControlSnapshot(
        20,
        "curve",
        "AUTO ON",
        40,
        0,
        (FanReading(1000, "RUNNING"), FanReading(1000, "RUNNING")),
        (
            EndpointSnapshot(
                "rack/1", "Z endpoint", True, 0, gpus=(incomplete,), sample_revision=1
            ),
            EndpointSnapshot("dgx:one", "A endpoint", True, 0, gpus=(gpu,), sample_revision=1),
        ),
    )
    second = ControlSnapshot(
        20,
        "curve",
        "AUTO ON",
        40,
        0,
        (FanReading(1000, "RUNNING"), FanReading(1000, "RUNNING")),
        (
            EndpointSnapshot("dgx:one", "A endpoint", True, 0, gpus=(), sample_revision=2),
            EndpointSnapshot(
                "rack/1", "Z endpoint", True, 0, gpus=(incomplete,), sample_revision=1
            ),
        ),
    )

    async def exercise() -> None:
        async with app.run_test() as _:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(first, 1)
            await asyncio.sleep(0)
            present = [str(panel.render()) for panel in ui.query(".dgx-panel")]
            assert (
                "50/100 MiB (50%)" in present[1] and "20%" in present[1] and "40.0 C" in present[1]
            )
            ui.update_snapshot(second, 2)
            ui.update_snapshot(second, 2.25)
            await asyncio.sleep(0)
            rendered = [str(panel.render()) for panel in ui.query(".dgx-panel")]
            assert len(rendered) == 2
            assert "A endpoint" in rendered[0] and "Z endpoint" in rendered[1]
            assert "N/A" in rendered[1]
            assert (
                "GPU-a" not in rendered[0]
                and "A100" not in rendered[0]
                and "(last seen)" not in rendered[0]
            )
            assert len(ui.history.points[("dgx:one", "GPU-a", "util")]) == 1

    asyncio.run(exercise())


def test_dashboard_signature_gates_charts_but_not_fast_status_updates(monkeypatch) -> None:
    """Fast health/fan changes must not make cached chart panels look sampled."""
    app = _DashboardApp()
    gpu = GPUStat(
        "GPU-a",
        "A100",
        memory_used_mib=50,
        memory_total_mib=100,
        utilization_percent=20,
        temperature_celsius=40,
    )

    def snapshot(
        endpoints: tuple[EndpointSnapshot, ...], *, state: str = "AUTO ON", healthy: bool = True
    ) -> ControlSnapshot:
        adjusted = tuple(
            EndpointSnapshot(
                endpoint.endpoint_id,
                endpoint.name,
                healthy if endpoint.endpoint_id == "a" else endpoint.healthy,
                endpoint.age_seconds,
                error="poll failed"
                if endpoint.endpoint_id == "a" and not healthy
                else endpoint.error,
                gpus=endpoint.gpus,
                sample_revision=endpoint.sample_revision,
            )
            for endpoint in endpoints
        )
        return ControlSnapshot(
            20,
            "curve",
            state,
            40,
            0,
            (FanReading(1000, "RUNNING"), FanReading(1000, "RUNNING")),
            adjusted,
        )

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
                renders.append(
                    tuple(
                        (endpoint.endpoint_id, endpoint.sample_revision)
                        for endpoint in rendered.endpoint_snapshots
                    )
                )
                original(rendered, now)

            monkeypatch.setattr(ui, "_render_dashboard", traced)
            ui.update_snapshot(snapshot((a1, b1)), 1)
            assert renders == [(("a", 1), ("b", 1))]
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
            assert renders == [
                (("a", 1), ("b", 1)),
                (("a", 2), ("b", 1)),
                (("a", 2), ("b", 2)),
                (("b", 2), ("a", 2)),
                (("a", 2),),
            ]

            before_resize_count = len(ui.history.points[("a", "GPU-a", "util")])
            before_resize_renders = len(renders)
            await pilot.resize_terminal(120, 24)
            await pilot.pause()
            assert len(renders) == before_resize_renders + 1
            ui.update_snapshot(snapshot((a2,)), 5.25)
            assert len(renders) == before_resize_renders + 1
            assert len(ui.history.points[("a", "GPU-a", "util")]) == before_resize_count

    asyncio.run(exercise())


def test_area_step_fills_only_the_configured_cadence_and_preserves_reducers() -> None:
    """Expected DCGM waits are continuous; longer missing/stale periods stay blank."""
    history = DashboardHistory(2)
    now = 120.0
    # These are the actual area widths mounted at 79 and 120 columns.
    for width in (70, 111):
        history.points[("one", "GPU-a", "util")] = [
            HistoryPoint(0, 25),
            HistoryPoint(2, 50),
            HistoryPoint(4, 75),
        ]
        normal = history.area("one", "GPU-a", "util", now, width, 100)[4][5:]
        first, last = 0, int(4 / 120 * width)
        assert all(column != " " for column in normal[first : last + 1])

        history.points[("one", "GPU-a", "temp")] = [HistoryPoint(0, 60), HistoryPoint(3, 80)]
        boundary = history.area("one", "GPU-a", "temp", now, width, 100)[4][5:]
        assert all(column != " " for column in boundary[: int(3 / 120 * width) + 1])

        history.points[("one", "GPU-a", "temp")] = [HistoryPoint(0, 60), HistoryPoint(4, 80)]
        outage = history.area("one", "GPU-a", "temp", now, width, 100)[4][5:]
        successor = int(4 / 120 * width)
        assert " " in outage[1:successor]
        assert outage[successor] != " "

    history.points[("one", "GPU-a", "temp")] = [HistoryPoint(10, 60)]
    prefix = history.area("one", "GPU-a", "temp", now, 120, 100)[4][5:]
    assert prefix[:10] == " " * 10

    history.points[("one", "GPU-a", "mem")] = [HistoryPoint(0, 0)]
    isolated = history.area("one", "GPU-a", "mem", now, 120, 100)[4][5:]
    assert isolated[:3] == "▁▁▁"
    assert isolated[3] == " "

    history.points[("one", "GPU-a", "mem")] = [HistoryPoint(119, 10), HistoryPoint(121, 90)]
    future = history.area("one", "GPU-a", "mem", now, 120, 100)
    assert sum(row[-1] != " " for row in future[:5]) == 1

    # Reducers run before fill: all samples share bin 10, so the visible height
    # distinguishes MEM-last (10), UTIL-average (50), and TEMP-max (90).
    points = [HistoryPoint(10.1, 90), HistoryPoint(10.9, 10)]
    history.points[("one", "GPU-a", "mem")] = points
    history.points[("one", "GPU-a", "util")] = points
    history.points[("one", "GPU-a", "temp")] = points
    heights = {
        metric: sum(
            row[5 + 10] != " " for row in history.area("one", "GPU-a", metric, now, 120, 100)[:5]
        )
        for metric in ("mem", "util", "temp")
    }
    assert heights == {"mem": 1, "util": 3, "temp": 4}


def test_compact_gpu_groups_pair_memory_and_temperature_without_identity_lines() -> None:
    app = _DashboardApp()
    gpus = (
        GPUStat(
            "GPU-z",
            "H100",
            memory_used_mib=20,
            memory_total_mib=100,
            utilization_percent=10,
            temperature_celsius=30,
        ),
        GPUStat(
            "GPU-a",
            "A100",
            memory_used_mib=50,
            memory_total_mib=100,
            utilization_percent=20,
            temperature_celsius=40,
        ),
    )
    snapshot = ControlSnapshot(
        20,
        "curve",
        "AUTO ON",
        40,
        0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (EndpointSnapshot("one", "One", True, 0, gpus=gpus, sample_revision=1),),
    )

    async def exercise() -> None:
        async with app.run_test(size=(100, 24)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 1)
            await pilot.pause()
            lines = str(next(iter(ui.query(".dgx-panel"))).render()).splitlines()
            assert (
                lines[0] == "One"
                and "GPU-" not in "\n".join(lines)
                and "A100" not in "\n".join(lines)
                and "H100" not in "\n".join(lines)
            )
            paired = [line for line in lines if line.startswith("┌ MEM ")]
            assert len(paired) == 2
            assert all(" ┌ TEMP " in line for line in paired)
            assert paired[0].startswith("┌ MEM 50/100 MiB (50%)")
            assert paired[1].startswith("┌ MEM 20/100 MiB (20%)")
            for pair in paired:
                index = lines.index(pair)
                assert lines[index + 4].startswith("┌ UTIL ")
            assert "" not in lines

    asyncio.run(exercise())


def test_mounted_paired_width_reuses_history() -> None:
    app = _DashboardApp()
    gpu = GPUStat(
        "GPU-a",
        "A100",
        memory_used_mib=50,
        memory_total_mib=100,
        utilization_percent=20,
        temperature_celsius=40,
    )
    snapshot = ControlSnapshot(
        20,
        "curve",
        "AUTO ON",
        40,
        0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (EndpointSnapshot("one", "One", True, 0, gpus=(gpu,), sample_revision=1),),
    )

    async def exercise() -> None:
        async with app.run_test(size=(78, 24)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 1)
            scroll = ui.query_one("#dashboard-scroll", VerticalScroll)
            assert "78" in str(ui.query_one("#dashboard-warning").render()) and not scroll.display
            count = len(ui.history.points[("one", "GPU-a", "util")])
            await pilot.resize_terminal(79, 24)
            await pilot.pause()
            assert scroll.display and len(ui.query(".dgx-panel")) == 1

            def assert_paired_width(expected_width: int) -> tuple[int, int, int]:
                lines = str(next(iter(ui.query(".dgx-panel"))).render()).splitlines()
                pair = next(line for line in lines if line.startswith("┌ MEM "))
                split = pair.index(" ┌ TEMP ")
                util = next(line for line in lines if line.startswith("┌ UTIL "))
                assert len(pair) == expected_width and len(util) == expected_width
                assert pair[:split].endswith("┐") and pair[split + 1 :].endswith("┐")
                assert util.endswith("┐")
                return split, len(pair) - split - 1, len(util)

            narrow = assert_paired_width(scroll.size.width)
            assert narrow[0] + narrow[1] + 1 == scroll.size.width
            await pilot.resize_terminal(85, 24)
            await pilot.pause()
            odd = assert_paired_width(scroll.size.width)
            await pilot.resize_terminal(120, 24)
            await pilot.pause()
            wide = assert_paired_width(scroll.size.width)
            assert wide[0] > narrow[0] and wide[2] > narrow[2]
            assert abs(odd[0] - odd[1]) <= 1
            assert count == len(ui.history.points[("one", "GPU-a", "util")])
            await pilot.resize_terminal(78, 24)
            await pilot.resize_terminal(79, 24)
            await pilot.pause()
            assert scroll.display and len(ui.query(".dgx-panel")) == 1
            assert count == len(ui.history.points[("one", "GPU-a", "util")])

    asyncio.run(exercise())


def test_dashboard_scrolls_with_keyboard() -> None:
    app = _DashboardApp()
    gpus = tuple(
        GPUStat(
            f"GPU-{i}",
            "A100",
            memory_used_mib=50,
            memory_total_mib=100,
            utilization_percent=20,
            temperature_celsius=40,
        )
        for i in range(5)
    )
    snapshot = ControlSnapshot(
        20,
        "curve",
        "AUTO ON",
        40,
        0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (
            EndpointSnapshot("z", "Z", True, 0, gpus=gpus, sample_revision=1),
            EndpointSnapshot("a", "A", True, 0, gpus=gpus, sample_revision=1),
        ),
    )

    async def exercise() -> None:
        async with app.run_test(size=(100, 12)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 1)
            await pilot.pause()
            scroll = ui.query_one("#dashboard-scroll", VerticalScroll)
            scroll.focus()
            assert scroll.can_focus and scroll.max_scroll_y > 0
            before = scroll.scroll_y
            await pilot.press("down")
            await pilot.pause()
            assert scroll.scroll_y > before

    asyncio.run(exercise())


def test_dashboard_color_spans_cover_complete_boxes_only() -> None:
    class ColoredDashboardApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI(
                "config.toml", lambda: None, 75, 2, DashboardColors("yellow", "cyan", "#ff0000")
            )

    app = ColoredDashboardApp()
    gpu = GPUStat(
        "GPU-a",
        "A100",
        memory_used_mib=50,
        memory_total_mib=100,
        utilization_percent=20,
        temperature_celsius=40,
    )
    snapshot = ControlSnapshot(
        20,
        "curve",
        "AUTO ON",
        40,
        0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (EndpointSnapshot("one", "One", True, 0, gpus=(gpu,), sample_revision=1),),
    )

    async def exercise() -> None:
        async with app.run_test(size=(100, 24)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 1)
            await pilot.pause()
            rendered = next(iter(ui.query(".dgx-panel"))).render()
            assert rendered.plain.startswith("One\n")
            box_height = (
                ui._plot_height(ui.query_one("#dashboard-scroll", VerticalScroll).size.height, 1, 1)
                + 3
            )
            assert len(rendered.spans) == box_height * 3
            previous_end = len("One\n")
            expected = [("ansi_yellow", "MEM"), ("rgb(255,0,0)", "TEMP")] * box_height + [
                ("ansi_cyan", "UTIL")
            ] * box_height
            for span, (color, metric) in zip(rendered.spans, expected, strict=True):
                row = rendered.plain[span.start : span.end]
                assert str(span.style) == color
                assert row.startswith(("┌", "│", "└")) and row.endswith(("┐", "│", "┘"))
                if row.startswith("┌"):
                    assert row.startswith(f"┌ {metric} ")
                assert rendered.plain[previous_end : span.start] in {"", " ", "\n"}
                previous_end = span.end
            assert previous_end == len(rendered.plain)

    asyncio.run(exercise())


def test_dashboard_without_colors_preserves_plain_geometry_and_sanitizes_external_text() -> None:
    app = _DashboardApp()
    gpu = GPUStat(
        "GPU-a",
        "A100",
        memory_used_mib=50,
        memory_total_mib=100,
        utilization_percent=20,
        temperature_celsius=40,
    )
    snapshot = ControlSnapshot(
        20,
        "curve",
        "AUTO ON",
        40,
        0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (
            EndpointSnapshot(
                "one",
                "One\x1b[31m[bad]",
                False,
                0,
                error="oops\x1b]0;bad\x07",
                gpus=(gpu,),
                sample_revision=1,
            ),
        ),
    )

    async def exercise() -> None:
        async with app.run_test(size=(79, 24)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 1)
            await pilot.pause()
            rendered = next(iter(ui.query(".dgx-panel"))).render()
            assert rendered.spans == []
            assert (
                "\x1b" not in rendered.plain
                and "\x1b" not in ui.query_one("#error-banner").render().plain
            )
            assert "�" in rendered.plain and "�" in ui.query_one("#error-banner").render().plain
            assert all(
                len(line) <= ui.query_one("#dashboard-scroll", VerticalScroll).size.width
                for line in rendered.plain.splitlines()
            )

    asyncio.run(exercise())


def test_two_single_gpu_endpoints_fit_short_viewports_and_expand_when_tall() -> None:
    app = _DashboardApp()
    gpu = GPUStat(
        "GPU-a",
        "A100",
        memory_used_mib=50,
        memory_total_mib=100,
        utilization_percent=20,
        temperature_celsius=40,
    )
    snapshot = ControlSnapshot(
        20,
        "curve",
        "AUTO ON",
        40,
        0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (
            EndpointSnapshot("one", "One", True, 0, gpus=(gpu,), sample_revision=1),
            EndpointSnapshot("two", "Two", True, 0, gpus=(gpu,), sample_revision=1),
        ),
    )

    async def exercise() -> None:
        async with app.run_test(size=(85, 25)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 1)
            await pilot.pause()
            scroll = ui.query_one("#dashboard-scroll", VerticalScroll)
            assert scroll.max_scroll_y == 0
            assert len(ui.query(".dgx-panel")) == 2
            assert all(
                {"MEM", "TEMP", "UTIL"}
                == {
                    line.split()[1]
                    for line in panel.render().plain.splitlines()
                    if line.startswith("┌ ") or " ┌ " in line
                    for line in line.replace(" ┌ ", "\n┌ ").splitlines()
                }
                for panel in ui.query(".dgx-panel")
            )
            compact_points = len(ui.history.points[("one", "GPU-a", "util")])
            compact_lines = len(next(iter(ui.query(".dgx-panel"))).render().plain.splitlines())
            await pilot.resize_terminal(100, 30)
            await pilot.pause()
            assert scroll.max_scroll_y == 0
            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            tall_lines = len(next(iter(ui.query(".dgx-panel"))).render().plain.splitlines())
            assert tall_lines > compact_lines
            assert ui._plot_height(scroll.size.height, 2, 2) == 5
            assert compact_points == len(ui.history.points[("one", "GPU-a", "util")])

    asyncio.run(exercise())


def test_rapid_resize_redraw_uses_final_viewport_without_growing_history() -> None:
    app = _DashboardApp()
    snapshot = _single_gpu_snapshot()

    async def exercise() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 1)
            await pilot.pause()
            history_count = len(ui.history.points[("one", "GPU-a", "util")])
            await pilot.resize_terminal(78, 20)
            await pilot.resize_terminal(85, 25)
            await pilot.resize_terminal(100, 30)
            await pilot.pause()
            scroll = ui.query_one("#dashboard-scroll", VerticalScroll)
            panel = next(iter(ui.query(".dgx-panel"))).render().plain.splitlines()
            pair = next(line for line in panel if line.startswith("┌ MEM "))
            assert (
                scroll.display
                and "Dashboard width" not in ui.query_one("#dashboard-warning").render().plain
            )
            assert len(pair) == scroll.size.width and len(panel) == 17
            assert ui._plot_height(scroll.size.height, 1, 1) == 5
            assert scroll.max_scroll_y == 0
            assert history_count == len(ui.history.points[("one", "GPU-a", "util")])
            await pilot.resize_terminal(78, 20)
            await pilot.pause()
            assert not scroll.display and "78" in ui.query_one("#dashboard-warning").render().plain
            await pilot.resize_terminal(79, 25)
            await pilot.pause()
            assert scroll.display and len(ui.query(".dgx-panel")) == 1
            assert history_count == len(ui.history.points[("one", "GPU-a", "util")])

    asyncio.run(exercise())
