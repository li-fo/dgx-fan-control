import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, Static, TabbedContent

from dgx_fan.app import DGXFanApp
from dgx_fan.config import DashboardColors, EndpointConfig, load_config
from dgx_fan.models import ControlSnapshot, EndpointSnapshot, FanReading, GPUStat
from dgx_fan.ui import DashboardHistory, FanAppUI, FanGauge, HistoryPoint


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
