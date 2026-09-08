import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from rich.color import Color
from textual.app import App, ComposeResult
from textual.content import Content
from textual.screen import Screen
from textual.widgets import Button, Input, Select, Static, Tab, TabbedContent, TabPane, Tabs

from dgx_fan.config import DashboardColors, load_config
from dgx_fan.settings_ui import ANSI_COLOR_PRESETS, SettingsScreen


def _initial(colors: dict[str, str | None] | None = None) -> dict[str, Any]:
    return {
        "source_id": "controller-1",
        "revision": 7,
        "settings": {
            "collection": {
                "interval_seconds": 2.0,
                "timeout_seconds": 1.0,
                "stale_after_seconds": 6.0,
                "retry_count": 2,
                "retry_delay_seconds": 3.0,
            },
            "control": {
                "fan_endpoint_ids": ["one", "two"],
                "enabled_at_startup": True,
                "max_speed_percent": 90,
                "fallback_speed_percent": 100,
                "hysteresis_celsius": 2.0,
                "emergency_temperature_celsius": 75.0,
                "recovery_seconds": 10.0,
                "stages": [
                    {"max_temperature_celsius": 45.0, "speed_percent": 20},
                    {"max_temperature_celsius": 55.0, "speed_percent": 50},
                    {"max_temperature_celsius": 70.0, "speed_percent": 80},
                    {"speed_percent": 100},
                ],
            },
            "hardware": {
                "startup_boost_seconds": 1.0,
                "stall_timeout_seconds": 5.0,
                "shutdown_mode": "full",
            },
            "dashboard": {
                "colors": colors or {"memory": "blue", "utilization": None, "temperature": "red"}
            },
        },
        "endpoints": [{"id": "one", "name": "DGX One"}, {"id": "two", "name": "DGX Two"}],
    }


class _ModalApp(App[None]):
    def __init__(self, screen: SettingsScreen, result: list[bool]) -> None:
        super().__init__()
        self.editor, self.result = screen, result

    def compose(self) -> ComposeResult:
        yield Static("base")

    def on_mount(self) -> None:
        self.push_screen(self.editor, lambda value: self.result.append(bool(value)))


def _editor(app: App[None]) -> Screen[bool]:
    return cast(Screen[bool], app.screen)


def test_settings_cancel_never_calls_save_at_small_terminal_size() -> None:
    calls: list[dict[str, Any]] = []

    async def save(*args: Any) -> dict[str, Any]:
        calls.append(args[0])
        return _initial()

    result: list[bool] = []
    app = _ModalApp(SettingsScreen(_initial(), save), result)

    async def exercise() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            screen = _editor(app)
            tabs = screen.query_one("#settings-tabs", TabbedContent)
            tabs.active = "settings-tab-colors"
            screen.query_one("#setting-color-memory", Select).value = "bright_cyan"
            screen.query_one("#setting-cancel", Button).press()
            await pilot.pause()
            assert result == [False] and calls == []

    asyncio.run(exercise())


def test_settings_save_sends_typed_allowlisted_patch() -> None:
    calls: list[tuple[dict[str, Any], int, str]] = []

    async def save(patch: dict[str, Any], revision: int, source_id: str) -> dict[str, Any]:
        calls.append((patch, revision, source_id))
        return _initial()

    result: list[bool] = []
    app = _ModalApp(SettingsScreen(_initial(), save), result)

    async def exercise() -> None:
        async with app.run_test(size=(128, 37)) as pilot:
            await pilot.pause()
            screen = _editor(app)
            screen.query_one("#setting-collection-retry_count", Input).value = "4"
            screen.query_one("#setting-stage-4-speed", Input).value = "95"
            screen.query_one("#setting-control-max_speed_percent", Input).value = "88"
            screen.query_one("#setting-control-fan-mode", Select).value = "linked"
            screen.query_one("#setting-hardware-shutdown-mode", Select).value = "off"
            screen.query_one("#setting-color-memory", Select).value = "bright_blue"
            screen.query_one("#setting-color-utilization", Select).value = ""
            screen.query_one("#setting-save", Button).press()
            await pilot.pause()
            assert result == [True]

    asyncio.run(exercise())
    patch, revision, source_id = calls[0]
    assert (revision, source_id) == (7, "controller-1")
    assert patch["collection"]["retry_count"] == 4
    assert isinstance(patch["collection"]["interval_seconds"], float)
    assert patch["control"]["stages"][3] == {"speed_percent": 95}
    assert patch["control"]["fan_mode"] == "linked"
    assert patch["control"]["max_speed_percent"] == 88
    assert patch["hardware"]["shutdown_mode"] == "off"
    assert patch["dashboard"]["colors"]["memory"] == "bright_blue"
    assert "utilization" not in patch["dashboard"]["colors"]
    assert set(patch["hardware"]) == {
        "startup_boost_seconds",
        "stall_timeout_seconds",
        "shutdown_mode",
    }


def test_settings_save_failure_retains_draft_and_reenables_submit() -> None:
    calls = 0

    async def save(*args: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise RuntimeError("settings changed; reload before saving")

    app = _ModalApp(SettingsScreen(_initial(), save), [])

    async def exercise() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            screen = _editor(app)
            field = screen.query_one("#setting-control-max_speed_percent", Input)
            field.value = "81"
            screen.query_one("#setting-save", Button).press()
            await pilot.pause()
            assert field.value == "81" and not screen.query_one("#setting-save", Button).disabled
            error = screen.query_one("#settings-error", Static)
            assert error.region.height > 0
            assert "reload before saving" in str(error.render())

    asyncio.run(exercise())
    assert calls == 1


def test_settings_ignores_duplicate_save_while_request_is_pending() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def save(*args: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        started.set()
        await asyncio.wait_for(release.wait(), 2)
        return _initial()

    result: list[bool] = []
    app = _ModalApp(SettingsScreen(_initial(), save), result)

    async def exercise() -> None:
        async with app.run_test(size=(128, 37)) as pilot:
            await pilot.pause()
            save_button = _editor(app).query_one("#setting-save", Button)
            save_button.press()
            await asyncio.wait_for(started.wait(), 2)
            save_button.press()
            assert calls == 1 and save_button.disabled
            release.set()
            await pilot.pause()
            assert result == [True]

    asyncio.run(exercise())


@pytest.mark.parametrize("size", [(80, 24), (128, 37)])
def test_settings_tabs_switch_by_mouse_and_keyboard_with_fixed_actions(
    size: tuple[int, int],
) -> None:
    async def save(*_args: Any) -> dict[str, Any]:
        return _initial()

    app = _ModalApp(SettingsScreen(_initial(), save), [])

    async def exercise() -> None:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            screen = _editor(app)
            content = screen.query_one("#settings-tabs", TabbedContent)
            actions = screen.query_one("#settings-actions")
            panes = list(screen.query(TabPane))
            assert str(screen.query_one("#settings-title", Static).render()) == "Setting"
            assert [tab.label_text for tab in screen.query(Tab)] == [
                "Collection",
                "Fan Speed",
                "Fan Control",
                "Hardware",
                "Colors",
            ]
            assert content.active == "settings-tab-collection"
            assert sum(pane.display for pane in panes) == 1
            assert actions.region.bottom <= app.size.height

            speed_tab = next(
                tab for tab in screen.query(Tab) if tab.id == "--content-tab-settings-tab-speed"
            )
            assert await pilot.click(speed_tab, offset=(2, 0))
            await pilot.pause()
            assert content.active == "settings-tab-speed"
            assert sum(pane.display for pane in panes) == 1
            assert {field.id for field in screen.query_one("#settings-tab-speed").query(Input)} == {
                "setting-stage-1-temperature",
                "setting-stage-1-speed",
                "setting-stage-2-temperature",
                "setting-stage-2-speed",
                "setting-stage-3-temperature",
                "setting-stage-3-speed",
                "setting-stage-4-speed",
            }
            threshold = screen.query_one("#setting-stage-1-temperature", Input)
            threshold.value = "46"
            assert threshold.region.height > 0
            assert screen.query_one("#setting-stage-4-speed", Input).region.height > 0
            rows = [screen.query_one(f"#setting-stage-row-{index}") for index in range(1, 5)]
            for child_index in range(1, 5):
                assert (
                    len(
                        {
                            (
                                row.children[child_index].region.x,
                                row.children[child_index].region.width,
                            )
                            for row in rows
                        }
                    )
                    == 1
                )
            placeholder = screen.query_one("#setting-stage-4-threshold", Static)
            assert str(placeholder.render()) == "Above stage 3"
            assert (placeholder.region.x, placeholder.region.width) == (
                rows[0].children[2].region.x,
                rows[0].children[2].region.width,
            )

            tabs = screen.query_one(Tabs)
            tabs.focus()
            await pilot.press("right")
            await pilot.pause()
            assert content.active == "settings-tab-control"
            await pilot.press("left")
            await pilot.pause()
            assert content.active == "settings-tab-speed"
            assert threshold.value == "46"
            assert screen.query_one("#setting-save", Button).region.bottom <= app.size.height

    asyncio.run(exercise())


def test_color_select_uses_mouse_open_and_keyboard_selection() -> None:
    async def save(*_args: Any) -> dict[str, Any]:
        return _initial()

    app = _ModalApp(SettingsScreen(_initial(), save), [])

    async def exercise() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            screen = _editor(app)
            color_tab = next(
                tab for tab in screen.query(Tab) if tab.id == "--content-tab-settings-tab-colors"
            )
            assert await pilot.click(color_tab, offset=(2, 0))
            await pilot.pause()
            assert screen.query_one("#settings-tabs", TabbedContent).active == "settings-tab-colors"
            select = screen.query_one("#setting-color-memory", Select)
            assert select.region.width > 0 and select.region.height > 0
            await pilot.click(
                select,
                offset=(select.region.width // 2, select.region.height // 2),
            )
            await pilot.pause()
            assert select.expanded
            await pilot.press("end", "enter")
            await pilot.pause()
            assert select.value == "bright_white"
            assert not select.expanded
            assert len(screen.query_one("#settings-tab-colors", TabPane).query(Input)) == 0

    asyncio.run(exercise())


def test_color_presets_have_named_rich_swatches_and_canonical_values() -> None:
    options, selected = SettingsScreen._color_options(None)

    assert selected == ""
    assert [value for _label, value in options] == ["", *ANSI_COLOR_PRESETS]
    assert len(ANSI_COLOR_PRESETS) == 16
    for label, value in options[1:]:
        assert label.plain.strip() == value.replace("_", " ").title()
        assert "\n" not in label.plain
        assert any(span.style == f"on {value}" for span in label.spans)
        assert Color.parse(value).name == value


@pytest.mark.parametrize("size", [(80, 24), (128, 37)])
def test_collapsed_color_prompts_are_single_line_visible_swatches(
    size: tuple[int, int],
) -> None:
    async def save(*_args: Any) -> dict[str, Any]:
        return _initial()

    app = _ModalApp(SettingsScreen(_initial(), save), [])

    async def exercise() -> None:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            screen = _editor(app)
            screen.query_one("#settings-tabs", TabbedContent).active = "settings-tab-colors"
            select = screen.query_one("#setting-color-memory", Select)
            current = select.query_one("SelectCurrent")
            current_label = current.query_one("#label", Static)
            for color_index, preset in enumerate(ANSI_COLOR_PRESETS):
                select.value = preset
                await pilot.pause()
                prompt = current_label.render()
                assert isinstance(prompt, Content)
                assert "\n" not in prompt.plain
                assert prompt.plain.strip() == preset.replace("_", " ").title()
                assert prompt.cell_length <= current_label.content_region.width
                assert len(prompt.spans) == 1
                style = prompt.spans[0].style
                assert not isinstance(style, str)
                assert style.background is not None
                assert style.background.ansi == color_index

    asyncio.run(exercise())


def test_existing_custom_colors_are_preserved_until_explicitly_replaced() -> None:
    initial = _initial(
        {
            "memory": "#123456",
            "utilization": "deep_sky_blue3",
            "temperature": "red",
        }
    )
    for current in ("#123456", "deep_sky_blue3"):
        options, selected = SettingsScreen._color_options(current)
        assert selected == current
        assert options[-1][0].plain == f"Current custom: {current}"
        assert options[-1][1] == current
    calls: list[dict[str, Any]] = []

    async def save(patch: dict[str, Any], *_args: Any) -> dict[str, Any]:
        calls.append(patch)
        return initial

    app = _ModalApp(SettingsScreen(initial, save), [])

    async def exercise() -> None:
        async with app.run_test(size=(128, 37)) as pilot:
            await pilot.pause()
            screen = _editor(app)
            assert screen.query_one("#setting-color-memory", Select).value == "#123456"
            assert screen.query_one("#setting-color-utilization", Select).value == "deep_sky_blue3"
            screen.query_one("#setting-collection-retry_count", Input).value = "3"
            screen.query_one("#setting-save", Button).press()
            await pilot.pause()

    asyncio.run(exercise())
    assert calls[0]["dashboard"]["colors"] == {
        "memory": "#123456",
        "utilization": "deep_sky_blue3",
        "temperature": "red",
    }


def test_hidden_tab_validation_error_is_visible_without_losing_draft() -> None:
    calls = 0

    async def save(*_args: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _initial()

    app = _ModalApp(SettingsScreen(_initial(), save), [])

    async def exercise() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            screen = _editor(app)
            content = screen.query_one("#settings-tabs", TabbedContent)
            hidden = screen.query_one("#setting-control-max_speed_percent", Input)
            hidden.value = "not-a-number"
            screen.query_one("#setting-save", Button).press()
            await pilot.pause()
            error = screen.query_one("#settings-error", Static)
            assert content.active == "settings-tab-collection"
            assert hidden.value == "not-a-number"
            assert "Normal maximum must be a whole number" in str(error.render())
            assert error.region.height > 0

    asyncio.run(exercise())
    assert calls == 0


@pytest.mark.parametrize("color", ANSI_COLOR_PRESETS)
def test_all_color_presets_parse_through_project_config(tmp_path: Path, color: str) -> None:
    config = Path("config.example.toml").read_text()
    config = config.replace('memory = "yellow"', f'memory = "{color}"')
    config = config.replace('utilization = "cyan"', f'utilization = "{color}"')
    config = config.replace('temperature = "red"', f'temperature = "{color}"')
    path = tmp_path / "config.toml"
    path.write_text(config)

    assert load_config(path).dashboard_colors == DashboardColors(
        memory=color,
        utilization=color,
        temperature=color,
    )
