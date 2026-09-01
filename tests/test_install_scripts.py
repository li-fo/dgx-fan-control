from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _clone(tmp_path: Path, *, config: bool = True, name: str = "clone with spaces") -> Path:
    clone = tmp_path / name
    clone.mkdir()
    for file_name in ("install.sh", "start.sh", "display.sh", "uninstall.sh", "config.example.toml"):
        source = ROOT / file_name
        target = clone / file_name
        shutil.copy2(source, target)
    if config:
        (clone / "config.toml").write_text(
            (ROOT / "config.example.toml").read_text().replace('backend = "fake"', 'backend = "raspberry-pi"')
        )
    (clone / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
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
        }
    )
    return environment


def _run(
    script: Path, *args: str, sandbox: Path, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=cwd or script.parent,
        env=_environment(sandbox),
        text=True,
        capture_output=True,
        check=False,
    )


def _launcher_clone(tmp_path: Path) -> tuple[Path, Path]:
    clone = tmp_path / "launcher-clone"
    clone.mkdir()
    launcher = clone / "dgx-fan-control.sh"
    shutil.copy2(ROOT / "dgx-fan-control.sh", launcher)
    command_log = tmp_path / "launcher-command.log"
    for name, status_variable in {
        "start.sh": "DGX_FAN_FAKE_START_STATUS",
        "display.sh": "DGX_FAN_FAKE_DISPLAY_STATUS",
        "web.sh": "DGX_FAN_FAKE_WEB_STATUS",
    }.items():
        script = clone / name
        script.write_text(
            "#!/bin/sh\n"
            "printf '%s %s\\n' \"$(basename \"$0\")\" \"$*\" >> \"$DGX_FAN_LAUNCHER_LOG\"\n"
            f"exit \"${{{status_variable}:-0}}\"\n"
        )
        script.chmod(0o755)
    return launcher, command_log


def _run_launcher(
    launcher: Path, command_log: Path, responses: str, *arguments: str, **statuses: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update({"DGX_FAN_LAUNCHER_LOG": str(command_log), **statuses})
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


def test_interactive_launcher_hdmi_default_web_no_and_web_failure_keeps_display(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)

    no_web = _run_launcher(launcher, command_log, "2\n\n")

    assert no_web.returncode == 0, no_web.stderr
    assert command_log.read_text().splitlines() == ["display.sh start"]

    command_log.unlink()
    web_failure = _run_launcher(
        launcher,
        command_log,
        "hdmi\ny\n",
        DGX_FAN_FAKE_WEB_STATUS="9",
    )

    assert web_failure.returncode == 0, web_failure.stderr
    assert command_log.read_text().splitlines() == ["display.sh start", "web.sh start"]
    assert "continuing with the selected primary display" in web_failure.stderr


def test_interactive_launcher_display_failure_blocks_web_and_cancel_has_no_side_effects(tmp_path: Path) -> None:
    launcher, command_log = _launcher_clone(tmp_path)

    display_failure = _run_launcher(
        launcher,
        command_log,
        "2\ny\n",
        DGX_FAN_FAKE_DISPLAY_STATUS="5",
    )

    assert display_failure.returncode == 5
    assert command_log.read_text().splitlines() == ["display.sh start"]

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
    }.items():
        tool = sandbox / "usr/bin" / command
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text("#!/bin/sh\nset -eu\n" + body)
        tool.chmod(0o755)
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
    assert helper.exists() and manager.exists() and session.exists() and sudoers.exists() and hook.exists()
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
    assert '"$DGX_FAN_PROJECT_ROOT/display.sh" start' in hook_text
    assert "raspi-config nonint do_boot_behaviour B2" in (sandbox / "command.log").read_text()
    assert f"uv cwd={clone}" in (sandbox / "command.log").read_text()
    assert f"--project {clone} --locked --extra raspberry-pi --no-dev" in (
        sandbox / "command.log"
    ).read_text()


def test_existing_system_parent_directory_mode_is_preserved(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    sudoers_directory = sandbox / "etc/sudoers.d"
    sudoers_directory.mkdir(parents=True)
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


def test_start_dry_run_uses_fixed_helper_and_clone_local_configuration() -> None:
    result = subprocess.run(
        ["bash", str(ROOT / "start.sh"), "--dry-run"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "sudo -n /usr/local/libexec/dgx-fan-prepare-hardware" in result.stdout
    assert "flock" not in result.stdout
    assert f"{ROOT}/.venv/bin/dgx-fan --config {ROOT}/config.toml" in result.stdout


def test_display_launcher_has_closed_actions_and_fixed_bridge() -> None:
    source = (ROOT / "display.sh").read_text()
    assert "start|restart|stop|status" in source
    assert "sudo -n \"$MANAGER\" \"$1\"" in source
    assert "systemd-run" not in source
    result = subprocess.run(["bash", str(ROOT / "display.sh"), "unknown"], text=True, capture_output=True, check=False)
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
        "openvt -c 8 -s -w -- " + str(sandbox / "usr/local/libexec/dgx-fan-display-tty-acquired") + " operator " + str(clone / "start.sh") + " tty3",
        "runuser -u operator -- " + str(clone / "start.sh"),
        "chvt 3",
        "deallocvt 8",
    ]
    assert not (sandbox / "dev/tty8").exists()

    log.unlink()
    no_marker_cleanup = subprocess.run(["sh", str(cleanup)], env=environment, text=True, capture_output=True, check=False)
    assert no_marker_cleanup.returncode == 0
    assert not log.exists()
    marker.write_text("tty4\n")
    _fake_command(sandbox / "usr/bin/chvt", "exit 1\n")
    failed_cleanup = subprocess.run(["sh", str(cleanup)], env=environment, text=True, capture_output=True, check=False)
    assert failed_cleanup.returncode == 1
    assert marker.read_text() == "tty4\n"
    _fake_command(sandbox / "usr/bin/chvt", 'printf "chvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/lifecycle.log"\n')
    retried_cleanup = subprocess.run(["sh", str(cleanup)], env=environment, text=True, capture_output=True, check=False)
    assert retried_cleanup.returncode == 0
    assert not marker.exists()


def test_cleanup_retries_dealloc_after_tty8_carriage_return(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/chvt", 'printf "chvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/retry.log"\n')
    _fake_command(sandbox / "usr/bin/deallocvt", 'printf "deallocvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/retry.log"\nif [ ! -e "$DGX_FAN_TEST_ROOT/dealloc-once" ]; then : > "$DGX_FAN_TEST_ROOT/dealloc-once"; exit 1; fi\n')
    marker = sandbox / "run/dgx-fan-display-tty8"
    marker.parent.mkdir()
    marker.write_text("tty3\n")
    tty8 = sandbox / "dev/tty8"
    tty8.parent.mkdir()
    tty8.write_text("")
    cleanup = sandbox / "usr/local/libexec/dgx-fan-display-cleanup"

    result = subprocess.run(["sh", str(cleanup)], env=_environment(sandbox), text=True, capture_output=True, check=False)
    assert result.returncode == 0
    assert not marker.exists()
    assert tty8.read_bytes() == b"\r"
    assert (sandbox / "retry.log").read_text().splitlines() == ["chvt 3", "deallocvt 8", "deallocvt 8"]


def test_cleanup_retains_marker_when_dealloc_retry_fails(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/chvt", 'printf "chvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/fail.log"\n')
    _fake_command(sandbox / "usr/bin/deallocvt", 'printf "deallocvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/fail.log"\nif [ -e "$DGX_FAN_TEST_ROOT/dealloc-failed-once" ]; then\n    [ "$(od -An -tx1 "$DGX_FAN_TEST_ROOT/dev/tty8" | tr -d " \\n")" = "0d" ] || exit 99\n    printf "%s\\n" "second dealloc failed" >&2\nfi\n: > "$DGX_FAN_TEST_ROOT/dealloc-failed-once"\nexit 1\n')
    marker = sandbox / "run/dgx-fan-display-tty8"
    marker.parent.mkdir()
    marker.write_text("tty3\n")
    tty8 = sandbox / "dev/tty8"
    tty8.parent.mkdir()
    tty8.write_text("")
    cleanup = sandbox / "usr/local/libexec/dgx-fan-display-cleanup"

    result = subprocess.run(["sh", str(cleanup)], env=_environment(sandbox), text=True, capture_output=True, check=False)
    assert result.returncode == 1
    assert marker.read_text() == "tty3\n"
    assert tty8.read_bytes() == b"\r"
    assert (sandbox / "fail.log").read_text().splitlines() == ["chvt 3", "deallocvt 8", "deallocvt 8"]
    assert result.stderr


def test_cleanup_retains_marker_when_tty8_write_fails(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/chvt", 'printf "chvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/write-fail.log"\n')
    _fake_command(sandbox / "usr/bin/deallocvt", 'printf "deallocvt %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/write-fail.log"\nexit 1\n')
    marker = sandbox / "run/dgx-fan-display-tty8"
    marker.parent.mkdir()
    marker.write_text("tty3\n")
    tty8 = sandbox / "dev/tty8"
    tty8.mkdir(parents=True)
    cleanup = sandbox / "usr/local/libexec/dgx-fan-display-cleanup"

    result = subprocess.run(["sh", str(cleanup)], env=_environment(sandbox), text=True, capture_output=True, check=False)
    assert result.returncode == 1
    assert marker.read_text() == "tty3\n"
    assert (sandbox / "write-fail.log").read_text().splitlines() == ["chvt 3", "deallocvt 8"]
    assert result.stderr


def test_fake_manager_refuses_active_and_orders_restart(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    _fake_command(sandbox / "usr/bin/systemctl", 'printf "systemctl %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/manager.log"\ncase "$1" in is-active) printf "%s\\n" "${DGX_TEST_UNIT_STATE:-inactive}";; show) if [ ! -e "$DGX_FAN_TEST_ROOT/show-seen" ]; then : > "$DGX_FAN_TEST_ROOT/show-seen"; printf "loaded\\n"; else printf "not-found\\n"; fi;; esac\n')
    _fake_command(sandbox / "usr/bin/systemd-run", 'printf "systemd-run %s\\n" "$*" >> "$DGX_FAN_TEST_ROOT/manager.log"\n')
    manager = sandbox / "usr/local/libexec/dgx-fan-display-manager"
    environment = _environment(sandbox)
    environment["DGX_TEST_UNIT_STATE"] = "active"
    active = subprocess.run(["sh", str(manager), "start"], env=environment, text=True, capture_output=True, check=False)
    assert active.returncode == 1 and "already active" in active.stderr
    (sandbox / "manager.log").unlink()
    environment["DGX_TEST_UNIT_STATE"] = "inactive"
    restarted = subprocess.run(["sh", str(manager), "restart"], env=environment, text=True, capture_output=True, check=False)
    assert restarted.returncode == 0, restarted.stderr
    entries = (sandbox / "manager.log").read_text().splitlines()
    assert entries[:2] == [
        "systemctl stop dgx-fan-display.service",
        "systemctl show --property=LoadState --value dgx-fan-display.service",
    ]
    assert entries[2] == "systemctl show --property=LoadState --value dgx-fan-display.service"
    assert entries[3] == "systemctl reset-failed dgx-fan-display.service"
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
        result = subprocess.run(["sh", str(manager), "start"], env=environment, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
    for state in ("activating", "deactivating", "reloading"):
        environment["DGX_TEST_UNIT_STATE"] = state
        result = subprocess.run(["sh", str(manager), "start"], env=environment, text=True, capture_output=True, check=False)
        assert result.returncode == 1
        assert f"already {state}" in result.stderr
    for action in ("stop", "status"):
        result = subprocess.run(["sh", str(manager), action], env=environment, text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr
    entries = (sandbox / "manager-coverage.log").read_text()
    assert entries.count("systemd-run ") == 2
    assert "systemctl stop dgx-fan-display.service" in entries
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
    manager = sandbox / "usr/local/libexec/dgx-fan-display-manager"
    result = subprocess.run(["sh", str(manager), "restart"], env=_environment(sandbox), text=True, capture_output=True, check=False)
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
