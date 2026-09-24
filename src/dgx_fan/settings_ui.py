"""A small, transport-agnostic editor for the controller settings envelope."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from math import isfinite
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Input,
    Label,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)

ANSI_COLOR_PRESETS = (
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white",
    "bright_black",
    "bright_red",
    "bright_green",
    "bright_yellow",
    "bright_blue",
    "bright_magenta",
    "bright_cyan",
    "bright_white",
)


class SettingsScreen(ModalScreen[bool]):
    """Edit a controller snapshot, applying it only after an acknowledged save."""

    DEFAULT_CSS = """
    SettingsScreen { align: center middle; }
    #settings-dialog { width: 78; max-width: 100%; height: 1fr; max-height: 100%; border: round $primary; }
    #settings-title { height: 1; content-align: center middle; }
    #settings-tabs { height: 1fr; margin: 0 1; }
    #settings-tabs ContentSwitcher, #settings-tabs TabPane { height: 1fr; }
    .settings-scroll { height: 1fr; padding: 0 1; }
    .settings-section { height: auto; margin: 1 0; }
    .setting-row { height: 3; }
    .setting-label { width: 38; content-align: left middle; }
    .setting-input { width: 1fr; }
    .speed-row { height: 3; }
    .speed-stage { width: 9; content-align: left middle; }
    .speed-label { width: 8; content-align: right middle; margin-right: 1; }
    .speed-input { width: 1fr; min-width: 8; }
    .speed-open { width: 1fr; content-align: left middle; padding: 0 2; }
    .setting-note { color: $text-muted; height: auto; margin-bottom: 1; }
    #settings-error { color: $error; height: auto; max-height: 3; margin: 0 1; }
    #settings-actions { height: 3; align: right middle; padding: 0 1; }
    #setting-save, #setting-cancel { margin-left: 1; min-width: 14; }
    """

    def __init__(
        self,
        initial: dict[str, Any],
        save: Callable[[dict[str, Any], int, str], Awaitable[dict[str, Any]]],
    ) -> None:
        super().__init__()
        self.initial = initial
        self.save = save
        self._saving = False
        self._source_id = self._required_string(initial, "source_id")
        self._revision = self._required_int(initial, "revision")
        self._settings = self._required_mapping(initial, "settings")
        self._endpoints = initial.get("endpoints", [])

    @staticmethod
    def _required_mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
        result = value.get(key)
        if not isinstance(result, dict):
            raise TypeError(f"settings response is missing {key}")
        return result

    @staticmethod
    def _required_string(value: dict[str, Any], key: str) -> str:
        result = value.get(key)
        if not isinstance(result, str) or not result:
            raise ValueError(f"settings response is missing {key}")
        return result

    @staticmethod
    def _required_int(value: dict[str, Any], key: str) -> int:
        result = value.get(key)
        if not isinstance(result, int) or isinstance(result, bool):
            raise TypeError(f"settings response is missing {key}")
        return result

    def _section(self, name: str) -> dict[str, Any]:
        return self._required_mapping(self._settings, name)

    @staticmethod
    def _text(value: object) -> str:
        return "" if value is None else str(value)

    def _input(self, field: str, value: object, label: str) -> Input:
        return Input(self._text(value), id=f"setting-{field}", classes="setting-input", name=label)

    def _row(self, label: str, widget: Input | Select[str] | Switch) -> Horizontal:
        return Horizontal(Label(label, classes="setting-label"), widget, classes="setting-row")

    def _stage_row(self, index: int, stage: dict[str, Any]) -> Horizontal:
        speed = self._input(
            f"stage-{index}-speed", stage.get("speed_percent"), f"Stage {index} speed"
        )
        speed.add_class("speed-input")
        if index == 4:
            return Horizontal(
                Label(f"Stage {index}", classes="speed-stage"),
                Label("", classes="speed-label"),
                Static("Above stage 3", id="setting-stage-4-threshold", classes="speed-open"),
                Label("Speed %", classes="speed-label"),
                speed,
                id=f"setting-stage-row-{index}",
                classes="speed-row",
            )
        threshold = self._input(
            f"stage-{index}-temperature",
            stage.get("max_temperature_celsius"),
            f"Stage {index} maximum temperature",
        )
        threshold.add_class("speed-input")
        return Horizontal(
            Label(f"Stage {index}", classes="speed-stage"),
            Label("Max C", classes="speed-label"),
            threshold,
            Label("Speed %", classes="speed-label"),
            speed,
            id=f"setting-stage-row-{index}",
            classes="speed-row",
        )

    @staticmethod
    def _color_options(current: object) -> tuple[list[tuple[Text, str]], str]:
        selected = current if isinstance(current, str) and current else ""
        options = [(Text("Default (terminal theme)"), "")]
        for name in ANSI_COLOR_PRESETS:
            label = Text()
            label.append("    ", style=f"on {name}")
            label.append(f" {name.replace('_', ' ').title()}")
            options.append((label, name))
        if selected and selected not in ANSI_COLOR_PRESETS:
            options.append((Text(f"Current custom: {selected}"), selected))
        return options, selected

    def compose(self) -> ComposeResult:
        collection = self._section("collection")
        control = self._section("control")
        hardware = self._section("hardware")
        colors = self._section("dashboard").get("colors", {})
        graph_view = self._section("dashboard").get("graph_view", "graph-1")
        if not isinstance(colors, dict):
            colors = {}
        stages = control.get("stages", [])
        if (
            not isinstance(stages, list)
            or len(stages) != 4
            or not all(isinstance(x, dict) for x in stages)
        ):
            raise ValueError("settings response must contain four control stages")
        endpoint_options = [
            (
                f"{item.get('name', item.get('id', 'endpoint'))} ({item.get('id', '')})",
                item.get("id", ""),
            )
            for item in self._endpoints
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
        ]
        with Vertical(id="settings-dialog"):
            yield Static("Setting", id="settings-title")
            with TabbedContent(initial="settings-tab-collection", id="settings-tabs"):
                with (
                    TabPane("Collection", id="settings-tab-collection"),
                    VerticalScroll(id="settings-scroll-collection", classes="settings-scroll"),
                    Vertical(id="settings-section-collection", classes="settings-section"),
                ):
                    for key, label in (
                        ("interval_seconds", "Poll interval (seconds)"),
                        ("timeout_seconds", "Request timeout (seconds)"),
                        ("stale_after_seconds", "Stale after (seconds)"),
                        ("retry_count", "Retry count"),
                        ("retry_delay_seconds", "Retry delay (seconds)"),
                    ):
                        yield self._row(
                            label,
                            self._input(f"collection-{key}", collection.get(key), label),
                        )
                with (
                    TabPane("Fan Speed", id="settings-tab-speed"),
                    VerticalScroll(id="settings-scroll-speed", classes="settings-scroll"),
                    Vertical(id="settings-section-speed", classes="settings-section"),
                ):
                    for index, stage in enumerate(stages, start=1):
                        yield self._stage_row(index, stage)
                with (
                    TabPane("Fan Control", id="settings-tab-control"),
                    VerticalScroll(id="settings-scroll-control", classes="settings-scroll"),
                    Vertical(id="settings-section-control", classes="settings-section"),
                ):
                    fan_ids = control.get("fan_endpoint_ids", ["", ""])
                    if not isinstance(fan_ids, list) or len(fan_ids) != 2:
                        fan_ids = ["", ""]
                    yield self._row(
                        "Fan 1 DGX endpoint",
                        Select(
                            endpoint_options,
                            value=fan_ids[0],
                            id="setting-control-fan-1",
                            classes="setting-input",
                        ),
                    )
                    yield self._row(
                        "Fan 2 DGX endpoint",
                        Select(
                            endpoint_options,
                            value=fan_ids[1],
                            id="setting-control-fan-2",
                            classes="setting-input",
                        ),
                    )
                    yield self._row(
                        "Fan mode",
                        Select(
                            [
                                ("Independent", "independent"),
                                ("Linked (higher demand)", "linked"),
                            ],
                            value=control.get("fan_mode", "independent"),
                            id="setting-control-fan-mode",
                            classes="setting-input",
                        ),
                    )
                    yield self._row(
                        "Enable at next startup",
                        Switch(
                            bool(control.get("enabled_at_startup")),
                            id="setting-control-enabled-at-startup",
                        ),
                    )
                    for key, label in (
                        ("max_speed_percent", "Normal maximum (%)"),
                        ("fallback_speed_percent", "Fallback safety speed (%)"),
                        ("hysteresis_celsius", "Hysteresis (C)"),
                        (
                            "emergency_temperature_celsius",
                            "Emergency temperature (C)",
                        ),
                        ("recovery_seconds", "Recovery dwell (seconds)"),
                    ):
                        yield self._row(
                            label,
                            self._input(f"control-{key}", control.get(key), label),
                        )
                    yield Static(
                        "Fallback overrides the normal cap; safety may override requested power.",
                        classes="setting-note",
                    )
                with (
                    TabPane("UI", id="settings-tab-ui"),
                    VerticalScroll(id="settings-scroll-ui", classes="settings-scroll"),
                    Vertical(id="settings-section-ui", classes="settings-section"),
                ):
                    yield self._row(
                        "Dashboard view",
                        Select(
                            [("Graph #1 (classic)", "graph-1"), ("Graph #2 (two DGX)", "graph-2")],
                            value=graph_view if graph_view in {"graph-1", "graph-2"} else "graph-1",
                            id="setting-dashboard-graph-view",
                            classes="setting-input",
                        ),
                    )
                with (
                    TabPane("Hardware", id="settings-tab-hardware"),
                    VerticalScroll(id="settings-scroll-hardware", classes="settings-scroll"),
                    Vertical(id="settings-section-hardware", classes="settings-section"),
                ):
                    yield self._row(
                        "Startup boost (seconds)",
                        self._input(
                            "hardware-startup-boost-seconds",
                            hardware.get("startup_boost_seconds"),
                            "Startup boost",
                        ),
                    )
                    yield self._row(
                        "Stall timeout (seconds)",
                        self._input(
                            "hardware-stall-timeout-seconds",
                            hardware.get("stall_timeout_seconds"),
                            "Stall timeout",
                        ),
                    )
                    yield self._row(
                        "Shutdown policy",
                        Select(
                            [("Full speed (safe)", "full"), ("Off", "off")],
                            value=hardware.get("shutdown_mode", "full"),
                            id="setting-hardware-shutdown-mode",
                            classes="setting-input",
                        ),
                    )
                    yield Static(
                        "Startup boost applies to later startup events; the shutdown policy applies at the next clean exit.",
                        classes="setting-note",
                    )
                with (
                    TabPane("Colors", id="settings-tab-colors"),
                    VerticalScroll(id="settings-scroll-colors", classes="settings-scroll"),
                    Vertical(id="settings-section-colors", classes="settings-section"),
                ):
                    for key, label in (
                        ("memory", "Memory color"),
                        ("utilization", "Utilization color"),
                        ("temperature", "Temperature color"),
                        ("power", "Power color"),
                    ):
                        options, selected = self._color_options(
                            colors.get(key, "ansi_green" if key == "power" else None)
                        )
                        yield self._row(
                            label,
                            Select(
                                options,
                                value=selected,
                                allow_blank=False,
                                id=f"setting-color-{key}",
                                classes="setting-input",
                            ),
                        )
            yield Static("", id="settings-error", markup=False)
            with Horizontal(id="settings-actions"):
                yield Button("Cancel", id="setting-cancel")
                yield Button("Save and Apply", id="setting-save", variant="primary")

    def _input_value(self, field: str) -> str:
        return self.query_one(f"#setting-{field}", Input).value.strip()

    def _number(self, field: str, label: str, *, integer: bool = False) -> int | float:
        raw = self._input_value(field)
        try:
            value = int(raw) if integer else float(raw)
        except ValueError as error:
            raise ValueError(
                f"{label} must be a {'whole number' if integer else 'number'}"
            ) from error
        if isinstance(value, float) and not isfinite(value):
            raise ValueError(f"{label} must be finite")
        return value

    def _select_value(self, field: str, label: str) -> str:
        value = self.query_one(f"#setting-{field}", Select).value
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label} is required")
        return value

    def _color_value(self, field: str) -> str:
        value = self.query_one(f"#setting-color-{field}", Select).value
        if not isinstance(value, str):
            raise ValueError(  # noqa: TRY004 - draft validation is user-facing.
                f"{field.title()} color is required"
            )
        return value

    def draft(self) -> dict[str, Any]:
        """Return the typed, allowlisted patch without mutating the controller."""
        collection = {
            "interval_seconds": self._number("collection-interval_seconds", "Poll interval"),
            "timeout_seconds": self._number("collection-timeout_seconds", "Request timeout"),
            "stale_after_seconds": self._number("collection-stale_after_seconds", "Stale after"),
            "retry_count": self._number("collection-retry_count", "Retry count", integer=True),
            "retry_delay_seconds": self._number("collection-retry_delay_seconds", "Retry delay"),
        }
        stages: list[dict[str, int | float]] = []
        for index in range(1, 5):
            stage: dict[str, int | float] = {
                "speed_percent": self._number(
                    f"stage-{index}-speed", f"Stage {index} speed", integer=True
                )
            }
            if index < 4:
                stage["max_temperature_celsius"] = self._number(
                    f"stage-{index}-temperature", f"Stage {index} maximum temperature"
                )
            stages.append(stage)
        colors = {
            key: value
            for key in ("memory", "utilization", "temperature", "power")
            if (value := self._color_value(key))
        }
        return {
            "collection": collection,
            "control": {
                "fan_endpoint_ids": [
                    self._select_value("control-fan-1", "Fan 1 endpoint"),
                    self._select_value("control-fan-2", "Fan 2 endpoint"),
                ],
                "fan_mode": self._select_value("control-fan-mode", "Fan mode"),
                "enabled_at_startup": self.query_one(
                    "#setting-control-enabled-at-startup", Switch
                ).value,
                "max_speed_percent": self._number(
                    "control-max_speed_percent", "Normal maximum", integer=True
                ),
                "fallback_speed_percent": self._number(
                    "control-fallback_speed_percent", "Fallback safety speed", integer=True
                ),
                "hysteresis_celsius": self._number("control-hysteresis_celsius", "Hysteresis"),
                "emergency_temperature_celsius": self._number(
                    "control-emergency_temperature_celsius", "Emergency temperature"
                ),
                "recovery_seconds": self._number("control-recovery_seconds", "Recovery dwell"),
                "stages": stages,
            },
            "hardware": {
                "startup_boost_seconds": self._number(
                    "hardware-startup-boost-seconds", "Startup boost"
                ),
                "stall_timeout_seconds": self._number(
                    "hardware-stall-timeout-seconds", "Stall timeout"
                ),
                "shutdown_mode": self._select_value("hardware-shutdown-mode", "Shutdown policy"),
            },
            "dashboard": {
                "colors": colors,
                "graph_view": self._select_value("dashboard-graph-view", "UI dashboard"),
            },
        }

    def _show_error(self, message: str) -> None:
        self.query_one("#settings-error", Static).update(message)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "setting-cancel":
            if not self._saving:
                self.dismiss(False)
            return
        if event.button.id != "setting-save" or self._saving:
            return
        try:
            patch = self.draft()
        except ValueError as error:
            self._show_error(str(error))
            return
        self._saving = True
        event.button.disabled = True
        self._show_error("Saving…")
        try:
            response = await self.save(patch, self._revision, self._source_id)
            self._required_string(response, "source_id")
            self._required_int(response, "revision")
            self._required_mapping(response, "settings")
        except Exception as error:  # noqa: BLE001 - save transports user-facing controller errors.
            self._show_error(str(error) or "Unable to save settings")
            self._saving = False
            event.button.disabled = False
            return
        self.dismiss(True)
