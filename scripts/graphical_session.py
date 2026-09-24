#!/usr/bin/python3
"""Own the isolated HDMI compositor, terminal, and controller lifecycle."""

from __future__ import annotations

import os
import secrets
import select
import selectors
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = (
    Path(os.environ["DGX_FAN_GRAPHICAL_TEST_ROOT"])
    if os.environ.get("DGX_FAN_INSTALL_TESTING") == "1" and os.environ.get("DGX_FAN_GRAPHICAL_TEST_ROOT")
    else Path(__file__).resolve().parents[1]
)
CONTROLLER = ROOT / ".venv/bin/dgx-fan"
CONFIG = ROOT / "config.toml"
PYTHON = Path(sys.executable)


def _write_config(directory: Path) -> Path:
    labwc = directory / "labwc"
    lxterminal = directory / "xdg/lxterminal"
    labwc.mkdir()
    lxterminal.mkdir(parents=True)
    (labwc / "rc.xml").write_text(
        '<labwc_config><windowRules><windowRule identifier="lxterminal" '
        'title="DGX Fan Controller"><action name="ToggleFullscreen"/>'
        '</windowRule></windowRules></labwc_config>\n',
        encoding="utf-8",
    )
    (lxterminal / "lxterminal.conf").write_text(
        "[general]\nfontname=DejaVu Sans Mono 12\nscrollback=0\n"
        "hidemenubar=true\nhidescrollbar=true\n",
        encoding="utf-8",
    )
    return labwc


def _terminate(process: subprocess.Popen[bytes], *, timeout: float = 3.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _signal_pidfd(pidfd: int, signum: int) -> None:
    signal.pidfd_send_signal(pidfd, signum)


def _notify_systemd_ready() -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if address:
        if address.startswith("@"):
            address = "\0" + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notification:
            notification.sendto(b"READY=1", address)


def _session() -> int:
    if os.geteuid() == 0:
        raise RuntimeError("graphical session must run as the login user")
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not Path(runtime).is_dir():
        raise RuntimeError("XDG_RUNTIME_DIR missing; check the PAM/logind tty8 session")
    for program in ("labwc", "lxterminal"):
        if not _which(program):
            raise RuntimeError(f"{program} missing; use ./scripts/display.sh start-console")
    if not CONTROLLER.is_file() or not os.access(CONTROLLER, os.X_OK) or not CONFIG.is_file():
        raise RuntimeError("controller or config missing; run ./install.sh first")

    requested_stop = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal requested_stop
        requested_stop = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with tempfile.TemporaryDirectory(prefix="dgx-fan-hdmi-", dir=runtime) as scratch:
        directory = Path(scratch)
        config_dir = _write_config(directory)
        socket_name = f"@dgx-fan-{os.getuid()}-{os.getpid()}-{secrets.token_hex(6)}"
        socket_address = "\0" + socket_name[1:]
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(socket_address)
        listener.listen(1)
        listener.setblocking(False)
        environment = os.environ.copy()
        environment["XDG_CONFIG_HOME"] = str(directory / "xdg")
        environment["LABWC_UPDATE_ACTIVATION_ENV"] = "0"
        environment["XDG_SESSION_TYPE"] = "wayland"
        environment.pop("DGX_FAN_TEXTUAL_TTY8_FONT", None)
        command = shlex.join([str(PYTHON), str(Path(__file__).resolve()), "terminal", socket_name])
        compositor = subprocess.Popen(
            ["labwc", "--config-dir", str(config_dir), "--session", command],
            env=environment,
            start_new_session=True,
        )
        pidfd: int | None = None
        connection: socket.socket | None = None
        mounted = False
        selector = selectors.DefaultSelector()
        selector.register(listener, selectors.EVENT_READ)

        def observe() -> None:
            nonlocal pidfd, connection, mounted
            for key, _ in selector.select(timeout=0.1):
                if key.fileobj is listener and pidfd is None:
                    connection, _ = listener.accept()
                    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    pid = int.from_bytes(credentials[:4], sys.byteorder, signed=True)
                    uid = int.from_bytes(credentials[4:8], sys.byteorder)
                    if uid != os.geteuid():
                        raise RuntimeError("controller session identity mismatch")
                    pidfd = os.pidfd_open(pid)
                    selector.unregister(listener)
                    selector.register(connection, selectors.EVENT_READ)
                elif connection is not None and key.fileobj is connection:
                    payload = connection.recv(1)
                    if payload == b"M" and not mounted:
                        mounted = True
                        _notify_systemd_ready()
                    elif not payload:
                        selector.unregister(connection)
                        connection.close()
                        connection = None

        try:
            while compositor.poll() is None and not requested_stop:
                observe()
            # A fast clean app can exit between sending mounted readiness and
            # the compositor exit observation. Consume that queued byte.
            if not requested_stop and not mounted and connection is not None:
                observe()
            if pidfd is not None:
                try:
                    _signal_pidfd(pidfd, signal.SIGUSR1 if requested_stop else signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 7.0
                while time.monotonic() < deadline:
                    readable, _, _ = select.select([pidfd], [], [], 0.1)
                    if readable:
                        break
                else:
                    try:
                        _signal_pidfd(pidfd, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            _terminate(compositor)
            if requested_stop:
                return 0
            if not mounted:
                print("dgx-fan graphical display: controller never became ready", file=sys.stderr)
                return 1
            return compositor.returncode or 0
        except Exception:
            if pidfd is not None:
                try:
                    _signal_pidfd(pidfd, signal.SIGTERM)
                    select.select([pidfd], [], [], 3.0)
                except ProcessLookupError:
                    pass
            raise
        finally:
            selector.close()
            listener.close()
            if connection is not None:
                connection.close()
            if pidfd is not None:
                os.close(pidfd)
            _terminate(compositor)


def _which(program: str) -> str | None:
    import shutil

    return shutil.which(program)


def _terminal(socket_name: str) -> int:
    command = shlex.join([str(PYTHON), str(Path(__file__).resolve()), "controller", socket_name])
    os.execvp(
        "lxterminal",
        ["lxterminal", "--no-remote", "--title=DGX Fan Controller", f"--command={command}"],
    )
    return 1


def _controller(socket_name: str) -> int:
    if not socket_name.startswith("@dgx-fan-"):
        raise RuntimeError("invalid graphical controller socket")
    socket_address = "\0" + socket_name[1:]
    if not CONTROLLER.is_file() or not os.access(CONTROLLER, os.X_OK) or not CONFIG.is_file():
        raise RuntimeError("controller or config missing")

    stop_signal: int | None = None

    def stop(signum: int, _frame: object) -> None:
        nonlocal stop_signal
        stop_signal = signum

    signal.signal(signal.SIGUSR1, stop)
    signal.signal(signal.SIGTERM, stop)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(socket_address)
        # Peer credentials let the supervisor target this wrapper even while
        # the hardware helper and Textual startup are still in progress.
        subprocess.run(["sudo", "-n", "/usr/local/libexec/dgx-fan-prepare-hardware"], check=True)
        if stop_signal is not None:
            return 0
        ready_reader, ready_writer = os.pipe()
        mount_reader, mount_writer = os.pipe()
        app_environment = os.environ.copy()
        app_environment["DGX_FAN_SIGNAL_READY_FD"] = str(ready_writer)
        app_environment["DGX_FAN_MOUNTED_READY_FD"] = str(mount_writer)
        try:
            app = subprocess.Popen(
                [str(CONTROLLER), "--config", str(CONFIG)],
                env=app_environment,
                pass_fds=(ready_writer, mount_writer),
            )
        finally:
            os.close(ready_writer)
            os.close(mount_writer)
        signal_ready = False
        mounted = False
        signal_pipe_open = True
        mount_pipe_open = True
        delivered = False
        try:
            while app.poll() is None:
                readers = [
                    fd
                    for fd, open_ in ((ready_reader, signal_pipe_open), (mount_reader, mount_pipe_open))
                    if open_
                ]
                readable, _, _ = select.select(readers, [], [], 0.05) if readers else ([], [], [])
                if ready_reader in readable:
                    signal_ready = os.read(ready_reader, 1) == b"1"
                    signal_pipe_open = False
                if mount_reader in readable:
                    mounted = os.read(mount_reader, 1) == b"M"
                    mount_pipe_open = False
                    if mounted:
                        connection.sendall(b"M")
                if stop_signal is not None and not delivered and (stop_signal == signal.SIGTERM or signal_ready):
                    app.send_signal(stop_signal)
                    delivered = True
                if not readers:
                    time.sleep(0.05)
            return (app.returncode or 0) if mounted else 1
        finally:
            os.close(ready_reader)
            os.close(mount_reader)
            if app.poll() is None:
                app.terminate()
                try:
                    app.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    app.kill()
                    app.wait()


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "session":
        return _session()
    if len(sys.argv) == 3 and sys.argv[1] == "terminal":
        return _terminal(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == "controller":
        return _controller(sys.argv[2])
    print("Usage: graphical_session.py session|terminal SOCKET|controller SOCKET", file=sys.stderr)
    return 64


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"dgx-fan graphical display: {error}", file=sys.stderr)
        raise SystemExit(1) from error
