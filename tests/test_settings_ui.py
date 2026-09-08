import asyncio
from typing import Any, cast

from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Button, Input, Static

from dgx_fan.settings_ui import SettingsScreen


def _initial() -> dict[str, Any]:
    return {
        "source_id": "controller-1", "revision": 7,
        "settings": {
            "collection": {"interval_seconds": 2.0, "timeout_seconds": 1.0, "stale_after_seconds": 6.0, "retry_count": 2, "retry_delay_seconds": 3.0},
            "control": {"fan_endpoint_ids": ["one", "two"], "enabled_at_startup": True, "max_speed_percent": 90, "fallback_speed_percent": 100, "hysteresis_celsius": 2.0, "emergency_temperature_celsius": 75.0, "recovery_seconds": 10.0, "stages": [{"max_temperature_celsius": 45.0, "speed_percent": 20}, {"max_temperature_celsius": 55.0, "speed_percent": 50}, {"max_temperature_celsius": 70.0, "speed_percent": 80}, {"speed_percent": 100}]},
            "hardware": {"startup_boost_seconds": 1.0, "stall_timeout_seconds": 5.0, "shutdown_mode": "full"},
            "dashboard": {"colors": {"memory": "blue", "utilization": None, "temperature": "red"}},
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
        calls.append(args[0]); return _initial()

    result: list[bool] = []
    app = _ModalApp(SettingsScreen(_initial(), save), result)

    async def exercise() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            _editor(app).query_one("#setting-cancel", Button).press()
            await pilot.pause()
            assert result == [False] and calls == []

    asyncio.run(exercise())


def test_settings_save_sends_typed_allowlisted_patch() -> None:
    calls: list[tuple[dict[str, Any], int, str]] = []

    async def save(patch: dict[str, Any], revision: int, source_id: str) -> dict[str, Any]:
        calls.append((patch, revision, source_id)); return _initial()

    result: list[bool] = []
    app = _ModalApp(SettingsScreen(_initial(), save), result)

    async def exercise() -> None:
        async with app.run_test(size=(128, 37)) as pilot:
            await pilot.pause()
            screen = _editor(app)
            screen.query_one("#setting-collection-retry_count", Input).value = "4"
            screen.query_one("#setting-stage-4-speed", Input).value = "95"
            screen.query_one("#setting-color-utilization", Input).value = ""
            screen.query_one("#setting-save", Button).press()
            await pilot.pause()
            assert result == [True]

    asyncio.run(exercise())
    patch, revision, source_id = calls[0]
    assert (revision, source_id) == (7, "controller-1")
    assert patch["collection"]["retry_count"] == 4
    assert isinstance(patch["collection"]["interval_seconds"], float)
    assert patch["control"]["stages"][3] == {"speed_percent": 95}
    assert "utilization" not in patch["dashboard"]["colors"]
    assert set(patch["hardware"]) == {"startup_boost_seconds", "stall_timeout_seconds", "shutdown_mode"}


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
            assert "reload before saving" in str(screen.query_one("#settings-error", Static).render())

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
        await release.wait()
        return _initial()

    result: list[bool] = []
    app = _ModalApp(SettingsScreen(_initial(), save), result)

    async def exercise() -> None:
        async with app.run_test(size=(128, 37)) as pilot:
            await pilot.pause()
            save_button = _editor(app).query_one("#setting-save", Button)
            save_button.press()
            await started.wait()
            save_button.press()
            assert calls == 1 and save_button.disabled
            release.set()
            await pilot.pause()
            assert result == [True]

    asyncio.run(exercise())
