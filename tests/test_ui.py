import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from rich.cells import cell_len
from rich.color import Color as RichColor
from rich.console import Console
from rich.style import Style
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.renderables.digits import Digits
from textual.widgets import Button, Sparkline, Static, TabbedContent

import dgx_fan.ui as ui_module
from dgx_fan.app import DGXFanApp
from dgx_fan.config import DashboardColors, EndpointConfig, load_config
from dgx_fan.models import (
    ControlSnapshot,
    EndpointSnapshot,
    FanReading,
    GPUStat,
    MemoryStat,
    NodeMemorySnapshot,
)
from dgx_fan.ui import DashboardHistory, FanAppUI, FanGauge, HistoryPoint
from dgx_fan.ui_sparkline import UtilTimeAxis, _FixedScaleRenderable, time_axis, time_columns


def _fixed_sparkline_rows(data: tuple[float, ...], width: int = 5, height: int = 1) -> list[str]:
    console = Console(width=width)
    renderable = _FixedScaleRenderable(
        data, width=width, height=height,
        min_color=RichColor.parse("green"), max_color=RichColor.parse("green"),
    )
    return ["".join(segment.text for segment in line) for line in
            console.render_lines(renderable, options=console.options.update(width=width), pad=False)]


def _mounted_sparkline_rows(sparkline: Sparkline) -> list[str]:
    console = Console(width=sparkline.size.width)
    return [
        "".join(segment.text for segment in line)
        for line in console.render_lines(
            sparkline.render(), options=console.options.update(width=sparkline.size.width), pad=False,
        )
    ]


def _mounted_sparkline_text(sparkline: Sparkline) -> str:
    return "".join(_mounted_sparkline_rows(sparkline))


def test_graph_two_fixed_sparkline_renders_absolute_scale_and_absent_data() -> None:
    assert _fixed_sparkline_rows((0, 50, 100)) == ["▁▁▄▄█"]
    assert _fixed_sparkline_rows((50,)) == ["▄▄▄▄▄"]
    assert _fixed_sparkline_rows((50, 50), width=1) == ["▄"]
    assert _fixed_sparkline_rows((0,)) == ["▁▁▁▁▁"]
    assert _fixed_sparkline_rows((0, 0)) == ["▁▁▁▁▁"]
    assert _fixed_sparkline_rows((5, 5)) == ["▁▁▁▁▁"]
    assert _fixed_sparkline_rows(()) == ["     "]
    assert _fixed_sparkline_rows((0, 50, 100), height=2) == ["    █", "▁▁███"]


def test_graph_two_timed_columns_and_axis_share_fixed_window() -> None:
    columns = time_columns((0, 50, 20, 100), (0, 60, 90, 120), 120, 40)
    assert len(columns) == 40
    assert tuple(index for index, value in enumerate(columns) if value is not None) == (0, 20, 30, 39)
    assert (columns[0], columns[20], columns[30], columns[39]) == (0, 50, 20, 100)
    assert time_columns((20, 80), (60, 60), 120, 40)[20] == 80
    assert time_columns((0, 100), (-0.1, 120.1), 120, 40) == (None,) * 40
    assert time_columns((50,), (120,), 120, 40) == (None,) * 39 + (50,)
    assert time_columns((0, 100), (0, 120), 120, 40)[1:39] == (None,) * 38
    axis = time_axis(40)
    assert len(axis) == 40
    assert axis.index("120s") == 0
    assert axis.index("60s") <= 20 < axis.index("60s") + 3
    assert axis.index("30s") <= 30 < axis.index("30s") + 3
    assert axis.endswith("now")


def test_graph_two_timed_columns_hold_only_expected_polling_wait() -> None:
    for values, times in (((100, 0), (0, 2)), ((0, 100), (2, 0))):
        coarse = time_columns(
            values, times, 120, 50, collection_interval_seconds=2,
        )
        assert coarse[:4] == (100, 0, None, None)
    for width in (79, 120):
        columns = time_columns(
            (10, 20, 30, 40, 50), (0, 2, 4, 6, 8), 120, width,
            collection_interval_seconds=2,
        )
        through = int(8 / 120 * width)
        assert all(value is not None for value in columns[:through + 1])
    gap = time_columns(
        (0, 100), (0, 4), 120, 120, collection_interval_seconds=2,
    )
    assert gap[:5] == (0, 0, 0, None, 100)
    assert gap[5:7] == (100, 100)
    assert time_columns((50,), (0,), 241, 120, collection_interval_seconds=2) == (None,) * 120
    assert time_columns((50,), (121,), 120, 120, collection_interval_seconds=2) == (None,) * 120


def test_graph_two_time_window_advances_without_new_revision() -> None:
    class GraphTwoApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI("config.toml", lambda: None, 75, 2, graph_view="graph-2")

    gpu = GPUStat("GPU-a", "A100", utilization_percent=50, temperature_celsius=50)
    snapshot = _fan_snapshot(
        20, "curve", "AUTO ON", 50, 0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (EndpointSnapshot("one", "One", True, 0, gpus=(gpu,), sample_revision=1),),
    )
    app = GraphTwoApp()

    async def exercise() -> None:
        async with app.run_test(size=(79, 30)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 120)
            await pilot.pause()
            sparkline = ui.query_one(Sparkline)
            key = ("one", "GPU-a", "util")
            assert len(ui.history.points[key]) == 1
            assert _mounted_sparkline_rows(sparkline)[-1].endswith("█")

            ui.update_snapshot(snapshot, 124)
            await pilot.pause()
            assert sparkline is ui.query_one(Sparkline)
            assert sparkline.window_end == 124
            assert len(ui.history.points[key]) == 1
            shifted = _mounted_sparkline_rows(sparkline)[-1]
            assert shifted.rstrip().endswith("█") and shifted[-1] == " "

            ui.update_snapshot(snapshot, 241)
            await pilot.pause()
            assert sparkline.window_end == 241
            assert set(_mounted_sparkline_text(sparkline)) == {" "}
            assert not ui.history.points.get(key)

    asyncio.run(exercise())


def _fan_snapshot(
    duty: int,
    reason: str,
    state: str,
    maximum: float | None,
    stage: int | None,
    fans: tuple[FanReading, FanReading],
    endpoints: tuple[EndpointSnapshot, ...],
) -> ControlSnapshot:
    return ControlSnapshot(
        (duty, duty), reason, state, maximum, (stage, stage), (maximum, maximum),
        ("one", "one"), fans, endpoints,
    )


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


def test_app_uses_configured_fallback_before_first_telemetry_read() -> None:
    original = load_config(Path("config.example.toml"))
    config = replace(original, control=replace(original.control, fallback_speed_percent=35))
    app = DGXFanApp(config)

    async def exercise() -> None:
        async with app.run_test():
            assert app.hardware is not None
            assert getattr(app.hardware, "duties", None) == (35, 35)
            assert app.latest is not None and app.latest.duty_percents == (35, 35)

    asyncio.run(exercise())


def test_fan_gauge_ring_fill_boundaries_are_monotonic() -> None:
    previous = -1
    expected_filled = {0: 0, 20: 2, 50: 6, 80: 10, 100: 12}
    for duty, expected in expected_filled.items():
        rendered = FanGauge._content(1, duty, 1234, "RUNNING").plain
        lines = rendered.splitlines()
        ring = lines[1:6]
        assert f"{duty}%" in rendered and "RPM: 1234 RPM" in rendered and "RUNNING" in rendered
        assert all(cell_len(line) == FanGauge.RING_WIDTH for line in ring)
        assert lines[3][5:10].strip() == f"{duty}%"
        filled = sum(line.count("●") for line in ring)
        assert filled >= previous
        assert filled == expected
        previous = filled
    waiting = FanGauge._content(2, None, None, "WAITING").plain
    assert waiting.startswith("Fan 2\n") and "--" in waiting and "WAITING" in waiting
    assert sum(line.count("●") for line in waiting.splitlines()[1:6]) == 0


def test_fan_control_panel_updates_gauges_and_buttons_without_side_effects() -> None:
    calls: list[str] = []

    class FanPanelApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI("config.toml", lambda: calls.append("toggle"), 75, 2)

    app = FanPanelApp()
    snapshot = ControlSnapshot(
        (20, 80),
        "curve",
        "AUTO ON",
        63,
        (0, 2),
        (40, 63),
        ("dgx-1", "dgx-2"),
        (FanReading(1234, "RUNNING"), FanReading(None, "NO TACH")),
        (),
    )

    async def exercise() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            ui = app.query_one(FanAppUI)
            app.query_one(TabbedContent).active = "fan-control"
            ui.update_snapshot(snapshot, 1)
            await pilot.pause()
            status = app.query_one("#fan-status", Static).render().plain
            one = app.query_one("#fan-1-gauge", FanGauge).render().plain
            two = app.query_one("#fan-2-gauge", FanGauge).render().plain
            assert status.splitlines() == [
                "Fan Status / Control · AUTO ON",
                "F1 dgx-1 20% 40.0 C S0",
                "F2 dgx-2 80% 63.0 C S2",
            ]
            assert not any(glyph in status for glyph in "┌┐└┘│")
            assert "F1 dgx-1 20%" in status and "F2 dgx-2 80%" in status
            assert "20%" in one and "RPM: 1234 RPM" in one and "RUNNING" in one
            assert "80%" in two and "RPM: N/A" in two and "NO TACH" in two
            refreshed = ControlSnapshot(
                (50, 80),
                "curve",
                "AUTO ON",
                70,
                (1, 3),
                (52, 70),
                ("dgx-1", "dgx-2"),
                (FanReading(1500, "RUNNING"), FanReading(900, "STOPPED")),
                (),
            )
            ui.update_snapshot(refreshed, 2)
            assert "50%" in app.query_one("#fan-1-gauge", FanGauge).render().plain
            assert "RPM: 1500 RPM" in app.query_one("#fan-1-gauge", FanGauge).render().plain
            assert "RPM: 900 RPM" in app.query_one("#fan-2-gauge", FanGauge).render().plain
            assert "STOPPED" in app.query_one("#fan-2-gauge", FanGauge).render().plain
            power = app.query_one("#power-toggle", Button)
            power.press()
            await pilot.pause()
            assert calls == ["toggle"]
            setting = app.query_one("#fan-settings", Button)
            setting.press()
            setting.remove_class("-active")
            setting.action_press()
            await pilot.pause()
            assert calls == ["toggle"] and ui.snapshot is refreshed
            # Textual keeps the click active briefly; a subsequent keyboard
            # activation is only valid once that public guard has cleared.
            power.remove_class("-active")
            power.action_press()
            await pilot.pause()
            assert calls == ["toggle", "toggle"]

            power.add_class("-active")
            power.action_press()
            await pilot.pause()
            assert calls == ["toggle", "toggle"]
            power.remove_class("-active")
            power.disabled = True
            power.press()
            await pilot.pause()
            assert calls == ["toggle", "toggle"]
            power.disabled = False
            power.display = False
            power.press()
            await pilot.pause()
            assert calls == ["toggle", "toggle"]

    asyncio.run(exercise())


def test_fan_status_is_sanitized_and_fits_79_columns() -> None:
    class FanPanelApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI("config.toml", lambda: None, 75, 2)

    snapshot = ControlSnapshot(
        (20, 80), "temperature curve", "AUTO ON", 63, (0, 2), (40, 63),
        ("very-long-dgx-name\x1b[31m", "second-dgx\x07-with-extra"),
        (FanReading(1234, "RUNNING"), FanReading(None, "NO TACH")), (),
    )
    app = FanPanelApp()

    async def exercise() -> None:
        async with app.run_test(size=(79, 30)) as pilot:
            app.query_one(TabbedContent).active = "fan-control"
            app.query_one(FanAppUI).update_snapshot(snapshot, 1)
            await pilot.pause()
            status = app.query_one("#fan-status", Static).render().plain
            lines = status.splitlines()
            content_width = app.query_one("#fan-status", Static).content_size.width
            assert len(lines) == 3 and all(cell_len(line) <= content_width for line in lines)
            assert "\x1b" not in status and "\x07" not in status
            assert "F1" in lines[1] and "F2" in lines[2]

    asyncio.run(exercise())


@pytest.mark.parametrize("state", ["AUTO ON", "USER OFF", "SAFETY OVERRIDE", "STARTUP BOOST"])
def test_fan_status_header_states_fit_actual_widget_width(
    state: Literal["AUTO ON", "USER OFF", "SAFETY OVERRIDE", "STARTUP BOOST"]
) -> None:
    class FanPanelApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI("config.toml", lambda: None, 75, 2)

    snapshot = ControlSnapshot(
        (100, 100), "safety recovery temperature", state, 74, (2, 2), (74, 74),
        ("dgx-1", "dgx-2"), (FanReading(1200, "RUNNING"), FanReading(1200, "RUNNING")), (),
    )
    app = FanPanelApp()

    async def exercise() -> None:
        async with app.run_test(size=(79, 30)) as pilot:
            app.query_one(TabbedContent).active = "fan-control"
            app.query_one(FanAppUI).update_snapshot(snapshot, 1)
            await pilot.pause()
            status = app.query_one("#fan-status", Static)
            assert cell_len(status.render().plain.splitlines()[0]) <= status.content_size.width

    asyncio.run(exercise())


def test_fan_control_two_row_geometry_survives_resize() -> None:
    app = _DashboardApp()

    async def exercise() -> None:
        async with app.run_test(size=(79, 25)) as pilot:
            app.query_one(TabbedContent).active = "fan-control"
            await pilot.pause()
            top = app.query_one("#fan-top-row")
            gauges = app.query_one("#fan-gauge-row")
            pane = app.query_one("#fan-control")
            one = app.query_one("#fan-1-gauge", FanGauge)
            two = app.query_one("#fan-2-gauge", FanGauge)
            status = app.query_one("#fan-status", Static)
            assert top.region.bottom <= gauges.region.y
            assert one.region.right <= two.region.x and one.region.height == two.region.height == 9
            assert _contained_in(pane, top) and _contained_in(pane, gauges)
            assert _contained_in(pane, status) and _contained_in(pane, one) and _contained_in(pane, two)
            _assert_complete_fan_panel_borders(status, one, two)
            _assert_gauge_content_fits(one)
            _assert_gauge_content_fits(two)
            await pilot.resize_terminal(100, 30)
            await pilot.pause()
            assert top.region.bottom <= gauges.region.y
            assert one.region.right <= two.region.x and one.region.height == two.region.height == 9
            assert _contained_in(pane, top) and _contained_in(pane, gauges)
            assert _contained_in(pane, status) and _contained_in(pane, one) and _contained_in(pane, two)
            _assert_complete_fan_panel_borders(status, one, two)
            _assert_gauge_content_fits(one)
            _assert_gauge_content_fits(two)
            await pilot.resize_terminal(79, 25)
            await pilot.pause()
            assert _contained_in(pane, top) and _contained_in(pane, gauges)
            assert _contained_in(pane, status) and _contained_in(pane, one) and _contained_in(pane, two)
            _assert_complete_fan_panel_borders(status, one, two)
            _assert_gauge_content_fits(one)
            _assert_gauge_content_fits(two)

    asyncio.run(exercise())


def _contained_in(parent: Static, child: Static) -> bool:
    return (
        child.region.x >= parent.region.x
        and child.region.y >= parent.region.y
        and child.region.right <= parent.region.right
        and child.region.bottom <= parent.region.bottom
    )


def test_dashboard_status_row_shows_live_fan_rpm_without_chart_redraw(monkeypatch) -> None:
    """Fan tach refreshes remain independent from revision-gated chart rendering."""
    app = _DashboardApp()
    endpoint = EndpointSnapshot(
        "one", "One", False, 1, error="poll failed", retry_count=3, failed_attempts=4, sample_revision=1
    )
    initial = _fan_snapshot(
        20,
        "curve",
        "AUTO ON",
        40,
        0,
        (FanReading(1234, "RUNNING"), FanReading(None, "NO TACH")),
        (endpoint,),
    )
    refreshed = _fan_snapshot(
        20,
        "curve",
        "AUTO ON",
        40,
        0,
        (FanReading(1500, "RUNNING"), FanReading(900, "RUNNING")),
        (endpoint,),
    )

    async def exercise() -> None:
        async with app.run_test(size=(79, 24)) as pilot:
            ui = app.query_one(FanAppUI)
            row = app.query_one("#dashboard-status-row", Horizontal)
            banner = app.query_one("#error-banner", Static)
            fan_one = app.query_one("#dashboard-fan-1-rpm", Static)
            separator = app.query_one("#dashboard-fan-separator", Static)
            fan_two = app.query_one("#dashboard-fan-2-rpm", Static)
            assert [child.id for child in row.children] == [
                "error-banner",
                "dashboard-fan-1-rpm",
                "dashboard-fan-separator",
                "dashboard-fan-2-rpm",
            ]
            assert banner.render().plain == "Waiting for DCGM metrics…"
            assert fan_one.render().plain == "Fan 1: N/A"
            assert separator.render().plain == " | "
            assert fan_two.render().plain == "Fan 2: N/A"

            renders: list[float] = []
            original = ui._render_dashboard

            def traced(snapshot: ControlSnapshot, now: float) -> None:
                renders.append(now)
                original(snapshot, now)

            monkeypatch.setattr(ui, "_render_dashboard", traced)
            ui.update_snapshot(initial, 1)
            await pilot.pause()
            assert renders == [1]
            assert "One: FAILED after 4 attempts: poll failed (sample age: 1.0s)" in banner.render().plain
            assert fan_one.render().plain == "Fan 1: 1234 RPM"
            assert fan_two.render().plain == "Fan 2: N/A"
            assert row.region.height == 1
            assert row.region.right <= app.screen.region.right
            assert all(widget.region.y == row.region.y for widget in (banner, fan_one, separator, fan_two))
            assert banner.region.right <= fan_one.region.x <= separator.region.x <= fan_two.region.x
            assert all(_contained_in(row, widget) for widget in (banner, fan_one, separator, fan_two))

            ui.update_snapshot(refreshed, 1.25)
            assert renders == [1]
            assert fan_one.render().plain == "Fan 1: 1500 RPM"
            assert fan_two.render().plain == "Fan 2: 900 RPM"
            assert len(ui.history.points) == 0

            for width in (100, 79):
                await pilot.resize_terminal(width, 24)
                await pilot.pause()
                assert row.region.height == 1
                assert row.region.right <= app.screen.region.right
                assert all(_contained_in(row, widget) for widget in (banner, fan_one, separator, fan_two))
                assert banner.region.right <= fan_one.region.x <= separator.region.x <= fan_two.region.x

    asyncio.run(exercise())


def _assert_complete_fan_panel_borders(*widgets: Static) -> None:
    for widget in widgets:
        top, right, bottom, left = widget.styles.border
        assert all(edge[0] for edge in (top, right, bottom, left))


def _assert_gauge_content_fits(gauge: FanGauge) -> None:
    lines = gauge.render().plain.splitlines()
    assert len(lines) == 7
    assert lines[0].startswith("Fan ")
    assert all(cell_len(line) == FanGauge.RING_WIDTH for line in lines[1:6])
    assert lines[-1].startswith("RPM: ")
    assert gauge.content_region.height >= len(lines)


def test_poll_exception_is_supervised_per_endpoint_and_unmount_cancels_tasks(monkeypatch) -> None:
    config = load_config(Path("config.example.toml"))
    app = DGXFanApp(config)

    async def failing_collect(endpoint, now: float | None = None):
        raise RuntimeError("poll boom")

    async def exercise() -> None:
        async with app.run_test() as pilot:
            monkeypatch.setattr(app.collector, "collect_endpoint", failing_collect)
            await app._poll_once(config.endpoints[0], 0)
            app.control_tick(1)
            await pilot.pause()
            assert app.latest is not None and app.latest.duty_percents == (100, 100)
            assert not app.endpoints[0].healthy
        assert app.hardware is not None
        assert not getattr(app.hardware, "released", False)

    asyncio.run(exercise())


def test_endpoint_poll_loops_do_not_wait_for_each_other(monkeypatch) -> None:
    original = load_config(Path("config.example.toml"))
    config = replace(
        original,
        endpoints=(
            original.endpoints[0],
            EndpointConfig("two", "Two", "http://two:9400/metrics"),
        ),
    )
    app = DGXFanApp(config)
    first_entered = asyncio.Event()
    allow_first = asyncio.Event()
    second_completed = asyncio.Event()

    async def fake_poll_once(endpoint, now=None) -> None:
        if endpoint.id == "dgx-1":
            first_entered.set()
            await allow_first.wait()
        else:
            second_completed.set()

    monkeypatch.setattr(app, "_poll_once", fake_poll_once)

    async def exercise() -> None:
        first = asyncio.create_task(app._poll_loop(config.endpoints[0]))
        second = asyncio.create_task(app._poll_loop(config.endpoints[1]))
        await first_entered.wait()
        await asyncio.wait_for(second_completed.wait(), timeout=0.2)
        first.cancel()
        second.cancel()
        for task in (first, second):
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())


def test_node_memory_poll_loop_does_not_wait_for_dcgm_poll_loop(monkeypatch) -> None:
    original = load_config(Path("config.example.toml"))
    endpoint = EndpointConfig(
        "dgx-1", "DGX-1", "http://dcgm/metrics", "node-exporter", "http://node/metrics"
    )
    app = DGXFanApp(replace(original, endpoints=(endpoint,)))
    dcgm_entered = asyncio.Event()
    release_dcgm = asyncio.Event()
    node_completed = asyncio.Event()

    async def blocked_dcgm(endpoint, now=None) -> None:
        dcgm_entered.set()
        await release_dcgm.wait()

    async def completed_node(endpoint, now=None) -> None:
        node_completed.set()

    monkeypatch.setattr(app, "_poll_once", blocked_dcgm)
    monkeypatch.setattr(app, "_node_poll_once", completed_node)

    async def exercise() -> None:
        dcgm_task = asyncio.create_task(app._poll_loop(endpoint))
        node_task = asyncio.create_task(app._node_poll_loop(endpoint))
        await dcgm_entered.wait()
        await asyncio.wait_for(node_completed.wait(), timeout=0.2)
        dcgm_task.cancel()
        node_task.cancel()
        for task in (dcgm_task, node_task):
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(exercise())


def test_dcgm_poll_completes_while_node_retry_is_blocked(monkeypatch) -> None:
    original = load_config(Path("config.example.toml"))
    endpoint = EndpointConfig(
        "dgx-1", "DGX-1", "http://dcgm/metrics", "node-exporter", "http://node/metrics"
    )
    app = DGXFanApp(replace(original, endpoints=(endpoint,)))
    node_retrying = asyncio.Event()
    dcgm_completed = asyncio.Event()

    async def completed_dcgm(endpoint, now=None) -> None:
        dcgm_completed.set()

    async def blocked_node_retry(endpoint, now=None) -> None:
        node_retrying.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app, "_poll_once", completed_dcgm)
    monkeypatch.setattr(app, "_node_poll_once", blocked_node_retry)

    async def exercise() -> None:
        node_task = asyncio.create_task(app._node_poll_loop(endpoint))
        await node_retrying.wait()
        dcgm_task = asyncio.create_task(app._poll_loop(endpoint))
        await asyncio.wait_for(dcgm_completed.wait(), timeout=0.2)
        for task in (dcgm_task, node_task):
            task.cancel()
        for task in (dcgm_task, node_task):
            with pytest.raises(asyncio.CancelledError):
                await task

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
    assert history.area("one", "GPU-a", "util", 120, 1, 100)[4][5:] == "."
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
        assert [line[5 + 2] for line in rendered[:5]] == [" ", " ", " ", " ", "."]
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
    assert filled_rows == {"mem": 4, "util": 3, "temp": 5}

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

        assert sum(line.count(".") for line in zero_glyphs) == 1
        assert zero_glyphs[height - 1].endswith(".")
        assert all(line == " " * width for line in (row[5:] for row in missing_rows[:-1]))
        assert sum(line.count(".") for line in positive_glyphs) == 1
        assert positive_glyphs[height - 1].endswith(".")


@pytest.mark.parametrize(
    ("height", "value", "expected"),
    (
        (1, 10, (".",)),
        (1, 100, (":",)),
        (2, 2, (" ", ".")),
        (2, 29, (" ", ":")),
        (2, 50, (" ", ":")),
        (2, 100, (":", ":")),
        (4, 10, (" ", " ", " ", ".")),
        (4, 20, (" ", " ", " ", ":")),
        (4, 30, (" ", " ", ".", ":")),
        (4, 100, (":", ":", ":", ":")),
        (5, 10, (" ", " ", " ", " ", ".")),
        (5, 20, (" ", " ", " ", " ", ":")),
        (5, 30, (" ", " ", " ", ".", ":")),
        (5, 40, (" ", " ", " ", ":", ":")),
        (5, 50, (" ", " ", ".", ":", ":")),
        (5, 100, (":", ":", ":", ":", ":")),
    ),
)
def test_area_renderer_uses_dot_colon_levels_at_compact_heights(
    height: int, value: float, expected: tuple[str, ...]
) -> None:
    """Positive values retain their relative height in portable ASCII cells."""
    now = 120.0
    history = DashboardHistory(2)
    history.points[("one", "GPU-a", "mem")] = [HistoryPoint(now, value)]

    rows = history.area("one", "GPU-a", "mem", now, 1, 100, height)
    glyphs = tuple(row[-1] for row in rows[:-1])
    assert glyphs == expected
    assert set(glyphs) <= {" ", ".", ":"}


def test_area_renderer_keeps_missing_blank_zero_baseline_and_clamps_bounds() -> None:
    now = 120.0
    height = 2
    history = DashboardHistory(2)
    key = ("one", "GPU-a", "util")

    assert tuple(row[-1] for row in history.area(*key, now, 1, 100, height)[:-1]) == (" ", " ")
    history.points[key] = [HistoryPoint(now, 0)]
    assert tuple(row[-1] for row in history.area(*key, now, 1, 100, height)[:-1]) == (" ", ".")
    history.points[key] = [HistoryPoint(now, -5)]
    assert tuple(row[-1] for row in history.area(*key, now, 1, 100, height)[:-1]) == (" ", ".")
    history.points[key] = [HistoryPoint(now, 150)]
    assert tuple(row[-1] for row in history.area(*key, now, 1, 100, height)[:-1]) == (":", ":")


class _DashboardApp(App[None]):
    def compose(self) -> ComposeResult:
        yield FanAppUI("config.toml", lambda: None, 75, 2)


def test_graph_two_headless_two_endpoint_power_and_resize() -> None:
    class GraphTwoApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI(
                "config.toml", lambda: None, 75, 2,
                DashboardColors("yellow", "cyan", "red", "green"), graph_view="graph-2",
            )

    gpu = GPUStat("GPU-a", "A100", 114399, 124546, 40, 55, 250)
    snapshot = _fan_snapshot(
        20, "curve", "AUTO ON", 55, 0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (
            EndpointSnapshot("one", "One", True, 0, gpus=(gpu,), sample_revision=1),
            EndpointSnapshot("two", "Two", True, 0, gpus=(gpu,), sample_revision=1),
        ),
    )
    app = GraphTwoApp()

    async def exercise() -> None:
        async with app.run_test(size=(102, 24)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 120)
            await pilot.pause()
            dashboard = next(iter(ui.query(".dgx-panel"))).render()
            text = "\n".join(panel.render().plain for panel in ui.query(".dgx-panel"))
            assert "TEMP" in text and "MEM" in text and "UTIL" in text and "POWER" in text
            assert text.index("TEMP") < text.index("MEM") < text.index("UTIL") < text.index("POWER")
            assert "GiB" in text and "TEMP 120s" in text and "X: overlap" in text
            styles = " ".join(str(span.style) for span in dashboard.spans)
            for color in ("ansi_yellow", "ansi_cyan", "ansi_red", "ansi_green"):
                assert color in styles
            sparklines = list(ui.query(Sparkline))
            assert len(sparklines) == 2
            assert all(sparkline.data == (40,) and sparkline.display for sparkline in sparklines)
            assert ui.query_one("#graph-two-util-row").size.height == 6
            assert all(sparkline.size.height == 4 for sparkline in sparklines)
            axes = list(ui.query(UtilTimeAxis))
            assert len(axes) == 2 and all(axis.size.height == 1 for axis in axes)
            assert all(axis.region.x == sparkline.region.x and axis.size.width == sparkline.size.width
                       for axis, sparkline in zip(axes, sparklines, strict=True))
            assert all(axis.render() == time_axis(axis.size.width) for axis in axes)
            gutter = ui.query_one(".graph-two-util-gutter")
            assert gutter.size.width == 2
            assert sparklines[1].region.x - sparklines[0].region.right == 2
            assert ui.query_one("#dashboard-scroll", VerticalScroll).max_scroll_y == 0
            width = sparklines[0].size.width
            for values, expected in (
                ((), [" " * width] * 4),
                ((0,), [" " * width] * 3 + [" " * (width - 1) + "▁"]),
                ((50,), [" " * width] * 2 + [" " * (width - 1) + "█"] * 2),
                ((100,), [" " * (width - 1) + "█"] * 4),
            ):
                sparklines[0].data = values
                assert _mounted_sparkline_rows(sparklines[0]) == expected
            sparklines[0].data = (40,)
            assert all(sparkline.min_color == sparkline.max_color for sparkline in sparklines)
            assert all(sparkline.max_color is not None and sparkline.max_color.hex == "#00FFFF" for sparkline in sparklines)
            await pilot.resize_terminal(102, 30)
            await pilot.pause()
            assert ui.query_one("#dashboard-scroll", VerticalScroll).max_scroll_y == 0
            failed = replace(
                snapshot,
                endpoint_snapshots=(
                    replace(snapshot.endpoint_snapshots[0], healthy=False, error="offline"),
                    snapshot.endpoint_snapshots[1],
                ),
            )
            ui.update_snapshot(failed, 121)
            await pilot.pause()
            text = next(iter(ui.query(".dgx-panel"))).render().plain
            assert text.count("N/A") >= 3
            assert next(iter(ui.query(Sparkline))).data == ()
            zero_total = replace(
                snapshot,
                endpoint_snapshots=(
                    replace(snapshot.endpoint_snapshots[0], gpus=(replace(gpu, memory_total_mib=0),)),
                    snapshot.endpoint_snapshots[1],
                ),
            )
            ui.update_snapshot(zero_total, 122)
            await pilot.pause()
            text = next(iter(ui.query(".dgx-panel"))).render().plain
            assert "GiB" in text and "N/A" not in text and " /" not in text
            partial_memory = replace(
                snapshot,
                endpoint_snapshots=(
                    replace(
                        snapshot.endpoint_snapshots[0],
                        gpus=(gpu, replace(gpu, memory_used_mib=None)),
                        sample_revision=2,
                    ),
                    snapshot.endpoint_snapshots[1],
                ),
            )
            ui.update_snapshot(partial_memory, 122.5)
            await pilot.pause()
            assert "N/A" in next(iter(ui.query(".dgx-panel"))).render().plain
            uma = replace(
                snapshot,
                endpoint_snapshots=(
                    replace(
                        snapshot.endpoint_snapshots[0],
                        memory_source="node-exporter",
                        uma_memory=MemoryStat(114399, 0),
                        sample_revision=3,
                    ),
                    snapshot.endpoint_snapshots[1],
                ),
            )
            ui.update_snapshot(uma, 123)
            await pilot.pause()
            text = next(iter(ui.query(".dgx-panel"))).render().plain
            assert "GiB" in text and "N/A" not in text
            await pilot.resize_terminal(85, 25)
            await pilot.pause()
            scroll = ui.query_one("#dashboard-scroll", VerticalScroll)
            assert scroll.display and scroll.max_scroll_y == 0
            assert all(sparkline.size.height == 4 for sparkline in sparklines)
            assert all(axis.size.width == sparkline.size.width for axis, sparkline in
                       zip(axes, sparklines, strict=True))
            assert all(axis.render() == time_axis(axis.size.width) for axis in axes)
            await pilot.resize_terminal(79, 30)
            await pilot.pause()
            scroll = ui.query_one("#dashboard-scroll", VerticalScroll)
            assert scroll.display and scroll.max_scroll_y == 0
            high = GPUStat(
                "GPU-high",
                "A100",
                976_562_500 * 1024,
                976_562_500 * 1024,
                100,
                150,
                999999,
            )
            extreme = replace(
                snapshot,
                endpoint_snapshots=(
                    replace(snapshot.endpoint_snapshots[0], gpus=(high,), sample_revision=4),
                    replace(snapshot.endpoint_snapshots[1], gpus=(high,), sample_revision=4),
                ),
            )
            ui.update_snapshot(extreme, 124)
            await pilot.pause()
            text = next(iter(ui.query(".dgx-panel"))).render().plain
            assert "TEMP 150 C" in text and "MEM 976562500 GiB" in text
            assert "UTIL 100%" in text and "POWER 999999 W" in text and "…" not in text
            styles = " ".join(str(span.style) for span in next(iter(ui.query(".dgx-panel"))).render().spans)
            for color in ("ansi_yellow", "ansi_cyan", "ansi_red", "ansi_green"):
                assert color in styles
            assert scroll.max_scroll_y == 0

    asyncio.run(exercise())


def test_graph_two_sparkline_uses_observed_endpoint_maxima_without_gap_fill() -> None:
    history = DashboardHistory(2)
    history.points[("one", "GPU-a", "util")] = [HistoryPoint(10, 20), HistoryPoint(70, 0)]
    history.points[("one", "GPU-b", "util")] = [HistoryPoint(10, 55)]

    # Sparkline accepts only numbers, so absent intervals are omitted rather
    # than represented as invented zero readings.
    assert history.endpoint_values("one", "util", 120) == (55, 0)


def test_graph_two_sparkline_updates_zero_stale_and_mode_switch() -> None:
    class GraphTwoApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI("config.toml", lambda: None, 75, 2, graph_view="graph-2")

    gpu = GPUStat("GPU-a", "A100", 1024, 2048, 5, 50, 12)
    snapshot = _fan_snapshot(
        20, "curve", "AUTO ON", 50, 0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (EndpointSnapshot("one", "One", True, 0, gpus=(gpu,), sample_revision=1),),
    )
    app = GraphTwoApp()

    async def exercise() -> None:
        async with app.run_test(size=(79, 30)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 120)
            await pilot.pause()
            sparkline = ui.query_one(Sparkline)
            assert sparkline.data == (5,) and sparkline.display
            assert _mounted_sparkline_rows(sparkline) == (
                [" " * sparkline.size.width] * 3
                + [" " * (sparkline.size.width - 1) + "▂"]
            )
            assert "UTIL 5% · 0–100% · 0s · One" in ui.query_one(".graph-two-util-label", Static).render().plain

            zero = replace(snapshot, endpoint_snapshots=(
                replace(snapshot.endpoint_snapshots[0], gpus=(replace(gpu, utilization_percent=0),), sample_revision=2),
            ))
            ui.update_snapshot(zero, 121)
            await pilot.pause()
            assert sparkline.data == (5, 0) and sparkline.display
            rows = _mounted_sparkline_rows(sparkline)
            assert rows[:3] == [" " * sparkline.size.width] * 3
            assert rows[-1] == " " * (sparkline.size.width - 1) + "▂"
            assert "UTIL 0% · 0–100% · 1s · One" in ui.query_one(".graph-two-util-label", Static).render().plain

            ui.reconfigure_display(75, 1, DashboardColors(utilization="green"), graph_view="graph-2")
            await pilot.pause()
            assert sparkline.max_color is not None and sparkline.max_color.hex == "#008000"
            assert sparkline.collection_interval_seconds == 1
            old_hold = time_columns(
                sparkline.data, sparkline.sample_times, 123, sparkline.size.width,
                collection_interval_seconds=2,
            )
            new_hold = time_columns(
                sparkline.data, sparkline.sample_times, 123, sparkline.size.width,
                collection_interval_seconds=sparkline.collection_interval_seconds,
            )
            assert old_hold[-1] is not None and new_hold[-1] is None
            console = Console(width=sparkline.size.width)
            rendered = console.render_lines(
                sparkline.render(),
                options=console.options.update(width=sparkline.size.width),
                pad=False,
            )
            assert any(
                segment.style is not None and segment.style.color is not None
                and segment.style.color.get_truecolor().hex == "#008000"
                for line in rendered for segment in line if segment.text.strip()
            )

            stale = replace(zero, endpoint_snapshots=(
                replace(zero.endpoint_snapshots[0], stale=True, error="offline"),
            ))
            ui.update_snapshot(stale, 122)
            await pilot.pause()
            assert sparkline.data == ()
            assert sparkline.display
            assert set(_mounted_sparkline_text(sparkline)) == {" "}
            assert "UTIL N/A · 0–100% · N/A · One" in ui.query_one(".graph-two-util-label", Static).render().plain

            sparse = replace(zero, endpoint_snapshots=(
                replace(zero.endpoint_snapshots[0], sample_revision=3),
            ))
            ui.update_snapshot(sparse, 130)
            await pilot.pause()
            assert sparkline.data == (5, 0, 0) and sparkline.display
            columns = time_columns(sparkline.data, (120, 121, 130), 130, sparkline.size.width)
            assert columns[-3] is None and columns[-1] == 0

            ui.graph_view = "graph-1"
            ui.update_snapshot(snapshot, 123)
            await pilot.pause()
            assert len(ui.query(Sparkline)) == 0
            ui.graph_view = "graph-2"
            ui.update_snapshot(snapshot, 124)
            await pilot.pause()
            assert len(ui.query(Sparkline)) == 1
            assert ui.query_one("#dashboard-scroll", VerticalScroll).max_scroll_y == 0

    asyncio.run(exercise())


def test_graph_two_sparkline_fixed_scale_label_survives_long_name_at_79_columns() -> None:
    class GraphTwoApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI("config.toml", lambda: None, 75, 2, graph_view="graph-2")

    name = "DGX-with-an-intentionally-very-long-endpoint-name"
    first = GPUStat("GPU-a", "A100", 1024, 2048, 5, 50, 12)
    second = replace(first, utilization_percent=10)
    snapshot = _fan_snapshot(
        20, "curve", "AUTO ON", 50, 0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (
            EndpointSnapshot("one", name, True, 0, gpus=(first,), sample_revision=1),
            EndpointSnapshot("two", name, True, 0, gpus=(first,), sample_revision=1),
        ),
    )
    updated = replace(snapshot, endpoint_snapshots=(
        replace(snapshot.endpoint_snapshots[0], gpus=(second,), sample_revision=2),
        replace(snapshot.endpoint_snapshots[1], gpus=(second,), sample_revision=2),
    ))
    app = GraphTwoApp()

    async def exercise() -> None:
        async with app.run_test(size=(79, 30)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 120)
            ui.update_snapshot(updated, 121)
            await pilot.pause()
            labels = [label.render().plain for label in ui.query(".graph-two-util-label")]
            assert len(labels) == 2
            assert all("UTIL 10% · 0–100% · 1s" in label for label in labels)
            assert all(len(label) <= 39 for label in labels)
            assert ui.query_one("#dashboard-scroll", VerticalScroll).max_scroll_y == 0

    asyncio.run(exercise())


def test_graph_two_sparkline_blanks_empty_and_shows_single_zero_sample() -> None:
    class GraphTwoApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI("config.toml", lambda: None, 75, 2, graph_view="graph-2")

    zero = GPUStat("GPU-a", "A100", 1024, 2048, 0, 50, 12)
    empty = _fan_snapshot(
        20, "curve", "AUTO ON", None, 0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (EndpointSnapshot("one", "One", True, 0, gpus=(), sample_revision=1),),
    )
    one_zero = replace(empty, endpoint_snapshots=(
        replace(empty.endpoint_snapshots[0], gpus=(zero,), sample_revision=2),
    ))
    app = GraphTwoApp()

    async def exercise() -> None:
        async with app.run_test(size=(79, 30)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(empty, 120)
            await pilot.pause()
            sparkline = ui.query_one(Sparkline)
            assert sparkline.data == () and sparkline.display
            assert len(ui.query(".graph-two-util-gutter")) == 0
            assert ui.query_one(UtilTimeAxis).size.width == sparkline.size.width
            ui.update_snapshot(one_zero, 121)
            await pilot.pause()
            assert sparkline.data == (0,) and sparkline.display
            assert _mounted_sparkline_rows(sparkline) == (
                [" " * sparkline.size.width] * 3
                + [" " * (sparkline.size.width - 1) + "▁"]
            )

    asyncio.run(exercise())


def test_native_digits_metric_layout_fits_wide_and_79_column_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    ui = FanAppUI("config.toml", lambda: None, 75, 2, graph_view="graph-2")
    expected: list[str] = []
    line = ""
    for segment in Digits("50.0").render(Style()):
        if segment.text == "\n":
            expected.append(line)
            line = ""
        else:
            line += segment.text
    assert Digits.get_width("50.0") == sum(3 if character.isdigit() else 1 for character in "50.0")
    assert ui._native_digit_lines("50.0") == tuple(expected)
    ordinary = ("50 C", "111 GiB", "0%", "12 W")
    rows, spans = ui._large_metric_rows(ordinary, 39)
    assert len(rows) == 4 and all(len(row) == 39 for row in rows)
    assert all(label in rows[0] for label in ("TEMP", "MEM", "UTIL", "POWER"))
    assert all(unit in rows[0] for unit in ("C", "GiB", "%", "W"))
    assert any(ord(character) > 127 for row in rows[1:] for character in row)
    assert spans[0] == ((0, 0, 9), (1, 10, 9), (2, 20, 9), (3, 30, 9))
    assert "." not in "\n".join(rows)
    assert len(rows[1:]) == len(ui._native_digit_lines("50"))

    normal_power_rows, normal_power_spans = ui._large_metric_rows(("50 C", "111 GiB", "55%", "250 W"), 39)
    assert len(normal_power_rows) == 4 and all(len(row) == 39 for row in normal_power_rows)
    assert all(label in normal_power_rows[0] for label in ("TEMP", "MEM", "UTIL", "POWER"))
    assert normal_power_spans[0][1][1] > normal_power_spans[0][0][1] + normal_power_spans[0][0][2]
    assert normal_power_spans[0][2][1] > normal_power_spans[0][1][1] + normal_power_spans[0][1][2]

    class FakeTTY:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 8

    fake_tty = FakeTTY()
    monkeypatch.setattr(ui_module.sys, "stdin", fake_tty)
    monkeypatch.setattr(ui_module.os, "ttyname", lambda _fd: "/dev/tty8")
    monkeypatch.setenv("TERM", "xterm-256color")
    assert ui_module._is_linux_virtual_console()
    monkeypatch.delenv("TERM")
    assert ui_module._is_linux_virtual_console()
    plain_console, _spans = ui._large_metric_rows(ordinary, 39)
    assert plain_console == [f"{label} {value}" for label, value in zip(("TEMP", "MEM", "UTIL", "POWER"), ordinary, strict=True)]
    assert "#" not in "\n".join(plain_console)
    monkeypatch.setenv(ui_module._TTY8_FONT_MARKER, "1")
    marked_console, _spans = ui._large_metric_rows(ordinary, 39)
    assert len(marked_console) == 4
    assert any(ord(character) > 127 for row in marked_console[1:] for character in row)

    extreme = ("1500 C", "976562500 GiB", "100%", "999999 W")
    rows, _spans = ui._large_metric_rows(extreme, 39)
    assert rows == [f"{label} {value}" for label, value in zip(("TEMP", "MEM", "UTIL", "POWER"), extreme, strict=True)]
    assert all("…" not in row and value in row for row, value in zip(rows, extreme, strict=True))


def test_graph_two_console_plain_fallback_two_dgx_79_columns_has_no_scroll(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeTTY:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 8

    class GraphTwoApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI("config.toml", lambda: None, 75, 2, graph_view="graph-2")

    monkeypatch.setattr(ui_module.sys, "stdin", FakeTTY())
    monkeypatch.setattr(ui_module.os, "ttyname", lambda _fd: "/dev/tty8")
    gpu = GPUStat("GPU-a", "A100", 114399, 0, 55, 50, 250)
    snapshot = _fan_snapshot(
        20, "curve", "AUTO ON", 50, 0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (
            EndpointSnapshot("one", "One", True, 0, gpus=(gpu,), sample_revision=1),
            EndpointSnapshot("two", "Two", True, 0, gpus=(gpu,), sample_revision=1),
        ),
    )
    app = GraphTwoApp()

    async def exercise() -> None:
        async with app.run_test(size=(79, 30)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 120)
            await pilot.pause()
            dashboard = next(iter(ui.query(".dgx-panel"))).render().plain
            assert "TEMP 50 C" in dashboard and "MEM 111 GiB" in dashboard
            assert "# #" not in dashboard and "###" not in dashboard
            scroll = ui.query_one("#dashboard-scroll", VerticalScroll)
            assert scroll.display and scroll.max_scroll_y == 0

    asyncio.run(exercise())


def test_graph_two_marked_tty8_integer_slots_fit_85x25_and_79x30(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeTTY:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return 8

    class GraphTwoApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI("config.toml", lambda: None, 75, 2, graph_view="graph-2")

    monkeypatch.setattr(ui_module.sys, "stdin", FakeTTY())
    monkeypatch.setattr(ui_module.os, "ttyname", lambda _fd: "/dev/tty8")
    monkeypatch.setenv(ui_module._TTY8_FONT_MARKER, "1")
    gpu = GPUStat("GPU-a", "A100", 114399, 0, 55, 50.4, 250)
    snapshot = _fan_snapshot(
        20, "curve", "AUTO ON", 50.4, 0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (
            EndpointSnapshot("one", "One", True, 0, gpus=(gpu,), sample_revision=1),
            EndpointSnapshot("two", "Two", True, 0, gpus=(gpu,), sample_revision=1),
        ),
    )
    app = GraphTwoApp()

    async def exercise() -> None:
        async with app.run_test(size=(102, 30)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 120)
            await pilot.pause()
            panel = next(iter(ui.query(".dgx-panel"))).render().plain
            assert "TEMP C" in panel and "MEM GiB" in panel and "." not in panel.split("┌ UTIL", 1)[0]
            scroll = ui.query_one("#dashboard-scroll", VerticalScroll)
            assert scroll.display and scroll.max_scroll_y == 0
            assert len(ui.query(Sparkline)) == 2
            await pilot.resize_terminal(85, 25)
            await pilot.pause()
            assert scroll.display and scroll.max_scroll_y == 0
            assert len(ui.query(Sparkline)) == 2
            await pilot.resize_terminal(79, 30)
            await pilot.pause()
            assert scroll.display and scroll.max_scroll_y == 0
            assert len(ui.query(Sparkline)) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize(("graph_view", "size"), [("graph-1", (100, 24)), ("graph-2", (79, 30))])
def test_documented_ansi_dashboard_colors_render_in_terminal(
    graph_view: str, size: tuple[int, int]
) -> None:
    config = load_config(Path("config.example.toml"))

    class AnsiDashboardApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI(
                "config.toml", lambda: None, 75, 2, config.dashboard_colors, graph_view=graph_view
            )

    gpu = GPUStat("GPU-a", "A100", 50, 100, 40, 55, 250)
    snapshot = _fan_snapshot(
        20, "curve", "AUTO ON", 55, 0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (EndpointSnapshot("one", "One", True, 0, gpus=(gpu,), sample_revision=1),),
    )
    app = AnsiDashboardApp()

    async def exercise() -> None:
        async with app.run_test(size=size) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 120)
            await pilot.pause()
            assert ui.query_one("#dashboard-scroll", VerticalScroll).display
            if graph_view == "graph-2":
                assert len(ui.query(Sparkline)) == 1
                await pilot.resize_terminal(120, 30)
                await pilot.pause()
                assert ui.query_one("#dashboard-scroll", VerticalScroll).max_scroll_y == 0
            else:
                assert len(ui.query(Sparkline)) == 0

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        (EndpointSnapshot("one", "One", False, None), "WAITING"),
        (
            EndpointSnapshot(
                "one", "One", True, 1, gpus=(), retrying=True, retry_attempt=1, retry_count=3
            ),
            "RETRYING 1/3",
        ),
        (
            EndpointSnapshot(
                "one", "One", False, 7, stale=True, retrying=True, retry_attempt=2, retry_count=3
            ),
            "RETRYING · STALE",
        ),
        (
            EndpointSnapshot(
                "one", "One", False, 1, error="HTTP 503", retry_count=3, failed_attempts=4
            ),
            "FAILED after 4 attempts",
        ),
        (EndpointSnapshot("one", "One", True, 0), "All configured DGX endpoints are healthy."),
    ],
)
def test_retry_aware_error_banner_states(endpoint: EndpointSnapshot, expected: str) -> None:
    app = _DashboardApp()
    snapshot = _fan_snapshot(
        100,
        "safe",
        "SAFETY OVERRIDE",
        None,
        None,
        (FanReading(None, "NO TACH"), FanReading(None, "NO TACH")),
        (endpoint,),
    )

    async def exercise() -> None:
        async with app.run_test(size=(100, 24)):
            app.query_one(FanAppUI).update_snapshot(snapshot, 10)
            assert expected in app.query_one("#error-banner", Static).render().plain

    asyncio.run(exercise())


def _single_gpu_snapshot(name: str = "One") -> ControlSnapshot:
    gpu = GPUStat(
        "GPU-a",
        "A100",
        memory_used_mib=50,
        memory_total_mib=100,
        utilization_percent=20,
        temperature_celsius=40,
    )
    return _fan_snapshot(
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
        FanAppUI, "call_after_refresh", lambda _self, callback: callbacks.append(callback) or True
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


def test_resize_redraw_retries_when_scheduling_is_refused(monkeypatch) -> None:
    ui = FanAppUI("config.toml", lambda: None, 75, 2)
    ui.snapshot, ui.last_render_time = _single_gpu_snapshot(), 1
    scheduled: list[bool] = []
    callbacks: list[object] = []

    def refusing_then_accepting(_self, callback) -> bool:
        scheduled.append(True)
        if len(scheduled) == 1:
            return False
        callbacks.append(callback)
        return True

    monkeypatch.setattr(FanAppUI, "call_after_refresh", refusing_then_accepting)
    ui.on_resize()
    assert not ui._resize_redraw_pending and len(scheduled) == 1
    ui.on_resize()
    assert ui._resize_redraw_pending and len(scheduled) == 2 and len(callbacks) == 1

    def unavailable(_self, _callback) -> bool:
        raise RuntimeError("closing")

    monkeypatch.setattr(FanAppUI, "call_after_refresh", unavailable)
    ui._resize_redraw_pending = False
    ui.on_resize()
    assert not ui._resize_redraw_pending


def test_layout_convergence_scheduling_is_bounded(monkeypatch) -> None:
    ui = FanAppUI("config.toml", lambda: None, 75, 2)
    ui.snapshot, ui.last_render_time = _single_gpu_snapshot(), 1
    callbacks: list[object] = []
    monkeypatch.setattr(
        FanAppUI, "call_after_refresh", lambda _self, callback: callbacks.append(callback) or True
    )

    for _ in range(3):
        ui._resize_redraw_pending = False
        ui._schedule_layout_check()

    assert len(callbacks) == 2 and ui._layout_convergence_passes == 2


def test_resize_resets_convergence_before_a_pending_second_check(monkeypatch) -> None:
    ui = FanAppUI("config.toml", lambda: None, 75, 2)
    ui.snapshot, ui.last_render_time = _single_gpu_snapshot(), 1
    callbacks: list[object] = []
    monkeypatch.setattr(
        FanAppUI, "call_after_refresh", lambda _self, callback: callbacks.append(callback) or True
    )
    ui._dashboard_layout_signature = (1, 1, 1, 1, 1, 1)
    ui._layout_convergence_passes = 2
    ui._resize_redraw_pending = True
    ui.on_resize()
    assert ui._layout_convergence_passes == 0 and ui._resize_redraw_pending

    monkeypatch.setattr(ui, "query_one", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(ui, "_layout_signature", lambda _scroll: (2, 2, 2, 2, 2, 2))
    monkeypatch.setattr(
        ui, "_render_dashboard", lambda _snapshot, _time: ui._schedule_layout_check()
    )
    ui._converge_dashboard_layout()
    assert len(callbacks) == 1 and ui._layout_convergence_passes == 1 and ui._resize_redraw_pending


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
    first = _fan_snapshot(
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
    second = _fan_snapshot(
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
        return _fan_snapshot(
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
            assert before_resize_renders + 1 <= len(renders) <= before_resize_renders + 2
            final_resize_renders = len(renders)
            ui.update_snapshot(snapshot((a2,)), 5.25)
            assert len(renders) == final_resize_renders
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
    assert isolated[:3] == "..."
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
    assert heights == {"mem": 1, "util": 3, "temp": 5}


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
    snapshot = _fan_snapshot(
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


def test_node_uma_memory_is_endpoint_scoped_and_not_duplicated_for_multiple_gpus() -> None:
    app = _DashboardApp()
    gpus = (
        GPUStat("GPU-a", "Spark", utilization_percent=20, temperature_celsius=40),
        GPUStat("GPU-b", "Spark", utilization_percent=30, temperature_celsius=45),
    )
    endpoint = EndpointSnapshot(
        "spark",
        "Spark",
        True,
        0,
        gpus=gpus,
        sample_revision=1,
        memory_source="node-exporter",
        uma_memory=MemoryStat(50, 100),
        memory_sample_revision=1,
    )
    snapshot = _fan_snapshot(
        20,
        "curve",
        "AUTO ON",
        45,
        0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (endpoint,),
    )

    async def exercise() -> None:
        async with app.run_test(size=(100, 30)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 1)
            await pilot.pause()
            rendered = str(next(iter(ui.query(".dgx-panel"))).render())
            assert rendered.count("┌ UMA MEM ") == 1
            assert "┌ UMA MEM 50/100 MiB (50%)" in rendered
            assert rendered.count("┌ TEMP ") == 2 and rendered.count("┌ UTIL ") == 2
            assert ui.history.points[("spark", "__uma__", "mem")][-1].value == 50

    asyncio.run(exercise())


def test_node_memory_failure_warns_without_marking_dcgm_endpoint_unhealthy() -> None:
    app = _DashboardApp()
    endpoint = EndpointSnapshot(
        "spark",
        "Spark",
        True,
        0,
        gpus=(GPUStat("GPU-a", "Spark", utilization_percent=20, temperature_celsius=40),),
        sample_revision=1,
        memory_source="node-exporter",
        memory_healthy=False,
        memory_error="missing node memory metrics",
        memory_retry_count=1,
        memory_failed_attempts=1,
    )
    snapshot = _fan_snapshot(
        20, "curve", "AUTO ON", 40, 0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")), (endpoint,),
    )

    async def exercise() -> None:
        async with app.run_test(size=(100, 24)):
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 1)
            assert "UMA MEM FAILED" in ui.query_one("#error-banner", Static).render().plain
            assert "┌ UMA MEM N/A" in str(next(iter(ui.query(".dgx-panel"))).render())
            assert endpoint.healthy

    asyncio.run(exercise())


def test_node_memory_failure_does_not_change_dcgm_temperature_fan_stage() -> None:
    config = load_config(Path("config.example.toml"))
    app = DGXFanApp(config)
    dcgm = EndpointSnapshot(
        "dgx-1",
        "DGX-1",
        True,
        0,
        gpus=(GPUStat("GPU-a", "Spark", temperature_celsius=40),),
        memory_source="node-exporter",
    )
    node_failure = NodeMemorySnapshot(
        "dgx-1", False, None, True, "missing node memory metrics", None, 0
    )
    merged = app._merge_memory_snapshot(dcgm, node_failure)
    result = app.controller.update(
        (merged,),
        (FanReading(1000, "RUNNING"), FanReading(1000, "RUNNING")),
        0,
    )
    assert merged.healthy and not merged.memory_healthy
    assert result.state == "AUTO ON" and result.active_stages == (0, 0)


def test_dcgm_failure_remains_fan_unsafe_when_node_memory_is_healthy() -> None:
    config = load_config(Path("config.example.toml"))
    app = DGXFanApp(config)
    dcgm_failure = EndpointSnapshot(
        "dgx-1", "DGX-1", False, 0, error="DCGM unavailable", memory_source="node-exporter"
    )
    node_success = NodeMemorySnapshot("dgx-1", True, 0, memory=MemoryStat(50, 100), sample_revision=1)
    merged = app._merge_memory_snapshot(dcgm_failure, node_success)
    result = app.controller.update(
        (merged,),
        (FanReading(1000, "RUNNING"), FanReading(1000, "RUNNING")),
        0,
    )
    assert not merged.healthy and merged.memory_healthy
    assert result.state == "SAFETY OVERRIDE" and result.duty_percents == (100, 100)


def test_node_memory_banner_distinguishes_retry_fresh_stale_and_waiting() -> None:
    app = _DashboardApp()
    gpu = GPUStat("GPU-a", "Spark", utilization_percent=20, temperature_celsius=40)

    def snapshot(**memory: object) -> ControlSnapshot:
        endpoint = EndpointSnapshot(
            "spark", "Spark", True, 0, gpus=(gpu,), sample_revision=1,
            memory_source="node-exporter", **memory,
        )
        return _fan_snapshot(
            20, "curve", "AUTO ON", 40, 0,
            (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")), (endpoint,),
        )

    async def exercise() -> None:
        async with app.run_test(size=(100, 24)):
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(
                snapshot(
                    uma_memory=MemoryStat(50, 100), memory_healthy=True,
                    memory_retrying=True, memory_retry_attempt=1, memory_retry_count=3,
                    memory_sample_revision=1,
                ),
                1,
            )
            assert "UMA MEM RETRYING 1/3" in ui.query_one("#error-banner", Static).render().plain
            assert "┌ UMA MEM 50/100 MiB (50%)" in str(next(iter(ui.query(".dgx-panel"))).render())
            ui.update_snapshot(snapshot(memory_stale=True, memory_healthy=False), 2)
            assert "UMA MEM STALE" in ui.query_one("#error-banner", Static).render().plain
            ui.update_snapshot(snapshot(memory_stale=True, memory_healthy=False, memory_error="awaiting first sample"), 3)
            assert "UMA MEM WAITING" in ui.query_one("#error-banner", Static).render().plain

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
    snapshot = _fan_snapshot(
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
    snapshot = _fan_snapshot(
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
    snapshot = _fan_snapshot(
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
    snapshot = _fan_snapshot(
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
    def chart_data_cells(rendered: str) -> list[str]:
        """Extract only the chart plot cells, excluding labels, values, and axes."""
        cells: list[str] = []
        for line in rendered.splitlines():
            for interior in line.split("│")[1:-1]:
                if len(interior) < 5 or any(label in interior[5:] for label in ("120s", "60s", "30s", "now")):
                    continue
                if interior[:5] == "     " or interior[:5].strip().isdigit():
                    cells.append(interior[5:])
        return cells

    app = _DashboardApp()
    gpu = GPUStat(
        "GPU-a",
        "A100",
        memory_used_mib=50,
        memory_total_mib=100,
        utilization_percent=20,
        temperature_celsius=40,
    )
    snapshot = _fan_snapshot(
        20,
        "curve",
        "AUTO ON",
        40,
        0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (
            EndpointSnapshot("one", "DGX-1#=+*", True, 0, gpus=(gpu,), sample_revision=1),
            EndpointSnapshot("two", "DGX-2▁", True, 0, gpus=(gpu,), sample_revision=1),
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
            rendered_panels = [panel.render().plain for panel in ui.query(".dgx-panel")]
            assert all("DGX-1#=+*" in rendered or "DGX-2▁" in rendered for rendered in rendered_panels)
            plot_cells = [cell for rendered in rendered_panels for cell in chart_data_cells(rendered)]
            assert plot_cells
            assert all(set(cell) <= {" ", ".", ":"} for cell in plot_cells)
            assert any(set(".:").intersection(cell) for cell in plot_cells)
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


def test_scrollbar_width_converges_after_resize_without_wrapping_rows() -> None:
    app = _DashboardApp()
    gpu = GPUStat(
        "GPU-a",
        "A100",
        memory_used_mib=50,
        memory_total_mib=100,
        utilization_percent=20,
        temperature_celsius=40,
    )
    fit = _fan_snapshot(
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
        async with app.run_test(size=(120, 60)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(fit, 1)
            await pilot.pause()
            history_count = len(ui.history.points[("one", "GPU-a", "util")])
            for width, height in ((98, 32), (85, 25)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                await pilot.pause()
                scroll = ui.query_one("#dashboard-scroll", VerticalScroll)
                usable = scroll.scrollable_content_region.width
                assert usable == scroll.size.width and scroll.max_scroll_y == 0
                assert all(
                    cell_len(line) <= usable
                    for panel in ui.query(".dgx-panel")
                    for line in panel.render().plain.splitlines()
                )
                assert history_count == len(ui.history.points[("one", "GPU-a", "util")])

    asyncio.run(exercise())


def test_true_overflow_uses_reduced_scrollable_content_width() -> None:
    app = _DashboardApp()
    gpus = tuple(
        GPUStat(
            f"GPU-{index}",
            "A100",
            memory_used_mib=50,
            memory_total_mib=100,
            utilization_percent=20,
            temperature_celsius=40,
        )
        for index in range(5)
    )
    snapshot = _fan_snapshot(
        20,
        "curve",
        "AUTO ON",
        40,
        0,
        (FanReading(1, "RUNNING"), FanReading(1, "RUNNING")),
        (EndpointSnapshot("one", "One", True, 0, gpus=gpus, sample_revision=1),),
    )

    async def exercise() -> None:
        async with app.run_test(size=(98, 32)) as pilot:
            ui = app.query_one(FanAppUI)
            ui.update_snapshot(snapshot, 1)
            await pilot.pause()
            await pilot.pause()
            scroll = ui.query_one("#dashboard-scroll", VerticalScroll)
            usable = scroll.scrollable_content_region.width
            assert scroll.max_scroll_y > 0 and usable < scroll.size.width
            assert all(
                cell_len(line) <= usable
                for panel in ui.query(".dgx-panel")
                for line in panel.render().plain.splitlines()
            )

    asyncio.run(exercise())
