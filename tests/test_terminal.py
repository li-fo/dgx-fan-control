from __future__ import annotations

import copy
import os
import select
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX PTY APIs are required")

if os.name == "posix":
    import pty
    import termios
    import tty

from dgx_fan import app as app_module


def _config() -> str:
    return '''version = 2
[[dgx]]
id = "one"
name = "One"
url = "http://127.0.0.1:9/metrics"
[collection]
interval_seconds = 2
timeout_seconds = 0.1
stale_after_seconds = 6
[control]
fan_endpoint_ids = ["one", "one"]
enabled_at_startup = true
max_speed_percent = 90
hysteresis_celsius = 2
emergency_temperature_celsius = 75
recovery_seconds = 10
[[control.stages]]
max_temperature_celsius = 45
speed_percent = 20
[[control.stages]]
max_temperature_celsius = 55
speed_percent = 50
[[control.stages]]
max_temperature_celsius = 70
speed_percent = 80
[[control.stages]]
speed_percent = 100
[hardware]
backend = "fake"
pwm_gpio_bcm = [18, 19]
pwm_frequency_hz = 25000
pwm_inverted = true
tach_gpio_bcm = [23, 24]
pulses_per_revolution = [2, 2]
startup_boost_seconds = 1
stall_timeout_seconds = 5
'''


def _pty_flags(fd: int) -> tuple[int, int, int, bool]:
    attributes = termios.tcgetattr(fd)
    return (
        attributes[3] & (termios.ICANON | termios.ECHO | termios.ISIG),
        attributes[0] & termios.IXON,
        attributes[1] & termios.OPOST,
        os.get_blocking(fd),
    )


def _read_until(fd: int, marker: bytes, deadline: float) -> bytes:
    output = bytearray()
    while time.monotonic() < deadline:
        readable, _, _ = select.select([fd], [], [], 0.1)
        if readable:
            try:
                output.extend(os.read(fd, 4096))
            except OSError:
                break
        if marker in output:
            return bytes(output)
    raise AssertionError(f"PTY output did not contain {marker!r}: {bytes(output)!r}")


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def test_stop_process_group_reaps_after_term() -> None:
    calls: list[object] = []

    class Process:
        pid = 17

        def poll(self) -> None:
            calls.append("poll")

        def wait(self, timeout: float) -> int:
            calls.append(("wait", timeout))
            return 0

    process = Process()
    original_killpg = os.killpg
    try:
        os.killpg = lambda pid, sig: calls.append(("kill", pid, sig))
        _stop_process_group(process)  # type: ignore[arg-type]
    finally:
        os.killpg = original_killpg

    assert calls == ["poll", ("kill", 17, signal.SIGTERM), ("wait", 2)]


def test_stop_process_group_kills_and_reaps_after_term_timeout() -> None:
    calls: list[object] = []

    class Process:
        pid = 17

        def poll(self) -> None:
            pass

        def wait(self, timeout: float) -> int:
            calls.append(("wait", timeout))
            if len(calls) == 2:
                raise subprocess.TimeoutExpired("test", timeout)
            return 0

    process = Process()
    original_killpg = os.killpg
    try:
        os.killpg = lambda pid, sig: calls.append(("kill", pid, sig))
        _stop_process_group(process)  # type: ignore[arg-type]
    finally:
        os.killpg = original_killpg

    assert calls == [
        ("kill", 17, signal.SIGTERM),
        ("wait", 2),
        ("kill", 17, signal.SIGKILL),
        ("wait", 2),
    ]


def test_stop_process_group_reaps_when_term_races_with_child_exit() -> None:
    calls: list[object] = []

    class Process:
        pid = 17

        def poll(self) -> None:
            calls.append("poll")

        def wait(self, timeout: float) -> int:
            calls.append(("wait", timeout))
            return 0

    process = Process()
    original_killpg = os.killpg
    try:
        os.killpg = lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError())
        _stop_process_group(process)  # type: ignore[arg-type]
    finally:
        os.killpg = original_killpg

    assert calls == ["poll", ("wait", 2)]


def test_capture_restore_restores_complete_state_and_closes_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    master, slave = pty.openpty()
    try:
        before = copy.deepcopy(termios.tcgetattr(slave))
        blocking = os.get_blocking(slave)
        stdin = type("PTYStdin", (), {"isatty": lambda self: True, "fileno": lambda self: slave})()
        monkeypatch.setattr(app_module.sys, "stdin", stdin)

        state = app_module._capture_terminal_state()
        assert state is not None and state.fd is not None
        duplicate_fd = state.fd
        tty.setraw(slave)
        os.set_blocking(slave, not blocking)
        assert termios.tcgetattr(slave) != before or os.get_blocking(slave) != blocking

        app_module._restore_terminal_state(state)

        assert termios.tcgetattr(slave) == before
        assert os.get_blocking(slave) == blocking
        assert state.fd is None
        with pytest.raises(OSError):
            os.fstat(duplicate_fd)
    finally:
        try:
            os.close(master)
        finally:
            os.close(slave)


def test_cli_quit_restores_posix_terminal_and_accepts_sentinel(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(_config())
    master, slave = pty.openpty()
    process: subprocess.Popen[bytes] | None = None
    try:
        import fcntl

        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        before = _pty_flags(slave)
        command = (
            f'"{sys.executable}" -m dgx_fan --config "{config}"; '
            'IFS= read -r value; printf "SENTINEL:%s\\n" "$value"'
        )
        process = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        _read_until(master, b"DGX Fan Controller", time.monotonic() + 6)
        os.write(master, b"\x11")  # Textual's confirmed default Quit binding: Ctrl+Q.

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and _pty_flags(slave) != before:
            time.sleep(0.05)
        assert _pty_flags(slave) == before

        os.write(master, b"restored-input\n")
        output = _read_until(master, b"SENTINEL:restored-input", time.monotonic() + 3)
        assert b"restored-input" in output  # Canonical shell input was echoed after the app quit.
        assert process.wait(timeout=3) == 0
    finally:
        try:
            if process is not None:
                _stop_process_group(process)
        finally:
            try:
                os.close(master)
            finally:
                os.close(slave)
