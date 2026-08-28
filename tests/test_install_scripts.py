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


def _sandbox(tmp_path: Path) -> Path:
    sandbox = tmp_path / "system-root"
    (sandbox / "boot/firmware").mkdir(parents=True)
    (sandbox / "boot/firmware/config.txt").write_text("# test boot configuration\n")
    fake_uv = sandbox / "fake-uv"
    fake_uv.write_text(
        "#!/bin/sh\nprintf 'uv cwd=%s args=%s\\n' \"$PWD\" \"$*\" >> \"$DGX_FAN_TEST_ROOT/command.log\"\n"
    )
    fake_uv.chmod(0o755)
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
    assert "openvt -c 8 -s -w" in session.read_text()
    assert "runuser -u" in session.read_text()
    assert "systemd-run --quiet --collect --service-type=exec --unit=dgx-fan-display" in manager.read_text()
    assert "unsafe runtime directory" in helper.read_text()
    assert "unsafe runtime lock" in helper.read_text()
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
    assert not (sandbox / "usr/local/libexec/dgx-fan-prepare-hardware").exists()
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
    assert "unmanaged destination" in unmanaged.stderr
    assert "dtoverlay=pwm-2chan" not in (sandbox / "boot/firmware/config.txt").read_text()

    destination.unlink()
    target = sandbox / "other"
    target.write_text("unmanaged\n")
    destination.symlink_to(target)
    symlink = _run(clone / "install.sh", sandbox=sandbox)
    assert symlink.returncode == 1
    assert "symlink destination" in symlink.stderr
    assert target.read_text() == "unmanaged\n"


def test_non_regular_destination_fails_closed(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    destination = sandbox / "etc/profile.d/dgx-fan-autostart.sh"
    destination.mkdir(parents=True)

    result = _run(clone / "install.sh", sandbox=sandbox)

    assert result.returncode == 1
    assert "non-regular destination" in result.stderr
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
    assert not (sandbox / "usr/local/libexec/dgx-fan-prepare-hardware").exists()
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


def test_uninstall_uses_root_marker_inspection_for_production_sudoers() -> None:
    source = (ROOT / "uninstall.sh").read_text()

    assert 'marker_check=( run_root grep -Fxq "$MARKER" "$path" )' in source
    assert 'can_read=( run_root test -r "$path" )' in source
    assert 'display_helpers_managed=true' in source
    assert 'run_root grep -Fxq "$MARKER" "$display_manager"' in source
    assert 'sudo systemctl stop dgx-fan-display.service' in source


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
    assert "flock -n /run/dgx-fan/instance.lock" in result.stdout
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
    assert "Restart=" not in manager
    assert "openvt -c 8 -s -w" in session
    assert "openvt -c 8 -s -w -f" not in session
    assert "runuser -u" in session


def test_generated_helper_rejects_arguments_before_touching_real_pwm(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    helper = sandbox / "usr/local/libexec/dgx-fan-prepare-hardware"

    assert helper.read_text().startswith("#!/bin/sh\n# Managed by dgx-fan install.sh; do not edit.\n")

    result = subprocess.run(["sh", str(helper), "unexpected"], text=True, capture_output=True, check=False)

    assert result.returncode == 64
    assert "accepts no arguments" in result.stderr


def test_generated_hardware_helper_rejects_unsafe_runtime_objects_before_chown(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    assert _run(clone / "install.sh", sandbox=sandbox).returncode == 0
    helper = (sandbox / "usr/local/libexec/dgx-fan-prepare-hardware").read_text()

    directory_check = helper.index('if [ -L "$runtime" ]')
    directory_reclaim = helper.index('chown root:root "$runtime"')
    lock_check = helper.index('if [ -L "$lock" ]')
    lock_chown = helper.index('chown root:gpio "$lock"')
    assert directory_check < directory_reclaim
    assert lock_check < lock_chown
    assert 'install -d -o root -g root -m 0755 "$runtime"' in helper
    assert 'chmod 0755 "$runtime"' in helper
    assert 'stat -c %h "$lock"' in helper


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
