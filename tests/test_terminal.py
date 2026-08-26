from __future__ import annotations

import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX PTY APIs are required")


def _config() -> str:
    return '''version = 1
[[dgx]]
id = "one"
name = "One"
url = "http://127.0.0.1:9/metrics"
[collection]
interval_seconds = 2
timeout_seconds = 0.1
stale_after_seconds = 6
[control]
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
pwm_gpio_bcm = 18
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
        _read_until(master, b"DGX Fan Controller", time.monotonic() + 10)
        os.write(master, b"\x11")  # Textual's confirmed default Quit binding: Ctrl+Q.

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pty_flags(slave) != before:
            time.sleep(0.05)
        assert _pty_flags(slave) == before

        os.write(master, b"restored-input\n")
        output = _read_until(master, b"SENTINEL:restored-input", time.monotonic() + 5)
        assert b"restored-input" in output  # Canonical shell input was echoed after the app quit.
        assert process.wait(timeout=5) == 0
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        os.close(master)
        os.close(slave)
