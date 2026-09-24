from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _clone(tmp_path: Path, *, config: bool = True, name: str = "clone with spaces") -> Path:
    clone = tmp_path / name
    clone.mkdir()
    for file_name in ("install.sh", "uninstall.sh", "dgx-fan-control.sh", "config.example.toml"):
        source = ROOT / file_name
        target = clone / file_name
        shutil.copy2(source, target)
    if config:
        (clone / "config.toml").write_text(
            (ROOT / "config.example.toml").read_text().replace('backend = "fake"', 'backend = "raspberry-pi"')
        )
    scripts = clone / "scripts"
    scripts.mkdir()
    for file_name in ("start.sh", "display.sh", "web.sh", "graphical_session.py"):
        shutil.copy2(ROOT / "scripts" / file_name, scripts / file_name)
    virtual_bin = clone / ".venv/bin"
    virtual_bin.mkdir(parents=True)
    python = virtual_bin / "python"
    python.write_text(
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(str(ROOT / 'src'))} exec {shlex.quote(sys.executable)} \"$@\"\n"
    )
    python.chmod(0o755)
    controller = virtual_bin / "dgx-fan"
    controller.write_text("#!/bin/sh\nexit 0\n")
    controller.chmod(0o755)
    return clone


def _environment(sandbox: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DGX_FAN_INSTALL_TESTING": "1",
            "DGX_FAN_TEST_ROOT": str(sandbox),
            "DGX_FAN_TEST_UV": str(sandbox / "fake-uv"),
            "DGX_FAN_TEST_INSTALL_USER": "operator",
            "SUDO_USER": "operator",
            "USER": "operator",
            "PATH": f"{sandbox / 'usr/bin'}:{os.environ['PATH']}",
        }
    )
    return environment


def _run(
    script: Path,
    *args: str,
    sandbox: Path,
    cwd: Path | None = None,
    input_text: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = _environment(sandbox)
    environment.update(extra_env or {})
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=cwd or script.parent,
        env=environment,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def _launcher_clone(tmp_path: Path) -> tuple[Path, Path]:
    clone = _clone(tmp_path, name="launcher-clone")
    launcher = clone / "dgx-fan-control.sh"
    command_log = tmp_path / "launcher-command.log"
    scripts = clone / "scripts"
    for name, status_variable in {
        "start.sh": "DGX_FAN_FAKE_START_STATUS",
        "display.sh": "DGX_FAN_FAKE_DISPLAY_STATUS",
        "web.sh": "DGX_FAN_FAKE_WEB_STATUS",
    }.items():
        script = scripts / name
        script.write_text(
            "#!/bin/sh\n"
            "printf '%s %s\\n' \"$(basename \"$0\")\" \"$*\" >> \"$DGX_FAN_LAUNCHER_LOG\"\n"
            f"exit \"${{{status_variable}:-0}}\"\n"
        )
        script.chmod(0o755)
    controller = clone / ".venv/bin/dgx-fan"
    controller.parent.mkdir(parents=True, exist_ok=True)
    controller.write_text("#!/bin/sh\nexit 0\n")
    controller.chmod(0o755)
    sandbox = _sandbox(tmp_path)
    marker = "# Managed by dgx-fan install.sh; do not edit.\n"
    for relative in (
        "usr/local/libexec/dgx-fan-prepare-hardware",
        "usr/local/libexec/dgx-fan-display-manager",
        "usr/local/libexec/dgx-fan-display-session",
        "usr/local/libexec/dgx-fan-display-tty-acquired",
        "usr/local/libexec/dgx-fan-display-cleanup",
        "usr/local/libexec/dgx-fan-graphical-acquire",
        "etc/profile.d/dgx-fan-autostart.sh",
        "etc/sudoers.d/dgx-fan",
    ):
        managed = sandbox / relative
        managed.parent.mkdir(parents=True, exist_ok=True)
        managed.write_text("#!/bin/sh\n" + marker)
        managed.chmod(0o755)
    record = sandbox / "etc/dgx-fan-installation"
    record.write_text(marker + "version=1\n" + str(clone) + "\n")
    (sandbox / "etc/sudoers.d/dgx-fan").chmod(0o440)
    for directory in (
        sandbox / "etc",
        sandbox / "etc/profile.d",
        sandbox / "etc/sudoers.d",
        sandbox / "usr",
        sandbox / "usr/local",
        sandbox / "usr/local/libexec",
    ):
        directory.chmod(0o755)
    return launcher, command_log


def _run_launcher(
    launcher: Path, command_log: Path, responses: str, *arguments: str, **statuses: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DGX_FAN_LAUNCHER_LOG": str(command_log),
            "DGX_FAN_INSTALL_TESTING": "1",
            "DGX_FAN_TEST_ROOT": str(command_log.parent / "system-root"),
            "DGX_FAN_TEST_PREREQUISITES_READY": "1",
            "DGX_FAN_TEST_UV": str(command_log.parent / "system-root/fake-uv"),
            "DGX_FAN_TEST_INSTALL_USER": "operator",
            "PATH": f"{command_log.parent / 'system-root/usr/bin'}:{os.environ['PATH']}",
            **statuses,
        }
    )
    return subprocess.run(
        ["bash", str(launcher), *arguments],
        cwd=launcher.parent,
        env=environment,
        input=responses,
        text=True,
        capture_output=True,
        check=False,
    )


def test_interactive_launcher_terminal_starts_web_before_foreground_primary(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)

    result = _run_launcher(launcher, command_log, "1\ny\n")

    assert result.returncode == 0, result.stderr
    assert command_log.read_text().splitlines() == ["web.sh start", "start.sh "]


def test_interactive_launcher_terminal_failure_stops_only_its_web_service(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)

    result = _run_launcher(
        launcher,
        command_log,
        "terminal\nyes\n",
        DGX_FAN_FAKE_START_STATUS="7",
    )

    assert result.returncode == 7
    assert command_log.read_text().splitlines() == ["web.sh start", "start.sh ", "web.sh stop"]


def test_interactive_launcher_stop_is_non_interactive_and_orders_web_before_display(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)
    (launcher.parent / "scripts/start.sh").unlink()

    result = _run_launcher(launcher, command_log, "", "stop")

    assert result.returncode == 0, result.stderr
    assert command_log.read_text().splitlines() == ["web.sh stop", "display.sh stop"]


def test_interactive_launcher_stop_attempts_both_and_returns_first_failure(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)

    web_failure = _run_launcher(
        launcher,
        command_log,
        "",
        "stop",
        DGX_FAN_FAKE_WEB_STATUS="7",
    )

    assert web_failure.returncode == 7
    assert "could not stop the web monitor" in web_failure.stderr
    assert command_log.read_text().splitlines() == ["web.sh stop", "display.sh stop"]

    command_log.unlink()
    display_failure = _run_launcher(
        launcher,
        command_log,
        "",
        "stop",
        DGX_FAN_FAKE_DISPLAY_STATUS="9",
    )

    assert display_failure.returncode == 9
    assert "could not stop the HDMI display" in display_failure.stderr
    assert command_log.read_text().splitlines() == ["web.sh stop", "display.sh stop"]

    command_log.unlink()
    both_failures = _run_launcher(
        launcher,
        command_log,
        "",
        "stop",
        DGX_FAN_FAKE_WEB_STATUS="7",
        DGX_FAN_FAKE_DISPLAY_STATUS="9",
    )

    assert both_failures.returncode == 7
    assert "could not stop the web monitor" in both_failures.stderr
    assert "could not stop the HDMI display" in both_failures.stderr
    assert command_log.read_text().splitlines() == ["web.sh stop", "display.sh stop"]


def test_interactive_launcher_stop_attempts_available_helper_when_other_is_missing_or_not_executable(
    tmp_path: Path,
) -> None:
    launcher, command_log = _launcher_clone(tmp_path)
    web_script = launcher.parent / "scripts/web.sh"
    display_script = launcher.parent / "scripts/display.sh"

    web_script.unlink()
    missing_web = _run_launcher(launcher, command_log, "", "stop")

    assert missing_web.returncode == 1
    assert "could not stop the web monitor; script is missing or not executable" in missing_web.stderr
    assert command_log.read_text().splitlines() == ["display.sh stop"]

    command_log.unlink()
    web_script.write_text("#!/bin/sh\nprintf '%s %s\\n' \"$(basename \"$0\")\" \"$*\" >> \"$DGX_FAN_LAUNCHER_LOG\"\n")
    web_script.chmod(0o644)
    non_executable_web = _run_launcher(launcher, command_log, "", "stop")

    assert non_executable_web.returncode == 1
    assert "could not stop the web monitor; script is missing or not executable" in non_executable_web.stderr
    assert command_log.read_text().splitlines() == ["display.sh stop"]

    command_log.unlink()
    web_script.chmod(0o755)
    display_script.unlink()
    missing_display = _run_launcher(launcher, command_log, "", "stop")

    assert missing_display.returncode == 1
    assert "could not stop the HDMI display; script is missing or not executable" in missing_display.stderr
    assert command_log.read_text().splitlines() == ["web.sh stop"]

    command_log.unlink()
    display_script.write_text(
        "#!/bin/sh\n"
        "printf '%s %s\\n' \"$(basename \"$0\")\" \"$*\" >> \"$DGX_FAN_LAUNCHER_LOG\"\n"
    )
    display_script.chmod(0o644)
    non_executable_display = _run_launcher(launcher, command_log, "", "stop")

    assert non_executable_display.returncode == 1
    assert "could not stop the HDMI display; script is missing or not executable" in non_executable_display.stderr
    assert command_log.read_text().splitlines() == ["web.sh stop"]


def test_interactive_launcher_stop_with_extra_argument_has_no_side_effects(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)

    result = _run_launcher(launcher, command_log, "", "stop", "extra")

    assert result.returncode == 64
    assert "Usage: ./dgx-fan-control.sh" in result.stderr
    assert not command_log.exists()


def test_interactive_launcher_hdmi_default_web_no_and_web_failure_keeps_display(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)

    no_web = _run_launcher(launcher, command_log, "2\n\n")

    assert no_web.returncode == 0, no_web.stderr
    assert command_log.read_text().splitlines() == ["display.sh restart"]

    command_log.unlink()
    web_failure = _run_launcher(
        launcher,
        command_log,
        "hdmi\ny\n",
        DGX_FAN_FAKE_WEB_STATUS="9",
    )

    assert web_failure.returncode == 0, web_failure.stderr
    assert command_log.read_text().splitlines() == ["display.sh restart", "web.sh start"]
    assert "continuing with the selected primary display" in web_failure.stderr


def test_interactive_launcher_console_choice_transitions_existing_hdmi(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)

    result = _run_launcher(launcher, command_log, "3\n\n")

    assert result.returncode == 0, result.stderr
    assert command_log.read_text().splitlines() == ["display.sh restart-console"]


def test_interactive_launcher_display_failure_blocks_web_and_cancel_has_no_side_effects(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)

    display_failure = _run_launcher(
        launcher,
        command_log,
        "2\ny\n",
        DGX_FAN_FAKE_DISPLAY_STATUS="5",
    )

    assert display_failure.returncode == 5
    assert command_log.read_text().splitlines() == ["display.sh restart"]

    command_log.unlink()
    cancelled = _run_launcher(launcher, command_log, "h\nnot-a-choice\nq\n")

    assert cancelled.returncode == 0
    assert "Invalid choice" in cancelled.stderr
    assert not command_log.exists()


def test_interactive_launcher_web_prompt_cancel_or_eof_has_no_side_effects(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)

    cancelled = _run_launcher(launcher, command_log, "1\nq\n")

    assert cancelled.returncode == 0
    assert "Cancelled." in cancelled.stdout
    assert not command_log.exists()

    eof_after_primary = _run_launcher(launcher, command_log, "1\n")

    assert eof_after_primary.returncode == 0
    assert "Cancelled." in eof_after_primary.stdout
    assert not command_log.exists()


def test_interactive_launcher_help_requires_exactly_one_argument(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)

    help_result = _run_launcher(launcher, command_log, "", "--help")

    assert help_result.returncode == 0
    assert "Usage: ./dgx-fan-control.sh" in help_result.stdout
    assert not command_log.exists()

    for arguments in (("--help", "extra"), ("-h", "extra"), ("unknown",)):
        invalid_result = _run_launcher(launcher, command_log, "", *arguments)

        assert invalid_result.returncode == 64
        assert "Usage: ./dgx-fan-control.sh" in invalid_result.stderr
        assert not command_log.exists()


def _remove_launcher_installation(command_log: Path) -> None:
    sandbox = command_log.parent / "system-root"
    for relative in (
        "etc/dgx-fan-installation",
        "etc/profile.d/dgx-fan-autostart.sh",
        "etc/sudoers.d/dgx-fan",
        "usr/local/libexec/dgx-fan-prepare-hardware",
        "usr/local/libexec/dgx-fan-display-manager",
        "usr/local/libexec/dgx-fan-display-session",
        "usr/local/libexec/dgx-fan-display-tty-acquired",
        "usr/local/libexec/dgx-fan-display-cleanup",
        "usr/local/libexec/dgx-fan-graphical-acquire",
    ):
        (sandbox / relative).unlink(missing_ok=True)


def test_launcher_missing_install_defaults_no_and_yes_repairs_without_recursion(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)
    _remove_launcher_installation(command_log)
    sandbox = command_log.parent / "system-root"
    before = (sandbox / "command.log").read_text() if (sandbox / "command.log").exists() else ""

    declined = _run_launcher(launcher, command_log, "\n")

    assert declined.returncode == 1
    assert "no installation changes were made" in declined.stdout
    assert ((sandbox / "command.log").read_text() if (sandbox / "command.log").exists() else "") == before
    assert not command_log.exists()

    boot = sandbox / "boot/firmware/config.txt"
    boot.write_text("[all]\ndtoverlay=pwm-2chan,pin=18,pin2=19,func=2,func2=2\n")
    repaired = _run_launcher(launcher, command_log, "yes\nq\n")

    assert repaired.returncode == 0, repaired.stderr
    assert "opening the controller launcher" not in repaired.stdout
    assert repaired.stdout.count("Choose the primary TUI display") == 1
    assert (sandbox / "etc/dgx-fan-installation").read_text().splitlines()[-1] == str(launcher.parent)


def test_launcher_partial_and_other_clone_installations_require_explicit_confirmation(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)
    sandbox = command_log.parent / "system-root"
    manager = sandbox / "usr/local/libexec/dgx-fan-display-manager"
    manager.unlink()

    partial = _run_launcher(launcher, command_log, "no\n")

    assert partial.returncode == 1
    assert "no installation changes were made" in partial.stdout
    assert not manager.exists()

    manager.write_text("#!/bin/sh\n# Managed by dgx-fan install.sh; do not edit.\n")
    manager.chmod(0o755)
    record = sandbox / "etc/dgx-fan-installation"
    record.write_text("# Managed by dgx-fan install.sh; do not edit.\nversion=1\n/opt/another clone\n")
    other = _run_launcher(launcher, command_log, "\n")

    assert other.returncode == 1
    assert "no installation changes were made" in other.stdout
    assert record.read_text().splitlines()[-1] == "/opt/another clone"

    boot = sandbox / "boot/firmware/config.txt"
    boot.write_text("[all]\ndtoverlay=pwm-2chan,pin=18,pin2=19,func=2,func2=2\n")
    switched = _run_launcher(launcher, command_log, "yes\nq\n")
    assert switched.returncode == 0, switched.stderr
    assert switched.stdout.count("Choose the primary TUI display") == 1
    assert record.read_text().splitlines()[-1] == str(launcher.parent)


def test_launcher_rechecks_pending_prerequisites_after_install(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)
    _remove_launcher_installation(command_log)

    result = _run_launcher(
        launcher,
        command_log,
        "yes\n",
        DGX_FAN_TEST_PREREQUISITES_READY="0",
    )

    assert result.returncode == 1
    assert "installation completed but is not ready" in result.stderr
    assert "Choose the primary TUI display" not in result.stdout
    assert not command_log.exists()


def test_launcher_treats_install_record_path_as_data_only(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)
    sandbox = command_log.parent / "system-root"
    injected = tmp_path / "record-was-executed"
    record = sandbox / "etc/dgx-fan-installation"
    record.write_text(
        "# Managed by dgx-fan install.sh; do not edit.\n"
        "version=1\n"
        f"/opt/$(touch {injected})\n"
    )

    result = _run_launcher(launcher, command_log, "no\n")

    assert result.returncode == 1
    assert not injected.exists()
    assert record.read_text().splitlines()[-1].startswith("/opt/$(touch ")


def test_launcher_requires_safe_sudoers_metadata_before_entering_primary_menu(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)
    sudoers = command_log.parent / "system-root/etc/sudoers.d/dgx-fan"
    sudoers.chmod(0o640)
    sudo_log = tmp_path / "sudo-list.log"

    result = _run_launcher(
        launcher,
        command_log,
        "no\n",
        DGX_FAN_FAKE_SUDO_LOG=str(sudo_log),
    )

    assert result.returncode == 1
    assert "Choose the primary TUI display" not in result.stdout
    assert not sudo_log.exists()
    assert not command_log.exists()


def test_launcher_requires_both_exact_noninteractive_sudo_authorizations(tmp_path: Path) -> None:
    for denied_status in ("DGX_FAN_FAKE_SUDO_HELPER_STATUS", "DGX_FAN_FAKE_SUDO_MANAGER_STATUS"):
        case = tmp_path / denied_status
        case.mkdir()
        launcher, command_log = _launcher_clone(case)
        sudo_log = case / "sudo-list.log"

        result = _run_launcher(
            launcher,
            command_log,
            "no\n",
            DGX_FAN_FAKE_SUDO_LOG=str(sudo_log),
            **{denied_status: "1"},
        )

        assert result.returncode == 1
        assert "Choose the primary TUI display" not in result.stdout
        listings = sudo_log.read_text().splitlines()
        assert listings[0].startswith("-n -l -- ")
        assert listings[0].endswith("/usr/local/libexec/dgx-fan-prepare-hardware")
        if denied_status == "DGX_FAN_FAKE_SUDO_MANAGER_STATUS":
            assert len(listings) == 2
            assert listings[1].endswith("/usr/local/libexec/dgx-fan-display-manager start")
        else:
            assert len(listings) == 1
        assert not command_log.exists()


def test_launcher_does_not_read_root_only_sudoers_marker_when_authorization_is_valid(
    tmp_path: Path,
) -> None:
    launcher, command_log = _launcher_clone(tmp_path)
    sudoers = command_log.parent / "system-root/etc/sudoers.d/dgx-fan"
    sudoers.chmod(0o600)
    sudoers.write_text("content deliberately unavailable to the operator\n")
    sudoers.chmod(0o440)
    sudo_log = tmp_path / "sudo-list.log"

    result = _run_launcher(
        launcher,
        command_log,
        "q\n",
        DGX_FAN_FAKE_SUDO_LOG=str(sudo_log),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.count("Choose the primary TUI display") == 1
    assert [line.split()[-1] for line in sudo_log.read_text().splitlines()] == [
        str(command_log.parent / "system-root/usr/local/libexec/dgx-fan-prepare-hardware"),
        "start",
    ]


def test_launcher_stop_and_help_never_probe_or_prompt_for_installation(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)
    _remove_launcher_installation(command_log)

    stopped = _run_launcher(launcher, command_log, "", "stop")
    helped = _run_launcher(launcher, command_log, "", "--help")

    assert stopped.returncode == 0
    assert command_log.read_text().splitlines() == ["web.sh stop", "display.sh stop"]
    assert helped.returncode == 0
    assert "Install this clone" not in helped.stdout


def _sandbox(tmp_path: Path) -> Path:
    sandbox = tmp_path / "system-root"
    (sandbox / "boot/firmware").mkdir(parents=True)
    (sandbox / "boot/firmware/config.txt").write_text("# test boot configuration\n")
    fake_uv = sandbox / "fake-uv"
    fake_uv.write_text(
        "#!/bin/sh\nprintf 'uv cwd=%s args=%s\\n' \"$PWD\" \"$*\" >> \"$DGX_FAN_TEST_ROOT/command.log\"\n"
    )
    fake_uv.chmod(0o755)
    for command, body in {
        "systemctl": 'case "$1" in show) printf "not-found\\n";; esac\n',
        "systemd-run": "exit 0\n",
        "openvt": "exit 0\n",
        "chvt": "exit 0\n",
        "deallocvt": "exit 0\n",
        "fuser": "exit 1\n",
        "python3": "exit 1\n",
        "sudo": (
            'if [ -n "${DGX_FAN_FAKE_SUDO_LOG:-}" ]; then printf "%s\\n" "$*" >> "$DGX_FAN_FAKE_SUDO_LOG"; fi\n'
            'case "$*" in *dgx-fan-prepare-hardware*) exit "${DGX_FAN_FAKE_SUDO_HELPER_STATUS:-0}";; '
            '*dgx-fan-display-manager*) exit "${DGX_FAN_FAKE_SUDO_MANAGER_STATUS:-0}";; *) exit 64;; esac\n'
        ),
    }.items():
        tool = sandbox / "usr/bin" / command
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text("#!/bin/sh\nset -eu\n" + body)
        tool.chmod(0o755)
    runuser = sandbox / "usr/sbin/runuser"
    runuser.parent.mkdir(parents=True, exist_ok=True)
    runuser.write_text("#!/bin/sh\nexit 0\n")
    runuser.chmod(0o755)
    return sandbox


def test_missing_project_config_is_seeded_and_stops_before_system_changes(tmp_path: Path) -> None:
    clone = _clone(tmp_path, config=False)
    sandbox = _sandbox(tmp_path)

    result = _run(clone / "install.sh", sandbox=sandbox)

    assert result.returncode == 1
    assert (clone / "config.toml").read_text() == (clone / "config.example.toml").read_text()
    assert not (sandbox / "etc/profile.d/dgx-fan-autostart.sh").exists()
    assert not (sandbox / "etc/sudoers.d/dgx-fan").exists()
    assert "edit config.toml" in result.stderr


def test_temp_root_install_is_idempotent_and_keeps_config_in_clone(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)

    first = _run(clone / "install.sh", sandbox=sandbox)
    second = _run(clone / "install.sh", sandbox=sandbox)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    boot_config = sandbox / "boot/firmware/config.txt"
    overlay = "dtoverlay=pwm-2chan,pin=18,pin2=19,func=2,func2=2"
    assert boot_config.read_text().count(overlay) == 1
    assert "[all]" in boot_config.read_text()
    assert (clone / "config.toml").exists()
    assert not (sandbox / "etc/config.toml").exists()
    helper = sandbox / "usr/local/libexec/dgx-fan-prepare-hardware"
    manager = sandbox / "usr/local/libexec/dgx-fan-display-manager"
    session = sandbox / "usr/local/libexec/dgx-fan-display-session"
    sudoers = sandbox / "etc/sudoers.d/dgx-fan"
    hook = sandbox / "etc/profile.d/dgx-fan-autostart.sh"
    install_record = sandbox / "etc/dgx-fan-installation"
    assert helper.exists() and manager.exists() and session.exists() and sudoers.exists() and hook.exists()
    assert install_record.read_text().splitlines() == [
        "# Managed by dgx-fan install.sh; do not edit.",
        "version=1",
        str(clone),
    ]
    assert "source" not in install_record.read_text()
    assert "accepts no arguments" in helper.read_text()
    assert "/usr/local/libexec/dgx-fan-prepare-hardware" in sudoers.read_text()
    assert "/usr/local/libexec/dgx-fan-display-manager *" in sudoers.read_text()
    assert '"$openvt" -c 8 -s -w' in session.read_text()
    acquired = sandbox / "usr/local/libexec/dgx-fan-display-tty-acquired"
    cleanup = sandbox / "usr/local/libexec/dgx-fan-display-cleanup"
    assert acquired.exists() and cleanup.exists()
    assert '"$runuser" -u' in acquired.read_text()
    assert "--collect --service-type=exec --unit=dgx-fan-display" in manager.read_text()
    assert "--property=ExecStopPost=\"$cleanup\"" in manager.read_text()
    assert "/run/dgx-fan/instance.lock" not in helper.read_text()
    hook_text = hook.read_text()
    assert '"/dev/tty1"' in hook_text
    assert "SSH_CONNECTION" in hook_text
    assert "DGX_FAN_AUTOSTART_ATTEMPTED" in hook_text
    assert '"$DGX_FAN_PROJECT_ROOT/scripts/display.sh" start' in hook_text
    assert "raspi-config nonint do_boot_behaviour B2" in (sandbox / "command.log").read_text()
    assert f"uv cwd={clone}" in (sandbox / "command.log").read_text()
    assert f"--project {clone} --locked --extra raspberry-pi --no-dev" in (
        sandbox / "command.log"
    ).read_text()


def test_interactive_install_hands_off_once_and_no_launch_returns_success(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    overlay = "dtoverlay=pwm-2chan,pin=18,pin2=19,func=2,func2=2"
    (sandbox / "boot/firmware/config.txt").write_text(f"[all]\n{overlay}\n")
    test_flags = {
        "DGX_FAN_TEST_INTERACTIVE": "1",
        "DGX_FAN_TEST_GROUP_READY": "1",
        "DGX_FAN_TEST_PREREQUISITES_READY": "1",
    }

    handed_off = _run(
        clone / "install.sh",
        sandbox=sandbox,
        input_text="q\n",
        extra_env=test_flags,
    )

    assert handed_off.returncode == 0, handed_off.stderr
    assert handed_off.stdout.count("opening the controller launcher") == 1
    assert handed_off.stdout.count("Choose the primary TUI display") == 1

    no_launch = _run(
        clone / "install.sh",
        "--no-launch",
        sandbox=sandbox,
        input_text="q\n",
        extra_env=test_flags,
    )

    assert no_launch.returncode == 0, no_launch.stderr
    assert "opening the controller launcher" not in no_launch.stdout
    assert "Choose the primary TUI display" not in no_launch.stdout


def test_noninteractive_or_new_overlay_install_does_not_open_launcher(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    noninteractive = _run(clone / "install.sh", sandbox=sandbox)
    assert noninteractive.returncode == 0, noninteractive.stderr
    assert "opening the controller launcher" not in noninteractive.stdout

    second_clone = _clone(tmp_path, name="second clone")
    second_sandbox = _sandbox(tmp_path / "second sandbox")
    new_overlay = _run(
        second_clone / "install.sh",
        sandbox=second_sandbox,
        extra_env={"DGX_FAN_TEST_INTERACTIVE": "1", "DGX_FAN_TEST_GROUP_READY": "1"},
    )
    assert new_overlay.returncode == 0, new_overlay.stderr
    assert "opening the controller launcher" not in new_overlay.stdout
    assert "restart required" in new_overlay.stdout


def test_existing_system_parent_directory_mode_is_preserved(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    sudoers_directory = sandbox / "etc/sudoers.d"
    sudoers_directory.mkdir(parents=True)
    (sandbox / "etc").chmod(0o755)
    sudoers_directory.chmod(0o711)

    result = _run(clone / "install.sh", sandbox=sandbox)

    assert result.returncode == 0, result.stderr
    assert sudoers_directory.stat().st_mode & 0o777 == 0o711


def test_test_root_rejects_group_writable_parent_before_system_mutation(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    parent = sandbox / "etc/sudoers.d"
    parent.mkdir(parents=True)
    parent.chmod(0o775)

    result = _run(clone / "install.sh", sandbox=sandbox)
    assert result.returncode == 1
    assert "writable or wrong-owner parent" in result.stderr
    assert "dtoverlay=pwm-2chan" not in (sandbox / "boot/firmware/config.txt").read_text()


def test_test_root_rejects_hardlinked_managed_destination(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    manager = sandbox / "usr/local/libexec/dgx-fan-display-manager"
    alias = sandbox / "manager-alias"
    os.link(manager, alias)

    result = _run(clone / "install.sh", sandbox=sandbox)
    assert result.returncode == 1
    assert "hardlinked destination" in result.stderr


def test_conflicting_overlay_fails_before_privileged_artifacts(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    (sandbox / "boot/firmware/config.txt").write_text("dtoverlay=pwm-2chan,pin=12,pin2=13\n")

    result = _run(clone / "install.sh", sandbox=sandbox)

    assert result.returncode == 1
    assert "conflicting active pwm-2chan overlay" in result.stderr
    assert not (sandbox / "etc/sudoers.d/dgx-fan").exists()


def test_conditional_overlay_is_rejected_without_boot_or_privilege_changes(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    (sandbox / "boot/firmware/config.txt").write_text("[pi5]\ndtoverlay=pwm-2chan,pin=18,pin2=19,func=2,func2=2\n")

    result = _run(clone / "install.sh", sandbox=sandbox)

    assert result.returncode == 1
    assert "conditional under [pi5]" in result.stderr
    assert not (sandbox / "etc/sudoers.d/dgx-fan").exists()
    for helper in (
        "dgx-fan-prepare-hardware",
        "dgx-fan-display-manager",
        "dgx-fan-display-session",
        "dgx-fan-display-tty-acquired",
        "dgx-fan-display-cleanup",
    ):
        assert not (sandbox / "usr/local/libexec" / helper).exists()
    assert not (sandbox / "usr/local/libexec/dgx-fan-display-manager").exists()
    assert not (sandbox / "usr/local/libexec/dgx-fan-display-session").exists()


def test_overlay_is_appended_under_all_after_conditional_section(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    boot = sandbox / "boot/firmware/config.txt"
    boot.write_text("[none]\n# no Pi gets this\n")

    result = _run(clone / "install.sh", sandbox=sandbox)

    assert result.returncode == 0, result.stderr
    assert boot.read_text().endswith(
        "[all]\ndtoverlay=pwm-2chan,pin=18,pin2=19,func=2,func2=2\n"
    )


def test_global_and_all_overlays_are_idempotent(tmp_path: Path) -> None:
    for text in (
        "dtoverlay=pwm-2chan,pin=18,pin2=19,func=2,func2=2\n[pi4]\nfoo=bar\n",
        "[all]\ndtoverlay=pwm-2chan,pin=18,pin2=19,func=2,func2=2\n",
    ):
        case = tmp_path / str(abs(hash(text)))
        case.mkdir()
        clone = _clone(case)
        sandbox = _sandbox(case)
        boot = sandbox / "boot/firmware/config.txt"
        boot.write_text(text)
        result = _run(clone / "install.sh", sandbox=sandbox)
        assert result.returncode == 0, result.stderr
        assert boot.read_text() == text


def test_unmanaged_or_symlink_destination_fails_closed_before_overlay(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    destination = sandbox / "etc/sudoers.d/dgx-fan"
    destination.parent.mkdir(parents=True)
    destination.write_text("unmanaged\n")

    unmanaged = _run(clone / "install.sh", sandbox=sandbox)
    assert unmanaged.returncode == 1
    assert "parent directory" in unmanaged.stderr or "destination" in unmanaged.stderr
    assert "dtoverlay=pwm-2chan" not in (sandbox / "boot/firmware/config.txt").read_text()

    destination.unlink()
    target = sandbox / "other"
    target.write_text("unmanaged\n")
    destination.symlink_to(target)
    symlink = _run(clone / "install.sh", sandbox=sandbox)
    assert symlink.returncode == 1
    assert "parent directory" in symlink.stderr or "symlink destination" in symlink.stderr
    assert target.read_text() == "unmanaged\n"


def test_non_regular_destination_fails_closed(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    destination = sandbox / "etc/profile.d/dgx-fan-autostart.sh"
    destination.mkdir(parents=True)

    result = _run(clone / "install.sh", sandbox=sandbox)

    assert result.returncode == 1
    assert "destination" in result.stderr or "parent directory" in result.stderr
    assert "dtoverlay=pwm-2chan" not in (sandbox / "boot/firmware/config.txt").read_text()


def test_unsafe_destination_parent_fails_before_any_system_mutation(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    parent = sandbox / "etc/sudoers.d"
    parent.parent.mkdir(parents=True)
    parent.write_text("not a directory\n")

    result = _run(clone / "install.sh", "--reboot", sandbox=sandbox)

    assert result.returncode == 1
    assert "unsafe parent directory" in result.stderr
    assert "dtoverlay=pwm-2chan" not in (sandbox / "boot/firmware/config.txt").read_text()
    for helper in (
        "dgx-fan-prepare-hardware",
        "dgx-fan-display-manager",
        "dgx-fan-display-session",
        "dgx-fan-display-tty-acquired",
        "dgx-fan-display-cleanup",
    ):
        assert not (sandbox / "usr/local/libexec" / helper).exists()
    assert not (sandbox / "etc/profile.d/dgx-fan-autostart.sh").exists()
    command_log = (sandbox / "command.log").read_text()
    assert "usermod" not in command_log
    assert not any(line.startswith("reboot ") for line in command_log.splitlines())


def test_non_raspberry_pi_backend_fails_before_boot_or_privilege_changes(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    (clone / "config.toml").write_text((ROOT / "config.example.toml").read_text())

    result = _run(clone / "install.sh", sandbox=sandbox)

    assert result.returncode == 1
    assert "hardware.backend must be raspberry-pi" in result.stderr
    assert "dtoverlay=pwm-2chan" not in (sandbox / "boot/firmware/config.txt").read_text()
    assert not (sandbox / "etc/sudoers.d/dgx-fan").exists()


def test_fixed_hardware_paths_and_pwm_pins_fail_before_system_changes(tmp_path: Path) -> None:
    replacements = (
        ("pwm_gpio_bcm = [18, 19]", "pwm_gpio_bcm = [18, 12]"),
        ('pwm_chip_path = "/sys/class/pwm/pwmchip0"', 'pwm_chip_path = "/tmp/pwmchip0"'),
        ('gpio_chip_path = "/dev/gpiochip0"', 'gpio_chip_path = "/tmp/gpiochip0"'),
    )
    base = (ROOT / "config.example.toml").read_text().replace(
        'backend = "fake"', 'backend = "raspberry-pi"'
    )
    for index, (original, replacement) in enumerate(replacements):
        case = tmp_path / f"hardware-{index}"
        case.mkdir()
        clone = _clone(case)
        sandbox = _sandbox(case)
        content = base.replace(original, replacement)
        if content == base:
            content += f"\n{replacement}\n"
        (clone / "config.toml").write_text(content)

        result = _run(clone / "install.sh", "--reboot", sandbox=sandbox)

        assert result.returncode == 1
        assert "hardware." in result.stderr
        assert "dtoverlay=pwm-2chan" not in (sandbox / "boot/firmware/config.txt").read_text()
        assert not (sandbox / "usr/local/libexec/dgx-fan-prepare-hardware").exists()
        assert not (sandbox / "etc/sudoers.d/dgx-fan").exists()
        assert not (sandbox / "etc/profile.d/dgx-fan-autostart.sh").exists()
        command_log = (sandbox / "command.log").read_text()
        assert "usermod" not in command_log
        assert not any(line.startswith("reboot ") for line in command_log.splitlines())


def test_uv_project_argument_is_independent_of_invocation_directory(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    result = _run(clone / "install.sh", sandbox=sandbox, cwd=elsewhere)

    assert result.returncode == 0, result.stderr
    command_log = (sandbox / "command.log").read_text()
    assert f"uv cwd={elsewhere}" in command_log
    assert f"--project {clone} --locked --extra raspberry-pi --no-dev" in command_log


def _managed_artifacts(sandbox: Path) -> tuple[Path, ...]:
    return tuple(
        sandbox / relative
        for relative in (
            "etc/profile.d/dgx-fan-autostart.sh",
            "etc/sudoers.d/dgx-fan",
            "etc/dgx-fan-installation",
            "usr/local/libexec/dgx-fan-prepare-hardware",
            "usr/local/libexec/dgx-fan-display-manager",
            "usr/local/libexec/dgx-fan-display-session",
            "usr/local/libexec/dgx-fan-display-tty-acquired",
            "usr/local/libexec/dgx-fan-display-cleanup",
        )
    )


def test_uninstall_confirmation_defaults_no_handles_invalid_and_yes_proceeds(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0

    for response in ("\n", ""):
        cancelled = _run(clone / "uninstall.sh", sandbox=sandbox, input_text=response)
        assert cancelled.returncode == 0
        assert "cancelled; no files were changed" in cancelled.stdout
        assert all(path.exists() for path in _managed_artifacts(sandbox))

    removed = _run(clone / "uninstall.sh", sandbox=sandbox, input_text="maybe\nyes\n")
    assert removed.returncode == 0, removed.stderr
    assert "Invalid choice" in removed.stderr
    assert all(not path.exists() for path in _managed_artifacts(sandbox))


def test_uninstall_stops_web_then_display_and_preserves_everything_on_web_failure(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    systemctl = sandbox / "usr/bin/systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$DGX_FAN_TEST_ROOT/stop-order.log\"\n"
        "if [ \"$1\" = --user ] && [ \"$2\" = show ]; then exit 7; fi\n"
        "case \"$1\" in show) printf 'not-found\\n';; esac\n"
    )
    systemctl.chmod(0o755)

    result = _run(clone / "uninstall.sh", "--yes", sandbox=sandbox)

    assert result.returncode == 1
    entries = (sandbox / "stop-order.log").read_text().splitlines()
    assert entries[0].startswith("--user show")
    assert entries[1] == "show --property=LoadState --value dgx-fan-graphical.service"
    assert entries[2] == "show --property=LoadState --value dgx-fan-display.service"
    assert not any("stop dgx-fan-display.service" in entry for entry in entries[1:])
    assert all(path.exists() for path in _managed_artifacts(sandbox))


def test_uninstall_partial_display_integration_fails_closed_without_deleting(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    (sandbox / "usr/local/libexec/dgx-fan-display-cleanup").unlink()

    result = _run(clone / "uninstall.sh", "--yes", sandbox=sandbox)

    assert result.returncode == 1
    assert "partial or untrusted" in result.stderr
    for path in _managed_artifacts(sandbox):
        if path.name != "dgx-fan-display-cleanup":
            assert path.exists()


def test_reboot_is_deferred_until_success_and_uninstall_preserves_config_and_overlay(
    tmp_path: Path,
) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)

    result = _run(clone / "install.sh", "--reboot", sandbox=sandbox)
    assert result.returncode == 0, result.stderr
    log = (sandbox / "command.log").read_text()
    assert "usermod -a -G gpio operator" in log
    assert "reboot" in log

    removed = _run(clone / "uninstall.sh", "--yes", sandbox=sandbox)
    assert removed.returncode == 0, removed.stderr
    assert (clone / "config.toml").exists()
    assert "dtoverlay=pwm-2chan" in (sandbox / "boot/firmware/config.txt").read_text()
    assert not (sandbox / "etc/profile.d/dgx-fan-autostart.sh").exists()
    assert not (sandbox / "etc/sudoers.d/dgx-fan").exists()
    assert not (sandbox / "usr/local/libexec/dgx-fan-prepare-hardware").exists()


def test_uninstall_preserves_unmanaged_or_symlink_destinations(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    profile = sandbox / "etc/profile.d/dgx-fan-autostart.sh"
    profile.parent.mkdir(parents=True)
    profile.write_text("operator content\n")
    helper = sandbox / "usr/local/libexec/dgx-fan-prepare-hardware"
    helper.parent.mkdir(parents=True)
    helper_target = sandbox / "helper-target"
    helper_target.write_text("keep\n")
    helper.symlink_to(helper_target)

    result = _run(clone / "uninstall.sh", "--yes", sandbox=sandbox)

    assert result.returncode == 1
    assert profile.read_text() == "operator content\n"
    assert helper.is_symlink() and helper_target.read_text() == "keep\n"
    assert "preserving unmanaged" in result.stderr
    assert "preserving unsafe" in result.stderr


def test_uninstall_cleanup_failure_preserves_all_managed_artifacts(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/chvt", "exit 1\n")
    marker = sandbox / "run/dgx-fan-display-tty8"
    marker.parent.mkdir()
    marker.write_text("tty2\n")
    result = _run(clone / "uninstall.sh", "--yes", sandbox=sandbox)
    assert result.returncode == 1
    assert "stop/cleanup failed" in result.stderr
    for path in (
        sandbox / "etc/profile.d/dgx-fan-autostart.sh",
        sandbox / "etc/sudoers.d/dgx-fan",
        sandbox / "usr/local/libexec/dgx-fan-prepare-hardware",
        sandbox / "usr/local/libexec/dgx-fan-display-manager",
        sandbox / "usr/local/libexec/dgx-fan-display-session",
        sandbox / "usr/local/libexec/dgx-fan-display-tty-acquired",
        sandbox / "usr/local/libexec/dgx-fan-display-cleanup",
    ):
        assert path.exists()


def test_uninstall_removes_all_artifacts_when_stop_reports_missing_unit(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/systemctl", 'case "$1" in stop) exit 1;; show) printf "not-found\\n";; esac\n')

    result = _run(clone / "uninstall.sh", "--yes", sandbox=sandbox)
    assert result.returncode == 0, result.stderr
    for path in (
        sandbox / "etc/profile.d/dgx-fan-autostart.sh",
        sandbox / "etc/sudoers.d/dgx-fan",
        sandbox / "usr/local/libexec/dgx-fan-prepare-hardware",
        sandbox / "usr/local/libexec/dgx-fan-display-manager",
        sandbox / "usr/local/libexec/dgx-fan-display-session",
        sandbox / "usr/local/libexec/dgx-fan-display-tty-acquired",
        sandbox / "usr/local/libexec/dgx-fan-display-cleanup",
    ):
        assert not path.exists()


def test_uninstall_uses_root_marker_inspection_for_production_sudoers() -> None:
    source = (ROOT / "uninstall.sh").read_text()

    assert 'marker_check=( run_root grep -Fxq "$MARKER" "$path" )' in source
    assert 'can_read=( run_root test -r "$path" )' in source
    assert 'display_helpers_managed=true' in source
    assert 'run_root grep -Fxq "$MARKER" "$display_manager"' in source
    assert 'run_root "$display_manager" stop' in source


def test_uninstall_accepts_legacy_install_without_graphical_helper(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    graphical = sandbox / "usr/local/libexec/dgx-fan-graphical-acquire"
    graphical.unlink()

    result = _run(clone / "uninstall.sh", "--yes", sandbox=sandbox)

    assert result.returncode == 0, result.stderr
    assert all(not path.exists() for path in _managed_artifacts(sandbox))


def test_start_dry_run_uses_fixed_helper_and_clone_local_configuration() -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/start.sh"), "--dry-run"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "sudo -n /usr/local/libexec/dgx-fan-prepare-hardware" in result.stdout
    assert "flock" not in result.stdout
    assert f"{ROOT}/.venv/bin/dgx-fan --config {ROOT}/config.toml" in result.stdout


def test_direct_runtime_scripts_live_only_under_scripts_and_resolve_clone_from_any_cwd(
    tmp_path: Path,
) -> None:
    for name in ("start.sh", "display.sh", "web.sh"):
        assert not (ROOT / name).exists()
        assert os.access(ROOT / "scripts" / name, os.X_OK)

    clone = _clone(tmp_path)
    elsewhere = tmp_path / "other cwd"
    elsewhere.mkdir()
    result = subprocess.run(
        ["bash", str(clone / "scripts/start.sh"), "--dry-run"],
        cwd=elsewhere,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    escaped_clone = str(clone).replace(" ", "\\ ")
    assert f"{escaped_clone}/.venv/bin/dgx-fan --config {escaped_clone}/config.toml" in result.stdout


def test_display_launcher_has_closed_actions_and_fixed_bridge() -> None:
    source = (ROOT / "scripts/display.sh").read_text()
    assert "start|restart|start-console|restart-console|stop|status" in source
    assert "sudo -n \"$MANAGER\" \"$1\"" in source
    assert "systemd-run" not in source
    result = subprocess.run(["bash", str(ROOT / "scripts/display.sh"), "unknown"], text=True, capture_output=True, check=False)
    assert result.returncode == 64


def test_generated_display_manager_rejects_untrusted_actions(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    manager = sandbox / "usr/local/libexec/dgx-fan-display-manager"
    result = subprocess.run(["sh", str(manager), "unsafe"], text=True, capture_output=True, check=False)
    assert result.returncode == 64
    assert "Usage: dgx-fan-display-manager" in result.stderr


def test_display_bridge_uses_transient_no_force_tty8_and_no_restart_policy(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    manager = (sandbox / "usr/local/libexec/dgx-fan-display-manager").read_text()
    session = (sandbox / "usr/local/libexec/dgx-fan-display-session").read_text()

    assert "--collect --service-type=exec --unit=dgx-fan-display" in manager
    assert manager.count("--property=KillSignal=SIGUSR1") == 2
    assert "Restart=" not in manager
    assert '"$openvt" -c 8 -s -w' in session
    assert "-c 8 -s -w -f" not in session
    assert "dgx-fan-display-tty-acquired" in session


def test_graphical_manager_uses_unprivileged_pam_session_and_root_cleanup(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/systemctl", 'case "$1" in show) printf "not-found\\n";; is-active) printf "inactive\\n";; esac\n')
    _fake_command(sandbox / "usr/bin/systemd-run", 'printf "%s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/graphical-unit.log"\n')
    _fake_command(sandbox / "usr/bin/chvt", 'printf "%s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/graphical-vt.log"\n')
    _fake_command(sandbox / "usr/bin/labwc", "exit 0\n")
    _fake_command(sandbox / "usr/bin/lxterminal", "exit 0\n")
    (sandbox / "run").mkdir()
    active = sandbox / "sys/class/tty/tty0/active"
    active.parent.mkdir(parents=True)
    active.write_text("tty3\n")
    manager = sandbox / "usr/local/libexec/dgx-fan-display-manager"

    result = subprocess.run(["sh", str(manager), "start"], env=_environment(sandbox), text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr
    assert (sandbox / "run/dgx-fan-display-tty8").read_text() == "tty3\n"
    assert (sandbox / "graphical-vt.log").read_text().strip() == "8"
    unit = (sandbox / "graphical-unit.log").read_text()
    assert "--unit=dgx-fan-graphical" in unit
    assert "--service-type=notify" in unit
    assert "--property=NotifyAccess=main" in unit
    assert "--property=TimeoutStartSec=20s" in unit
    assert "--property=User=operator" in unit
    assert "--property=PAMName=login" in unit
    assert "--property=TTYPath=/dev/tty8" in unit
    assert "--property=StandardInput=tty-force" in unit
    assert "--property=KillMode=mixed" in unit
    assert "--property=ExecStopPost=+" + str(sandbox / "usr/local/libexec/dgx-fan-display-cleanup") in unit
    assert str(clone / "scripts/graphical_session.py") in unit


def test_graphical_startup_failure_reports_error_and_releases_own_vt(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/systemctl", 'case "$1" in show) printf "not-found\\n";; is-active) printf "inactive\\n";; esac\n')
    _fake_command(sandbox / "usr/bin/systemd-run", "exit 7\n")
    _fake_command(sandbox / "usr/bin/chvt", "exit 0\n")
    _fake_command(sandbox / "usr/bin/deallocvt", "exit 0\n")
    _fake_command(sandbox / "usr/bin/labwc", "exit 0\n")
    _fake_command(sandbox / "usr/bin/lxterminal", "exit 0\n")
    (sandbox / "run").mkdir()
    active = sandbox / "sys/class/tty/tty0/active"
    active.parent.mkdir(parents=True)
    active.write_text("tty3\n")
    manager = sandbox / "usr/local/libexec/dgx-fan-display-manager"

    result = subprocess.run(["sh", str(manager), "start"], env=_environment(sandbox), text=True, capture_output=True, check=False)

    assert result.returncode != 0
    assert not (sandbox / "run/dgx-fan-display-tty8").exists()


def _fake_command(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o755)


def test_fake_vt_lifecycle_runs_cleanup_only_after_tty_marker(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    log = sandbox / "lifecycle.log"
    _fake_command(sandbox / "usr/sbin/runuser", 'printf "runuser %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/lifecycle.log"\n')
    _fake_command(sandbox / "usr/bin/openvt", 'printf "openvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/lifecycle.log"\nwhile [ "$1" != "--" ]; do shift; done\nshift\nexec "$@"\n')
    _fake_command(sandbox / "usr/bin/chvt", 'printf "chvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/lifecycle.log"\n')
    _fake_command(sandbox / "usr/bin/deallocvt", 'printf "deallocvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/lifecycle.log"\n')
    (sandbox / "run").mkdir()
    active_tty = sandbox / "sys/class/tty/tty0/active"
    active_tty.parent.mkdir(parents=True)
    active_tty.write_text("tty3\n")
    session = sandbox / "usr/local/libexec/dgx-fan-display-session"
    cleanup = sandbox / "usr/local/libexec/dgx-fan-display-cleanup"
    environment = _environment(sandbox)

    started = subprocess.run(["sh", str(session)], env=environment, text=True, capture_output=True, check=False)
    assert started.returncode == 0, started.stderr
    marker = sandbox / "run/dgx-fan-display-tty8"
    assert marker.exists()
    cleaned = subprocess.run(["sh", str(cleanup)], env=environment, text=True, capture_output=True, check=False)
    assert cleaned.returncode == 0, cleaned.stderr
    assert not marker.exists()
    assert log.read_text().splitlines() == [
        "openvt -c 8 -s -w -- " + str(sandbox / "usr/local/libexec/dgx-fan-display-tty-acquired") + " operator " + str(clone / "scripts/start.sh") + " tty3",
        "runuser -u operator -- " + str(clone / "scripts/start.sh"),
        "chvt 3",
        "deallocvt 8",
    ]
    assert not (sandbox / "dev/tty8").exists()

    log.unlink()
    no_marker_cleanup = subprocess.run(["sh", str(cleanup)], env=environment, text=True, capture_output=True, check=False)
    assert no_marker_cleanup.returncode == 0
    assert not log.exists()
    marker.write_text("tty4\n")
    active_tty.write_text("tty4\n")
    _fake_command(sandbox / "usr/bin/chvt", "exit 1\n")
    failed_cleanup = subprocess.run(["sh", str(cleanup)], env=environment, text=True, capture_output=True, check=False)
    assert failed_cleanup.returncode == 1
    assert marker.read_text() == "tty4\n"
    _fake_command(sandbox / "usr/bin/chvt", 'printf "chvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/lifecycle.log"\n')
    retried_cleanup = subprocess.run(["sh", str(cleanup)], env=environment, text=True, capture_output=True, check=False)
    assert retried_cleanup.returncode == 0
    assert not marker.exists()


def test_tty8_acquisition_marks_native_digits_only_after_font_success(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    log = sandbox / "font.log"
    font = sandbox / "usr/share/consolefonts/Lat15-TerminusBold20x10.psf.gz"
    font.parent.mkdir(parents=True)
    font.write_bytes(b"font")
    _fake_command(sandbox / "usr/bin/setfont", 'printf "setfont %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/font.log"\n')
    _fake_command(
        sandbox / "usr/sbin/runuser",
        'printf "runuser marker=%s %s\\n" "${DGX_FAN_TEXTUAL_TTY8_FONT:-}" "$*" >> "$DGX_FAN_TEST_ROOT/font.log"\n',
    )
    (sandbox / "run").mkdir()
    acquired = sandbox / "usr/local/libexec/dgx-fan-display-tty-acquired"

    result = subprocess.run(
        ["sh", str(acquired), "operator", str(clone / "scripts/start.sh"), "tty3"],
        env=_environment(sandbox), text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        f"setfont -C /dev/tty8 {font}",
        f"runuser marker=1 -u operator -- {clone / 'scripts/start.sh'}",
    ]


def test_tty8_acquisition_font_failures_warn_and_keep_plain_fallback(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    log = sandbox / "font-fallback.log"
    _fake_command(
        sandbox / "usr/sbin/runuser",
        'printf "marker=%s\\n" "${DGX_FAN_TEXTUAL_TTY8_FONT:-}" >> "$DGX_FAN_TEST_ROOT/font-fallback.log"\n',
    )
    (sandbox / "run").mkdir()
    acquired = sandbox / "usr/local/libexec/dgx-fan-display-tty-acquired"
    inherited = _environment(sandbox)
    inherited["DGX_FAN_TEXTUAL_TTY8_FONT"] = "1"
    missing = subprocess.run(
        ["sh", str(acquired), "operator", str(clone / "scripts/start.sh"), "tty3"],
        env=inherited, text=True, capture_output=True, check=False,
    )
    assert missing.returncode == 0 and "setfont unavailable" in missing.stderr
    assert log.read_text() == "marker=\n"

    marker = sandbox / "run/dgx-fan-display-tty8"
    marker.unlink()
    font = sandbox / "usr/share/consolefonts/Lat15-TerminusBold20x10.psf.gz"
    font.parent.mkdir(parents=True)
    font.write_bytes(b"font")
    _fake_command(sandbox / "usr/bin/setfont", "exit 1\n")
    failed = subprocess.run(
        ["sh", str(acquired), "operator", str(clone / "scripts/start.sh"), "tty3"],
        env=_environment(sandbox), text=True, capture_output=True, check=False,
    )
    assert failed.returncode == 0 and "unable to set tty8 font" in failed.stderr
    assert log.read_text().splitlines() == ["marker=", "marker="]


def test_cleanup_recovers_selection_after_failed_normal_dealloc(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/chvt", 'printf "chvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/retry.log"\n')
    _fake_command(sandbox / "usr/bin/deallocvt", 'printf "deallocvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/retry.log"\nif [ ! -e "$DGX_FAN_TEST_ROOT/dealloc-once" ]; then : > "$DGX_FAN_TEST_ROOT/dealloc-once"; exit 1; fi\n')
    marker = sandbox / "run/dgx-fan-display-tty8"
    marker.parent.mkdir()
    marker.write_text("tty3\n")
    active_tty = sandbox / "sys/class/tty/tty0/active"
    active_tty.parent.mkdir(parents=True)
    active_tty.write_text("tty3\n")
    return_console = sandbox / "dev/tty3"
    return_console.parent.mkdir()
    return_console.write_text("")
    _fake_command(sandbox / "usr/bin/fuser", "exit 1\n")
    _fake_command(sandbox / "usr/bin/python3", 'printf "python3 %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/retry.log"\n')
    cleanup = sandbox / "usr/local/libexec/dgx-fan-display-cleanup"

    result = subprocess.run(["sh", str(cleanup)], env=_environment(sandbox), text=True, capture_output=True, check=False)
    assert result.returncode == 0
    assert not marker.exists()
    assert (sandbox / "retry.log").read_text().splitlines()[:2] == ["chvt 3", "deallocvt 8"]
    assert "python3 -I -S -c" in (sandbox / "retry.log").read_text()
    assert (sandbox / "retry.log").read_text().splitlines()[-1] == "deallocvt 8"


def test_cleanup_retains_marker_when_dealloc_retry_fails(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/chvt", 'printf "chvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/fail.log"\n')
    _fake_command(sandbox / "usr/bin/deallocvt", 'printf "deallocvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/fail.log"\nif [ -e "$DGX_FAN_TEST_ROOT/dealloc-failed-once" ]; then\n    [ "$(od -An -tx1 "$DGX_FAN_TEST_ROOT/dev/tty8" | tr -d " \\n")" = "0d" ] || exit 99\n    printf "%s\\n" "second dealloc failed" >&2\nfi\n: > "$DGX_FAN_TEST_ROOT/dealloc-failed-once"\nexit 1\n')
    marker = sandbox / "run/dgx-fan-display-tty8"
    marker.parent.mkdir()
    marker.write_text("tty3\n")
    active_tty = sandbox / "sys/class/tty/tty0/active"
    active_tty.parent.mkdir(parents=True)
    active_tty.write_text("tty3\n")
    (sandbox / "dev").mkdir()
    (sandbox / "dev/tty3").write_text("")
    _fake_command(sandbox / "usr/bin/fuser", "exit 1\n")
    _fake_command(sandbox / "usr/bin/python3", "exit 0\n")
    cleanup = sandbox / "usr/local/libexec/dgx-fan-display-cleanup"

    result = subprocess.run(["sh", str(cleanup)], env=_environment(sandbox), text=True, capture_output=True, check=False)
    assert result.returncode == 1
    assert marker.read_text() == "tty3\n"
    assert (sandbox / "fail.log").read_text().splitlines() == ["chvt 3", "deallocvt 8", "deallocvt 8"]
    assert result.stderr


def test_cleanup_retains_marker_when_selection_recovery_cannot_open_return_console(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/chvt", 'printf "chvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/write-fail.log"\n')
    _fake_command(sandbox / "usr/bin/deallocvt", 'printf "deallocvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/write-fail.log"\nexit 1\n')
    marker = sandbox / "run/dgx-fan-display-tty8"
    marker.parent.mkdir()
    marker.write_text("tty3\n")
    active_tty = sandbox / "sys/class/tty/tty0/active"
    active_tty.parent.mkdir(parents=True)
    active_tty.write_text("tty3\n")
    _fake_command(sandbox / "usr/bin/fuser", "exit 1\n")
    _fake_command(sandbox / "usr/bin/python3", "exit 1\n")
    cleanup = sandbox / "usr/local/libexec/dgx-fan-display-cleanup"

    result = subprocess.run(["sh", str(cleanup)], env=_environment(sandbox), text=True, capture_output=True, check=False)
    assert result.returncode == 1
    assert marker.read_text() == "tty3\n"
    assert (sandbox / "write-fail.log").read_text().splitlines() == ["chvt 3", "deallocvt 8"]
    assert result.stderr


def test_cleanup_rejects_malformed_or_tty8_return_marker_without_deallocating(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    marker = sandbox / "run/dgx-fan-display-tty8"
    marker.parent.mkdir()
    cleanup = sandbox / "usr/local/libexec/dgx-fan-display-cleanup"
    _fake_command(sandbox / "usr/bin/deallocvt", 'printf dealloc >> "$DGX_FAN_TEST_ROOT/dealloc.log"\n')

    for value in ("tty8\n", "tty64\n", "tty3oops\n"):
        marker.write_text(value)
        result = subprocess.run(["sh", str(cleanup)], env=_environment(sandbox), text=True, capture_output=True, check=False)
        assert result.returncode == 1
        assert marker.read_text() == value
    assert not (sandbox / "dealloc.log").exists()


def test_cleanup_retains_marker_when_tty8_is_occupied(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    marker = sandbox / "run/dgx-fan-display-tty8"
    marker.parent.mkdir()
    marker.write_text("tty3\n")
    active_tty = sandbox / "sys/class/tty/tty0/active"
    active_tty.parent.mkdir(parents=True)
    active_tty.write_text("tty3\n")
    _fake_command(sandbox / "usr/bin/chvt", "exit 0\n")
    _fake_command(sandbox / "usr/bin/deallocvt", "exit 1\n")
    _fake_command(sandbox / "usr/bin/fuser", "printf '123' >&2\nexit 0\n")
    cleanup = sandbox / "usr/local/libexec/dgx-fan-display-cleanup"

    result = subprocess.run(["sh", str(cleanup)], env=_environment(sandbox), text=True, capture_output=True, check=False)
    assert result.returncode == 1
    assert marker.read_text() == "tty3\n"
    assert "still occupied" in result.stderr


def test_fake_manager_refuses_active_and_orders_restart(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/systemctl", 'printf "systemctl %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/manager.log"\ncase "$1" in is-active) printf "%s\\n" "${DGX_TEST_UNIT_STATE:-inactive}";; show) if [ "$4" = dgx-fan-graphical.service ]; then printf "not-found\\n"; elif [ ! -e "$DGX_FAN_TEST_ROOT/show-seen" ]; then : > "$DGX_FAN_TEST_ROOT/show-seen"; printf "loaded\\n"; else printf "not-found\\n"; fi;; esac\n')
    _fake_command(sandbox / "usr/bin/systemd-run", 'printf "systemd-run %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/manager.log"\n')
    manager = sandbox / "usr/local/libexec/dgx-fan-display-manager"
    environment = _environment(sandbox)
    environment["DGX_TEST_UNIT_STATE"] = "active"
    active = subprocess.run(["sh", str(manager), "start"], env=environment, text=True, capture_output=True, check=False)
    assert active.returncode == 1 and "already active" in active.stderr
    (sandbox / "manager.log").unlink()
    environment["DGX_TEST_UNIT_STATE"] = "inactive"
    restarted = subprocess.run(["sh", str(manager), "restart-console"], env=environment, text=True, capture_output=True, check=False)
    assert restarted.returncode == 0, restarted.stderr
    entries = (sandbox / "manager.log").read_text().splitlines()
    assert entries[:4] == [
        "systemctl show --property=LoadState --value dgx-fan-graphical.service",
        "systemctl show --property=LoadState --value dgx-fan-display.service",
        "systemctl stop dgx-fan-display.service",
        "systemctl show --property=LoadState --value dgx-fan-display.service",
    ]
    assert entries[4] == "systemctl reset-failed dgx-fan-display.service"
    assert "--property=TimeoutStopSec=10s" in entries[-1]
    assert "--property=KillSignal=SIGUSR1" in entries[-1]
    assert "--property=ExecStopPost=" + str(sandbox / "usr/local/libexec/dgx-fan-display-cleanup") in entries[-1]


def test_fake_manager_covers_stale_start_refusals_stop_and_status(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/systemctl", 'printf "systemctl %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/manager-coverage.log"\ncase "$1" in is-active) printf "%s\\n" "${DGX_TEST_UNIT_STATE:-inactive}";; show) printf "not-found\\n";; esac\n')
    _fake_command(sandbox / "usr/bin/systemd-run", 'printf "systemd-run %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/manager-coverage.log"\n')
    manager = sandbox / "usr/local/libexec/dgx-fan-display-manager"
    environment = _environment(sandbox)
    for state in ("inactive", "failed"):
        environment["DGX_TEST_UNIT_STATE"] = state
        result = subprocess.run(["sh", str(manager), "start-console"], env=environment, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
    for state in ("activating", "deactivating", "reloading"):
        environment["DGX_TEST_UNIT_STATE"] = state
        result = subprocess.run(["sh", str(manager), "start-console"], env=environment, text=True, capture_output=True, check=False)
        assert result.returncode == 1
        assert f"already {state}" in result.stderr
    for action in ("stop", "status"):
        result = subprocess.run(["sh", str(manager), action], env=environment, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
    entries = (sandbox / "manager-coverage.log").read_text()
    assert entries.count("systemd-run ") == 2
    assert "systemctl stop dgx-fan-display.service" not in entries
    assert "systemctl status --no-pager dgx-fan-display.service" in entries


def test_fake_manager_unload_timeout_prevents_cleanup_or_relaunch(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/systemctl", 'case "$1" in is-active) printf "inactive\\n";; show) printf "loaded\\n";; esac\n')
    manager = sandbox / "usr/local/libexec/dgx-fan-display-manager"
    result = subprocess.run(["sh", str(manager), "start"], env=_environment(sandbox), text=True, capture_output=True, check=False)
    assert result.returncode == 1
    assert "timed out waiting" in result.stderr


def test_fake_manager_restart_cleans_stale_marker_in_one_action(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/systemctl", 'case "$1" in show) printf "not-found\\n";; esac\n')
    _fake_command(sandbox / "usr/bin/systemd-run", "exit 0\n")
    _fake_command(sandbox / "usr/bin/chvt", "exit 0\n")
    _fake_command(sandbox / "usr/bin/deallocvt", "exit 0\n")
    marker = sandbox / "run/dgx-fan-display-tty8"
    marker.parent.mkdir()
    marker.write_text("tty2\n")
    active_tty = sandbox / "sys/class/tty/tty0/active"
    active_tty.parent.mkdir(parents=True)
    active_tty.write_text("tty2\n")
    manager = sandbox / "usr/local/libexec/dgx-fan-display-manager"
    result = subprocess.run(["sh", str(manager), "restart-console"], env=_environment(sandbox), text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert not marker.exists()


def test_generated_helper_rejects_arguments_before_touching_real_pwm(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    helper = sandbox / "usr/local/libexec/dgx-fan-prepare-hardware"

    assert helper.read_text().startswith("#!/bin/sh\n# Managed by dgx-fan install.sh; do not edit.\n")

    result = subprocess.run(["sh", str(helper), "unexpected"], text=True, capture_output=True, check=False)

    assert result.returncode == 64
    assert "accepts no arguments" in result.stderr


def test_generated_hardware_helper_has_no_writable_filesystem_lock(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    helper = (sandbox / "usr/local/libexec/dgx-fan-prepare-hardware").read_text()

    assert "/run/dgx-fan" not in helper


def test_reboot_is_not_attempted_when_configuration_validation_fails(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    (clone / "config.toml").write_text((ROOT / "config.example.toml").read_text())

    result = _run(clone / "install.sh", "--reboot", sandbox=sandbox)

    assert result.returncode == 1
    command_log = (sandbox / "command.log").read_text()
    assert "usermod" not in command_log
    assert not any(line.startswith("reboot ") for line in command_log.splitlines())


def test_project_newline_path_fails_before_clone_or_system_mutation(tmp_path: Path) -> None:
    clone = _clone(tmp_path, config=False, name="clone\nnewline")
    sandbox = _sandbox(tmp_path)

    result = _run(clone / "install.sh", sandbox=sandbox)

    assert result.returncode == 1
    assert "project path must not contain a newline" in result.stderr
    assert not (clone / "config.toml").exists()
    assert (sandbox / "boot/firmware/config.txt").read_text() == "# test boot configuration\n"
    assert not (sandbox / "etc").exists()


def test_test_root_symlink_is_rejected(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    actual = _sandbox(tmp_path)
    linked = tmp_path / "linked-root"
    linked.symlink_to(actual)
    environment = _environment(actual)
    environment["DGX_FAN_TEST_ROOT"] = str(linked)

    result = subprocess.run(
        ["bash", str(clone / "install.sh"), "--dry-run"],
        cwd=clone,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "real directory" in result.stderr


def test_test_root_override_is_rejected_without_explicit_test_mode(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    environment = os.environ.copy()
    environment["DGX_FAN_TEST_ROOT"] = str(sandbox)
    environment["SUDO_USER"] = "operator"
    environment["USER"] = "operator"

    result = subprocess.run(
        ["bash", str(clone / "install.sh"), "--dry-run"],
        cwd=clone,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "test-only" in result.stderr
