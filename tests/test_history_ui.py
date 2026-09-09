from __future__ import annotations

import asyncio
from time import time

from textual import events
from textual.app import App, ComposeResult
from textual.widgets import Button, TabbedContent

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
            assert "Memory mean %" in app.query_one("#history-memory-chart").render().plain
            assert "UTIL max %" in app.query_one("#history-utilization-chart").render().plain
            assert "Temp max C" in app.query_one("#history-temperature-chart").render().plain
            assert "Power max W" in app.query_one("#history-power-chart").render().plain
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
            midpoint = panel.end - ZOOM_SPANS[before] / 2
            app.query_one("#history-zoom-in", Button).press()
            await pilot.pause()
            assert panel.span_index == min(before + 1, len(ZOOM_SPANS) - 1)
            assert not panel.live
            assert abs((panel.end - ZOOM_SPANS[panel.span_index] / 2) - midpoint) < 0.01
            panel.action_pan_left()
            assert not panel.live
            app.query_one("#history-now", Button).press()
            await pilot.pause()
            assert panel.live and abs(panel.end - time()) < 2
            await pilot.resize_terminal(79, 24)
            await pilot.pause()
            assert all(chart.region.width > 0 for chart in panel.query("HistoryChart"))
            release.set()

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
            assert temperature.maximum == 120 and "max 120 C" in temperature.render().plain
            assert power.maximum == 1500 and "max 1500 W" in power.render().plain
            panel.deactivate()

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
            panel.deactivate()

    asyncio.run(exercise())
