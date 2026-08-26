from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _clone(tmp_path: Path, *, config: bool = True) -> Path:
    clone = tmp_path / "clone with spaces"
    clone.mkdir()
    for name in ("install.sh", "start.sh", "uninstall.sh", "config.example.toml"):
        source = ROOT / name
        target = clone / name
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
            "DGX_FAN_TEST_UV": "/bin/true",
            "DGX_FAN_TEST_INSTALL_USER": "operator",
            "SUDO_USER": "operator",
            "USER": "operator",
        }
    )
    return environment


def _run(script: Path, *args: str, sandbox: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=script.parent,
        env=_environment(sandbox),
        text=True,
        capture_output=True,
        check=False,
    )


def _sandbox(tmp_path: Path) -> Path:
    sandbox = tmp_path / "system-root"
    (sandbox / "boot/firmware").mkdir(parents=True)
    (sandbox / "boot/firmware/config.txt").write_text("# test boot configuration\n")
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
    assert (clone / "config.toml").exists()
    assert not (sandbox / "etc/config.toml").exists()
    helper = sandbox / "usr/local/libexec/dgx-fan-prepare-hardware"
    sudoers = sandbox / "etc/sudoers.d/dgx-fan"
    hook = sandbox / "etc/profile.d/dgx-fan-autostart.sh"
    assert helper.exists() and sudoers.exists() and hook.exists()
    assert "accepts no arguments" in helper.read_text()
    assert "operator ALL=(root) NOPASSWD: /usr/local/libexec/dgx-fan-prepare-hardware" in sudoers.read_text()
    hook_text = hook.read_text()
    assert '"/dev/tty1"' in hook_text
    assert "SSH_CONNECTION" in hook_text
    assert "DGX_FAN_AUTOSTART_ATTEMPTED" in hook_text
    assert "exec " not in hook_text
    assert "raspi-config nonint do_boot_behaviour B2" in (sandbox / "command.log").read_text()


def test_conflicting_overlay_fails_before_privileged_artifacts(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    (sandbox / "boot/firmware/config.txt").write_text("dtoverlay=pwm-2chan,pin=12,pin2=13\n")

    result = _run(clone / "install.sh", sandbox=sandbox)

    assert result.returncode == 1
    assert "conflicting active pwm-2chan overlay" in result.stderr
    assert not (sandbox / "etc/sudoers.d/dgx-fan").exists()


def test_non_raspberry_pi_backend_fails_before_boot_or_privilege_changes(tmp_path: Path) -> None:
    clone = _clone(tmp_path)
    sandbox = _sandbox(tmp_path)
    (clone / "config.toml").write_text((ROOT / "config.example.toml").read_text())

    result = _run(clone / "install.sh", sandbox=sandbox)

    assert result.returncode == 1
    assert 'hardware.backend must be "raspberry-pi"' in result.stderr
    assert "dtoverlay=pwm-2chan" not in (sandbox / "boot/firmware/config.txt").read_text()
    assert not (sandbox / "etc/sudoers.d/dgx-fan").exists()


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
    assert f"{ROOT}/.venv/bin/dgx-fan --config {ROOT}/config.toml" in result.stdout


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
