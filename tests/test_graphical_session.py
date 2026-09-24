from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/graphical_session.py"


def _fixture(
    tmp_path: Path,
    *,
    app_mode: str = "wait",
    compositor_mode: str = "run",
    terminal_mode: str = "run",
    sudo_delay: float = 0,
) -> tuple[dict[str, str], Path]:
    project = tmp_path / "project with spaces"
    (project / ".venv/bin").mkdir(parents=True)
    (project / "config.toml").write_text("[hardware]\nbackend='fake'\n")
    app = project / ".venv/bin/dgx-fan"
    app.write_text(
        "#!/usr/bin/python3\n"
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "log = Path(os.environ['DGX_TEST_LOG'])\n"
        "def stopped(signum, frame):\n"
        "    log.open('a').write(f'app-signal:{signum}\\n')\n"
        "    raise SystemExit(0 if signum == signal.SIGUSR1 else 1)\n"
        "signal.signal(signal.SIGUSR1, stopped)\n"
        "signal.signal(signal.SIGTERM, stopped)\n"
        "os.write(int(os.environ['DGX_FAN_SIGNAL_READY_FD']), b'1')\n"
        "os.close(int(os.environ['DGX_FAN_SIGNAL_READY_FD']))\n"
        "log.open('a').write(f'app-ready:{os.getpid()}\\n')\n"
        f"mode = {app_mode!r}\n"
        "if mode == 'pre_mount_exit': raise SystemExit(0)\n"
        "os.write(int(os.environ['DGX_FAN_MOUNTED_READY_FD']), b'M')\n"
        "os.close(int(os.environ['DGX_FAN_MOUNTED_READY_FD']))\n"
        "log.open('a').write('app-mounted\\n')\n"
        "if mode == 'natural':\n"
        "    log.open('a').write('app-natural\\n')\n"
        "else:\n"
        "    while True: time.sleep(.05)\n"
    )
    app.chmod(0o755)
    binary = tmp_path / "bin"
    binary.mkdir()
    for name, body in {
        "sudo": f'printf "sudo-start\\n" >> "$DGX_TEST_LOG"\nsleep {sudo_delay}\nexit 0\n',
        "labwc": (
            "import os, shlex, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "Path(os.environ['DGX_TEST_LOG']).open('a').write('labwc-start\\n')\n"
            f"mode = {compositor_mode!r}\n"
            "if mode == 'crash': time.sleep(.2); sys.exit(9)\n"
            "child = subprocess.Popen(shlex.split(sys.argv[-1]))\n"
            "try: sys.exit(child.wait())\n"
            "finally:\n"
            "    if child.poll() is None: child.terminate(); child.wait()\n"
        ),
        "lxterminal": (
            "import os, shlex, subprocess, sys\n"
            "from pathlib import Path\n"
            "assert '--no-remote' in sys.argv\n"
            "Path(os.environ['DGX_TEST_LOG']).open('a').write('terminal-start\\n')\n"
            f"mode = {terminal_mode!r}\n"
            "if mode == 'empty': sys.exit(0)\n"
            "command = next(arg.split('=', 1)[1] for arg in sys.argv if arg.startswith('--command='))\n"
            "sys.exit(subprocess.run(shlex.split(command)).returncode)\n"
        ),
    }.items():
        path = binary / name
        path.write_text(("#!/bin/sh\n" if name == "sudo" else "#!/usr/bin/python3\n") + body)
        path.chmod(0o755)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{binary}:{environment['PATH']}",
            "XDG_RUNTIME_DIR": str(runtime),
            "DGX_FAN_INSTALL_TESTING": "1",
            "DGX_FAN_GRAPHICAL_TEST_ROOT": str(project),
            "DGX_TEST_LOG": str(tmp_path / "lifecycle.log"),
        }
    )
    return environment, tmp_path / "lifecycle.log"


def _wait_for(log: Path, token: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if log.exists() and token in log.read_text():
            return
        time.sleep(.05)
    raise AssertionError(f"missing {token}: {log.read_text() if log.exists() else '<no log>'}")


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd required")
def test_graphical_stop_signals_ready_app_before_teardown(tmp_path: Path) -> None:
    environment, log = _fixture(tmp_path)
    address = f"\0dgx-test-notify-{os.getpid()}-{time.monotonic_ns()}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notification:
        notification.bind(address)
        notification.settimeout(5)
        environment["NOTIFY_SOCKET"] = "@" + address[1:]
        process = subprocess.Popen([sys.executable, str(SCRIPT), "session"], env=environment)
        try:
            assert notification.recv(64) == b"READY=1"
            _wait_for(log, "app-mounted")
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=8) == 0
            entries = log.read_text().splitlines()
            assert entries.index("app-mounted") < entries.index(f"app-signal:{signal.SIGUSR1}")
            assert "terminal-start" in entries and "labwc-start" in entries
            app_pid = int(next(entry.split(":", 1)[1] for entry in entries if entry.startswith("app-ready:")))
            with pytest.raises(ProcessLookupError):
                os.kill(app_pid, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd required")
def test_graphical_natural_exit_closes_compositor(tmp_path: Path) -> None:
    environment, log = _fixture(tmp_path, app_mode="natural")
    completed = subprocess.run([sys.executable, str(SCRIPT), "session"], env=environment, timeout=8, check=False)
    assert completed.returncode == 0
    assert "app-natural" in log.read_text()


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd required")
def test_graphical_early_stop_finishes_before_controller_launch(tmp_path: Path) -> None:
    environment, log = _fixture(tmp_path, sudo_delay=1)
    process = subprocess.Popen([sys.executable, str(SCRIPT), "session"], env=environment)
    try:
        _wait_for(log, "sudo-start")
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=8) == 0
        assert "app-ready" not in log.read_text()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd required")
def test_graphical_compositor_crash_uses_abnormal_signal(tmp_path: Path) -> None:
    environment, log = _fixture(tmp_path, compositor_mode="crash")
    completed = subprocess.run([sys.executable, str(SCRIPT), "session"], env=environment, timeout=8, check=False)
    assert completed.returncode != 0
    if "app-ready" in log.read_text():
        assert f"app-signal:{signal.SIGTERM}" in log.read_text()


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd required")
@pytest.mark.parametrize("terminal_mode,app_mode", [("empty", "wait"), ("run", "pre_mount_exit")])
def test_graphical_success_without_mounted_controller_is_failure(
    tmp_path: Path, terminal_mode: str, app_mode: str
) -> None:
    environment, log = _fixture(tmp_path, terminal_mode=terminal_mode, app_mode=app_mode)
    completed = subprocess.run([sys.executable, str(SCRIPT), "session"], env=environment, timeout=8, check=False)
    assert completed.returncode != 0
    assert "app-mounted" not in log.read_text()
