import sys
from types import SimpleNamespace

import pytest

import dgx_fan.app as app_module
from dgx_fan.app import _capture_terminal_state, _restore_terminal_state, _TerminalState, main


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    assert "--config" in capsys.readouterr().out


def test_invalid_config_fails_before_hardware() -> None:
    with pytest.raises(SystemExit, match="configuration error"):
        main(["--config", "does-not-exist.toml"])


def test_terminal_capture_is_a_noop_for_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert _capture_terminal_state() is None


def test_terminal_state_restores_exact_attributes_and_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    original = [1, 2, 3, 4, 5, 6, [7, 8]]
    calls: list[object] = []
    fake_termios = SimpleNamespace(
        TCSANOW=9,
        tcgetattr=lambda fd: original,
        tcsetattr=lambda fd, when, attrs: calls.append((fd, when, attrs)),
    )
    monkeypatch.setitem(sys.modules, "termios", fake_termios)
    monkeypatch.setattr(app_module.sys, "stdin", SimpleNamespace(isatty=lambda: True, fileno=lambda: 42))
    monkeypatch.setattr(app_module.os, "name", "posix")
    monkeypatch.setattr(app_module.os, "get_blocking", lambda fd: False)
    monkeypatch.setattr(app_module.os, "set_blocking", lambda fd, blocking: calls.append((fd, blocking)))

    state = _capture_terminal_state()
    assert state == _TerminalState(42, [1, 2, 3, 4, 5, 6, [7, 8]], False)
    original[6][0] = 99
    _restore_terminal_state(state)

    assert calls == [(42, 9, [1, 2, 3, 4, 5, 6, [7, 8]]), (42, False)]


def test_app_exception_restores_terminal_and_releases_hardware(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class Hardware:
        def release(self) -> None:
            events.append("release")

    class ExplodingApp:
        def __init__(self, config: object) -> None:
            self.hardware = Hardware()

        def run(self) -> None:
            raise RuntimeError("app failure")

    monkeypatch.setattr(app_module, "load_config", lambda path: object())
    monkeypatch.setattr(app_module, "DGXFanApp", ExplodingApp)
    monkeypatch.setattr(app_module, "_capture_terminal_state", lambda: None)
    monkeypatch.setattr(app_module, "_restore_terminal_state", lambda state: events.append("restore"))

    with pytest.raises(RuntimeError, match="app failure"):
        main(["--config", "unused.toml"])
    assert events == ["restore", "release"]


def test_terminal_restore_failure_is_contained_and_hardware_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    fake_termios = SimpleNamespace(TCSANOW=0, tcsetattr=lambda fd, when, attrs: (_ for _ in ()).throw(OSError()))

    class Hardware:
        def release(self) -> None:
            events.append("release")

    class ReturningApp:
        def __init__(self, config: object) -> None:
            self.hardware = Hardware()

        def run(self) -> None:
            return None

    monkeypatch.setitem(sys.modules, "termios", fake_termios)
    monkeypatch.setattr(app_module, "load_config", lambda path: object())
    monkeypatch.setattr(app_module, "DGXFanApp", ReturningApp)
    monkeypatch.setattr(app_module, "_capture_terminal_state", lambda: _TerminalState(1, [], True))
    monkeypatch.setattr(app_module.os, "set_blocking", lambda fd, blocking: (_ for _ in ()).throw(OSError()))

    main(["--config", "unused.toml"])
    assert events == ["release"]
