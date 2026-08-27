import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.color import ColorSystem
from rich.segment import Segment
from rich.style import Style
from rich.terminal_theme import DEFAULT_TERMINAL_THEME
from textual.color import Color
from textual.filter import ANSIToTruecolor
from textual.strip import Strip

import dgx_fan.app as app_module
from dgx_fan.app import (
    DGXFanApp,
    _capture_terminal_state,
    _restore_terminal_state,
    _TerminalState,
    main,
)
from dgx_fan.config import DashboardColors, load_config
from dgx_fan.ui import FanAppUI


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    assert "--config" in capsys.readouterr().out


def test_invalid_config_fails_before_hardware() -> None:
    with pytest.raises(SystemExit, match="configuration error"):
        main(["--config", "does-not-exist.toml"])


def test_invalid_dashboard_color_fails_before_app_or_hardware(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = (
        Path("config.example.toml").read_text().replace('memory = "yellow"', 'memory = "bold red"')
    )
    path = tmp_path / "config.toml"
    path.write_text(config)
    monkeypatch.setattr(
        app_module,
        "DGXFanApp",
        lambda config: (_ for _ in ()).throw(AssertionError("app must not start")),
    )
    with pytest.raises(SystemExit, match=r"dashboard\.colors\.memory"):
        main(["--config", str(path)])


def test_app_transports_dashboard_colors_to_ui() -> None:
    config = load_config(Path("config.example.toml"))
    app = DGXFanApp(config)
    assert app.config.dashboard_colors == DashboardColors("yellow", "cyan", "red")

    async def exercise() -> None:
        async with app.run_test() as _:
            assert app.query_one(FanAppUI).dashboard_colors == config.dashboard_colors

    import asyncio

    asyncio.run(exercise())


def test_app_preserves_named_chart_colors_on_standard_ansi_terminals() -> None:
    """The app's ANSI filter must retain the standard Linux console slots."""
    config = load_config(Path("config.example.toml"))
    app = DGXFanApp(config)

    assert app.ansi_theme_dark is DEFAULT_TERMINAL_THEME
    assert app.ansi_theme_light is DEFAULT_TERMINAL_THEME

    ansi_filter = next(
        filter for filter in app._filters if isinstance(filter, ANSIToTruecolor)
    )
    for color, expected_sgr in (("yellow", "33"), ("cyan", "36"), ("red", "31")):
        segment = ansi_filter.apply(
            [Segment("chart", Style(color=color))], Color(0, 0, 0)
        )[0]
        assert segment.style is not None
        assert Strip.render_ansi(segment.style, ColorSystem.STANDARD) == expected_sgr


def test_terminal_capture_is_a_noop_for_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert _capture_terminal_state() is None


def test_terminal_state_restores_exact_attributes_and_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = [1, 2, 3, 4, 5, 6, [7, 8]]
    calls: list[object] = []
    fake_termios = SimpleNamespace(
        TCSANOW=9,
        tcgetattr=lambda fd: original,
        tcsetattr=lambda fd, when, attrs: calls.append((fd, when, attrs)),
    )
    monkeypatch.setitem(sys.modules, "termios", fake_termios)
    monkeypatch.setattr(
        app_module.sys, "stdin", SimpleNamespace(isatty=lambda: True, fileno=lambda: 42)
    )
    monkeypatch.setattr(app_module.os, "name", "posix")
    monkeypatch.setattr(app_module.os, "dup", lambda fd: 43)
    monkeypatch.setattr(app_module.os, "get_blocking", lambda fd: False)
    monkeypatch.setattr(
        app_module.os, "set_blocking", lambda fd, blocking: calls.append((fd, blocking))
    )
    monkeypatch.setattr(app_module.os, "close", lambda fd: calls.append(("close", fd)))

    state = _capture_terminal_state()
    assert state == _TerminalState(43, [1, 2, 3, 4, 5, 6, [7, 8]], False)
    original[6][0] = 99
    _restore_terminal_state(state)

    assert calls == [(43, 9, [1, 2, 3, 4, 5, 6, [7, 8]]), (43, False), ("close", 43)]
    assert state.fd is None


def test_terminal_capture_closes_duplicate_after_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    fake_termios = SimpleNamespace(TCSANOW=0, tcgetattr=lambda fd: (_ for _ in ()).throw(OSError()))
    monkeypatch.setitem(sys.modules, "termios", fake_termios)
    monkeypatch.setattr(
        app_module.sys, "stdin", SimpleNamespace(isatty=lambda: True, fileno=lambda: 42)
    )
    monkeypatch.setattr(app_module.os, "name", "posix")
    monkeypatch.setattr(app_module.os, "dup", lambda fd: 43)
    monkeypatch.setattr(app_module.os, "close", closed.append)

    assert _capture_terminal_state() is None
    assert closed == [43]


def test_app_exception_restores_terminal_and_releases_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Hardware:
        def release(self, *, normal_shutdown: bool = False) -> None:
            events.append(f"release:{normal_shutdown}")

    class ExplodingApp:
        def __init__(self, config: object) -> None:
            self.hardware = Hardware()

        def run(self) -> None:
            raise RuntimeError("app failure")

    monkeypatch.setattr(app_module, "load_config", lambda path: object())
    monkeypatch.setattr(app_module, "DGXFanApp", ExplodingApp)
    monkeypatch.setattr(app_module, "_capture_terminal_state", lambda: None)
    monkeypatch.setattr(
        app_module, "_restore_terminal_state", lambda state: events.append("restore")
    )

    with pytest.raises(RuntimeError, match="app failure"):
        main(["--config", "unused.toml"])
    assert events == ["restore", "release:False"]


def test_closed_stdin_does_not_prevent_app_run(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class Hardware:
        def release(self, *, normal_shutdown: bool = False) -> None:
            events.append(f"release:{normal_shutdown}")

    class ReturningApp:
        def __init__(self, config: object) -> None:
            self.hardware = Hardware()

        def run(self) -> None:
            events.append("run")

        return_code = 0

    monkeypatch.setattr(app_module, "load_config", lambda path: object())
    monkeypatch.setattr(app_module, "DGXFanApp", ReturningApp)
    monkeypatch.setattr(
        app_module.sys, "stdin", SimpleNamespace(isatty=lambda: (_ for _ in ()).throw(OSError()))
    )

    main(["--config", "unused.toml"])
    assert events == ["run", "release:True"]


def test_terminal_restore_failure_is_contained_and_hardware_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    fake_termios = SimpleNamespace(
        TCSANOW=0, tcsetattr=lambda fd, when, attrs: (_ for _ in ()).throw(OSError())
    )

    class Hardware:
        def release(self, *, normal_shutdown: bool = False) -> None:
            events.append(f"release:{normal_shutdown}")

    class ReturningApp:
        def __init__(self, config: object) -> None:
            self.hardware = Hardware()

        def run(self) -> None:
            return None

        return_code = 0

    monkeypatch.setitem(sys.modules, "termios", fake_termios)
    monkeypatch.setattr(app_module, "load_config", lambda path: object())
    monkeypatch.setattr(app_module, "DGXFanApp", ReturningApp)
    monkeypatch.setattr(app_module, "_capture_terminal_state", lambda: _TerminalState(1, [], True))
    monkeypatch.setattr(
        app_module.os, "set_blocking", lambda fd, blocking: (_ for _ in ()).throw(OSError())
    )

    main(["--config", "unused.toml"])
    assert events == ["release:True"]


def test_returned_textual_error_code_keeps_hardware_full(monkeypatch: pytest.MonkeyPatch) -> None:
    dispositions: list[bool] = []

    class Hardware:
        def release(self, *, normal_shutdown: bool = False) -> None:
            dispositions.append(normal_shutdown)

    class FailedApp:
        return_code = 1

        def __init__(self, config: object) -> None:
            self.hardware = Hardware()

        def run(self) -> None:
            return None

    monkeypatch.setattr(app_module, "load_config", lambda path: object())
    monkeypatch.setattr(app_module, "DGXFanApp", FailedApp)
    monkeypatch.setattr(app_module, "_capture_terminal_state", lambda: None)
    main(["--config", "unused.toml"])
    assert dispositions == [False]
