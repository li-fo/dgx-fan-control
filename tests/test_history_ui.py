from __future__ import annotations

import asyncio
from time import time

from rich.cells import cell_len
from textual import events
from textual.app import App, ComposeResult
from textual.widgets import Button, Footer, TabbedContent

from dgx_fan.config import DashboardColors, EndpointConfig
from dgx_fan.history_ui import ZOOM_SPANS, HistoryChart, HistoryPanel
from dgx_fan.ui import FanAppUI


def _endpoints() -> tuple[EndpointConfig, EndpointConfig]:
    return (
        EndpointConfig("one", "DGX-01 [safe]", "http://one:9400/metrics"),
        EndpointConfig("two", "DGX-02", "http://two:9400/metrics"),
    )


def _result(endpoint_id: str, start: float, end: float, width: int) -> dict[str, object]:
    return {
        "endpoint_id": endpoint_id,
        "start": start,
        "end": end,
        "now": end,
        "retention_start": end - 8 * 86400,
        "status": "History ready",
        "series": {
            "memory": [25.0] * width,
            "utilization": [50.0] * width,
            "temperature": [60.0] * width,
            "power": [120.0] * width,
        },
    }


def test_history_tab_queries_only_when_active_and_renders_four_charts_at_79_columns() -> None:
    calls: list[tuple[str, float, float, int]] = []

    async def query(endpoint_id: str, start: float, end: float, width: int) -> dict[str, object]:
        calls.append((endpoint_id, start, end, width))
        return _result(endpoint_id, start, end, width)

    class HistoryApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI(
                "config.toml",
                lambda: None,
                75,
                2,
                DashboardColors("ansi_yellow", "ansi_cyan", "ansi_red"),
                history_query=query,
                history_endpoints=_endpoints(),
            )

    app = HistoryApp()

    async def exercise() -> None:
        async with app.run_test(size=(79, 24)) as pilot:
            ui = app.query_one(FanAppUI)
            panel = app.query_one(HistoryPanel)
            assert app.query_one("#history") and not calls
            app.query_one(TabbedContent).active = "history"
            await pilot.pause()
            await pilot.pause()
            assert calls and calls[-1][0] == "one"
            assert 1 <= calls[-1][3] <= 1024
            assert app.query_one("#history-memory-chart", HistoryChart).border_title == "Memory mean %"
            assert app.query_one("#history-utilization-chart", HistoryChart).border_title == "UTIL max %"
            assert app.query_one("#history-temperature-chart", HistoryChart).border_title == "Temp max C"
            assert app.query_one("#history-power-chart", HistoryChart).border_title == "Power max · fixed 240 W"
            assert "[safe]" in app.query_one("#history-endpoint-0", Button).label.plain
            assert panel._mounted_active
            app.query_one(TabbedContent).active = "dashboard"
            await pilot.pause()
            assert not panel._mounted_active
            # Preserve existing FanAppUI history through the new optional interface.
            assert ui.history.collection_interval_seconds == 2

    asyncio.run(exercise())


def test_history_endpoint_zoom_pan_now_resize_and_pending_request_cancellation() -> None:
    calls: list[str] = []
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def query(endpoint_id: str, start: float, end: float, width: int) -> dict[str, object]:
        calls.append(endpoint_id)
        if len(calls) == 1:
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                # A slow external client can complete after cancellation. The
                # panel must reject that stale generation rather than paint it.
                await release.wait()
                stale = _result(endpoint_id, start, end, width)
                stale["series"] = {
                    metric: [1.0] * width for metric in ("memory", "utilization", "temperature", "power")
                }
                return stale
        return _result(endpoint_id, start, end, width)

    class HistoryOnlyApp(App[None]):
        def compose(self) -> ComposeResult:
            yield HistoryPanel(_endpoints(), query, interval_seconds=2)

    app = HistoryOnlyApp()

    async def exercise() -> None:
        async with app.run_test(size=(128, 37)) as pilot:
            panel = app.query_one(HistoryPanel)
            # Mounted alone is deliberately not enough: a parent tab must activate it.
            assert not calls
            panel.activate()
            await pilot.pause()
            assert calls == ["one"]
            panel.deactivate()
            await pilot.pause()
            assert cancelled.is_set()
            panel.activate()
            await pilot.pause()
            app.query_one("#history-endpoint-1", Button).press()
            await asyncio.sleep(0.1)
            await pilot.pause()
            assert calls[-1] == "two"
            release.set()
            await pilot.pause()
            chart = app.query_one("#history-memory-chart", HistoryChart)
            assert chart.values and all(value == 25.0 for value in chart.values)
            before = panel.span_index
            app.query_one("#history-zoom-in", Button).press()
            await pilot.pause()
            assert panel.span_index == min(before + 1, len(ZOOM_SPANS) - 1)
            assert panel.live and abs(panel.end - time()) < 2
            panel.action_pan_left()
            assert not panel.live
            panel.deactivate()
            panel.activate()
            await pilot.pause()
            assert panel.live and abs(panel.end - time()) < 2
            await pilot.resize_terminal(79, 24)
            await pilot.pause()
            assert all(chart.region.width > 0 for chart in panel.query("HistoryChart"))
            release.set()

    asyncio.run(exercise())


def test_history_loading_indicator_tracks_current_request_without_status_churn() -> None:
    first_stale_release = asyncio.Event()
    second_release = asyncio.Event()
    calls: list[str] = []

    async def query(endpoint_id: str, start: float, end: float, width: int) -> dict[str, object]:
        calls.append(endpoint_id)
        if endpoint_id == "one":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Emulate a client that returns after cancellation. Its stale
                # completion must not clear the newer endpoint's indicator.
                await first_stale_release.wait()
                return _result(endpoint_id, start, end, width)
        await second_release.wait()
        return _result(endpoint_id, start, end, width)

    class HistoryOnlyApp(App[None]):
        def compose(self) -> ComposeResult:
            yield HistoryPanel(_endpoints(), query)

    app = HistoryOnlyApp()

    async def exercise() -> None:
        async with app.run_test(size=(128, 37)) as pilot:
            panel = app.query_one(HistoryPanel)
            loading = app.query_one("#history-loading")
            status = app.query_one("#history-status")
            panel.activate()
            await pilot.pause()
            assert calls == ["one"] and loading.render().plain == "[*]"
            assert "Loading history" not in status.render().plain
            app.query_one("#history-endpoint-1", Button).press()
            await asyncio.sleep(0.1)
            await pilot.pause()
            assert calls[-1] == "two" and loading.render().plain == "[*]"
            first_stale_release.set()
            await pilot.pause()
            assert loading.render().plain == "[*]"
            panel.deactivate()
            assert loading.render().plain == "   "
            panel.activate()
            await pilot.pause()
            assert loading.render().plain == "[*]"
            second_release.set()
            await pilot.pause()
            assert loading.render().plain == "   "

    asyncio.run(exercise())


def test_history_live_timer_stops_inactive_and_drag_uses_screen_coordinates() -> None:
    calls = 0

    async def query(endpoint_id: str, start: float, end: float, width: int) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return _result(endpoint_id, start, end, width)

    class HistoryOnlyApp(App[None]):
        def compose(self) -> ComposeResult:
            yield HistoryPanel(_endpoints(), query, interval_seconds=0.1)

    app = HistoryOnlyApp()

    async def exercise() -> None:
        async with app.run_test(size=(128, 37)) as pilot:
            panel = app.query_one(HistoryPanel)
            panel.activate()
            await asyncio.sleep(0.24)
            await pilot.pause()
            assert calls >= 2
            timeline = app.query_one("#history-timeline")
            before = panel.end
            panel.on_mouse_down(
                events.MouseDown(timeline, 1, 1, 0, 0, 1, False, False, False, screen_x=30)
            )
            panel.on_mouse_move(
                events.MouseMove(timeline, 1, 1, 0, 0, 1, False, False, False, screen_x=50)
            )
            panel.on_mouse_up(
                events.MouseUp(timeline, 1, 1, 0, 0, 1, False, False, False, screen_x=50)
            )
            assert panel._drag_x is None and not panel.live and panel.end < before
            await asyncio.sleep(0.1)
            await pilot.pause()
            baseline = calls
            panel.deactivate()
            await asyncio.sleep(0.2)
            assert calls == baseline

    asyncio.run(exercise())


def test_history_live_timer_does_not_starve_slow_query() -> None:
    calls = 0
    release = asyncio.Event()

    async def query(endpoint_id: str, start: float, end: float, width: int) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            await release.wait()
        return _result(endpoint_id, start, end, width)

    class HistoryOnlyApp(App[None]):
        def compose(self) -> ComposeResult:
            yield HistoryPanel(_endpoints(), query, interval_seconds=0.1)

    app = HistoryOnlyApp()

    async def exercise() -> None:
        async with app.run_test(size=(128, 37)) as pilot:
            panel = app.query_one(HistoryPanel)
            panel.activate()
            await asyncio.sleep(0.25)  # More than two live refresh intervals.
            assert calls == 1
            release.set()
            await pilot.pause()
            chart = app.query_one("#history-memory-chart", HistoryChart)
            assert chart.values and all(value == 25.0 for value in chart.values)
            panel.deactivate()

    asyncio.run(exercise())


def test_history_uses_safe_ordinal_dom_ids_and_preserves_domain_endpoint_id() -> None:
    endpoint = EndpointConfig("dgx: one/slash.dot", "DGX [one]", "http://one:9400/metrics")
    calls: list[str] = []

    async def query(endpoint_id: str, start: float, end: float, width: int) -> dict[str, object]:
        calls.append(endpoint_id)
        return _result(endpoint_id, start, end, width)

    class HistoryApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI(
                "config.toml", lambda: None, 75, 2, history_query=query, history_endpoints=(endpoint,)
            )

    app = HistoryApp()

    async def exercise() -> None:
        async with app.run_test(size=(79, 24)) as pilot:
            app.query_one(TabbedContent).active = "history"
            await pilot.pause()
            await pilot.pause()
            assert calls and all(value == endpoint.id for value in calls)
            assert "[one]" in app.query_one("#history-endpoint-0", Button).label.plain

    asyncio.run(exercise())


def test_history_status_and_dynamic_temperature_power_maximum_are_visible() -> None:
    async def warning_query(endpoint_id: str, start: float, end: float, width: int) -> dict[str, object]:
        result = _result(endpoint_id, start, end, width)
        result["status"] = "Storage warning: database busy"
        result["series"] = {
            "memory": [25.0] * width,
            "utilization": [50.0] * width,
            "temperature": [120.0] * width,
            "power": [1500.0] * width,
        }
        return result

    class HistoryOnlyApp(App[None]):
        def compose(self) -> ComposeResult:
            yield HistoryPanel(_endpoints(), warning_query)

    app = HistoryOnlyApp()

    async def exercise() -> None:
        async with app.run_test(size=(128, 37)) as pilot:
            panel = app.query_one(HistoryPanel)
            panel.activate()
            await pilot.pause()
            assert "Storage warning: database busy" in app.query_one("#history-status").render().plain
            temperature = app.query_one("#history-temperature-chart", HistoryChart)
            power = app.query_one("#history-power-chart", HistoryChart)
            assert temperature.maximum == 120
            assert power.maximum == 240 and power.values == [1500.0] * len(power.values)
            panel.deactivate()

    asyncio.run(exercise())


def test_history_compact_layout_keeps_all_charts_visible_after_real_app_resizes() -> None:
    widths: list[int] = []

    async def query(endpoint_id: str, start: float, end: float, width: int) -> dict[str, object]:
        widths.append(width)
        return _result(endpoint_id, start, end, width)

    class HistoryApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI(
                "config.toml", lambda: None, 75, 2, history_query=query, history_endpoints=_endpoints()
            )

    app = HistoryApp()

    async def exercise() -> None:
        async with app.run_test(size=(79, 24)) as pilot:
            app.query_one(TabbedContent).active = "history"
            await pilot.pause()
            panel = app.query_one(HistoryPanel)
            timeline = app.query_one("#history-timeline")
            plots = app.query_one("#history-plots")
            footer = app.query_one(Footer)
            assert len(panel.query("VerticalScroll")) == 0
            assert len(app.query("#history-now")) == 0
            controls = (
                app.query_one("#history-endpoint-0", Button),
                app.query_one("#history-endpoint-1", Button),
                app.query_one("#history-zoom-out", Button),
                app.query_one("#history-zoom-in", Button),
            )
            loading = app.query_one("#history-loading")
            assert all(button.region.height == 1 and button.region.width > 0 for button in controls)
            assert loading.region.width == loading.content_size.width == 3 and loading.region.height == 1
            before = panel.span_index
            controls[-1].press()
            await pilot.pause()
            assert panel.span_index == min(before + 1, len(ZOOM_SPANS) - 1)
            for size in ((79, 24), (128, 37), (79, 24), (128, 37)):
                await pilot.resize_terminal(*size)
                await asyncio.sleep(0.1)
                await pilot.pause()
                charts = list(panel.query(HistoryChart))
                assert len(charts) == 4
                assert all(chart.region.height >= 3 for chart in charts)
                assert charts[0].region.y >= plots.region.y
                assert all(chart.region.bottom <= timeline.region.y for chart in charts)
                assert charts[-1].region.bottom <= timeline.region.y
                assert timeline.region.bottom <= footer.region.y
                assert timeline.content_size.height >= 1
                assert all(chart.border_title for chart in charts)
                assert all(_has_complete_border(chart) for chart in charts)
                assert plots.region.height >= sum(chart.region.height for chart in charts)
                assert panel.scroll_y == 0 and plots.scroll_y == 0
                assert all(not any(character.isdigit() for character in chart.render().plain) for chart in charts)
                assert all(
                    len(chart.render().plain.splitlines()) <= chart.content_size.height
                    and all(cell_len(line) <= chart.content_size.width for line in chart.render().plain.splitlines())
                    for chart in charts
                )
                assert widths[-1] == app.query_one("#history-memory-chart", HistoryChart).content_size.width

    asyncio.run(exercise())


def test_history_controls_stay_plain_in_native_ansi_focus_hover_and_active_states() -> None:
    queried: list[str] = []

    async def query(endpoint_id: str, start: float, end: float, width: int) -> dict[str, object]:
        queried.append(endpoint_id)
        return _result(endpoint_id, start, end, width)

    class HistoryApp(App[None]):
        def compose(self) -> ComposeResult:
            yield FanAppUI(
                "config.toml", lambda: None, 75, 2, history_query=query, history_endpoints=_endpoints()
            )

    app = HistoryApp(ansi_color=True)
    app.theme = "ansi-dark"

    async def exercise() -> None:
        async with app.run_test(size=(79, 24)) as pilot:
            app.query_one(TabbedContent).active = "history"
            await pilot.pause()
            endpoint_one = app.query_one("#history-endpoint-0", Button)
            endpoint_two = app.query_one("#history-endpoint-1", Button)
            zoom_out = app.query_one("#history-zoom-out", Button)
            zoom_in = app.query_one("#history-zoom-in", Button)
            controls = (endpoint_one, endpoint_two, zoom_out, zoom_in)
            assert all(button.region.height == button.content_size.height == 1 for button in controls)
            assert all(_has_no_border(button) for button in controls)
            assert endpoint_one.has_class("-primary") and "reverse" in str(endpoint_one.styles.text_style)
            endpoint_two.focus()
            await pilot.pause()
            await pilot.hover("#history-endpoint-1")
            assert endpoint_two.has_focus and _has_no_border(endpoint_two)
            assert "reverse" in str(endpoint_two.styles.text_style)
            before = app.query_one(HistoryPanel).span_index
            assert await pilot.click("#history-endpoint-1")
            await asyncio.sleep(0.1)
            assert queried[-1] == "two" and endpoint_two.has_class("-primary")
            assert await pilot.click("#history-zoom-in")
            await pilot.pause()
            assert app.query_one(HistoryPanel).span_index == min(before + 1, len(ZOOM_SPANS) - 1)
            assert all(_has_no_border(button) for button in controls)
            assert all("▁" not in button.render().plain and "▄" not in button.render().plain for button in controls)

    asyncio.run(exercise())


def _has_complete_border(chart: HistoryChart) -> bool:
    top, right, bottom, left = chart.styles.border
    return all(edge[0] for edge in (top, right, bottom, left))


def _has_no_border(button: Button) -> bool:
    return not any(edge[0] for edge in button.styles.border)


def test_power_chart_uses_fixed_240_watt_display_proportions() -> None:
    class ChartApp(App[None]):
        def compose(self) -> ComposeResult:
            yield HistoryChart("power", "Power max · fixed 240 W", 240, "ansi_green", " W")

    app = ChartApp()

    async def exercise() -> None:
        async with app.run_test(size=(80, 10)) as pilot:
            chart = app.query_one(HistoryChart)
            chart.update_series([120.0], 1, 2, "ready")
            half_rows = sum(chart._glyph(120.0, row, 4) != " " for row in range(4))
            full_rows = sum(chart._glyph(240.0, row, 4) != " " for row in range(4))
            assert chart.maximum == 240 and chart.values == [120.0]
            assert half_rows == 2 and full_rows == 4
            await pilot.pause()

    asyncio.run(exercise())


def test_history_query_exception_is_visible_in_panel_status() -> None:
    async def failing_query(endpoint_id: str, start: float, end: float, width: int) -> dict[str, object]:
        raise RuntimeError("database corrupt")

    class HistoryOnlyApp(App[None]):
        def compose(self) -> ComposeResult:
            yield HistoryPanel(_endpoints(), failing_query)

    app = HistoryOnlyApp()

    async def exercise() -> None:
        async with app.run_test(size=(79, 24)) as pilot:
            panel = app.query_one(HistoryPanel)
            panel.activate()
            await pilot.pause()
            assert "History unavailable: database corrupt" in app.query_one("#history-status").render().plain
            assert app.query_one("#history-loading").render().plain == "   "
            panel.deactivate()

    asyncio.run(exercise())
