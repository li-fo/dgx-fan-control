#!/usr/bin/env bash
# Install the local-console launcher for dgx-fan on Raspberry Pi OS.
set -euo pipefail

readonly OVERLAY='dtoverlay=pwm-2chan,pin=18,pin2=19,func=2,func2=2'
readonly HELPER_NAME='dgx-fan-prepare-hardware'
readonly PROFILE_NAME='dgx-fan-autostart.sh'
readonly SUDOERS_NAME='dgx-fan'

PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
CONFIG_PATH="$PROJECT_ROOT/config.toml"
REBOOT=false
DRY_RUN=false
TEST_ROOT=""

usage() {
    cat <<'EOF'
Usage: ./install.sh [--reboot] [--dry-run]

Installs Raspberry Pi OS tty1 console auto-login integration for this clone.
The editable configuration is always kept at ./config.toml.
EOF
}

fail() { printf 'dgx-fan install: %s\n' "$*" >&2; exit 1; }
note() { printf 'dgx-fan install: %s\n' "$*"; }

while (($#)); do
    case "$1" in
        --reboot) REBOOT=true ;;
        --dry-run) DRY_RUN=true ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown option: $1" ;;
    esac
    shift
done

# This seam exists solely for the repository's temp-root tests. It cannot redirect
# paths during a normal installation, even if an environment variable is inherited.
if [[ -n "${DGX_FAN_TEST_ROOT:-}" ]]; then
    [[ "${DGX_FAN_INSTALL_TESTING:-}" == "1" ]] || fail 'DGX_FAN_TEST_ROOT is test-only'
    [[ "${DGX_FAN_TEST_ROOT}" = /* ]] || fail 'DGX_FAN_TEST_ROOT must be absolute'
    TEST_ROOT=${DGX_FAN_TEST_ROOT%/}
fi

target_path() {
    if [[ -n "$TEST_ROOT" ]]; then
        printf '%s%s' "$TEST_ROOT" "$1"
    else
        printf '%s' "$1"
    fi
}

run_root() {
    if [[ -n "$TEST_ROOT" ]]; then
        case "$1" in
            usermod|reboot)
                printf '%q ' "$@" >>"$TEST_ROOT/command.log"
                printf '\n' >>"$TEST_ROOT/command.log"
                return
                ;;
            chown)
                # Temp-root tests cannot and must not impersonate root ownership.
                return
                ;;
        esac
        "$@"
    else
        sudo "$@"
    fi
}

posix_quote() {
    [[ "$1" != *$'\n'* && "$1" != *$'\r'* ]] || fail 'project path must not contain a newline'
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

copy_if_missing_config() {
    if [[ ! -f "$CONFIG_PATH" ]]; then
        cp -- "$PROJECT_ROOT/config.example.toml" "$CONFIG_PATH"
        note "created $CONFIG_PATH from config.example.toml"
        fail 'edit config.toml (including hardware.backend = "raspberry-pi") and run install.sh again'
    fi
}

ensure_uv() {
    if [[ -n "$TEST_ROOT" && -n "${DGX_FAN_TEST_UV:-}" ]]; then
        UV_BIN=$DGX_FAN_TEST_UV
        return
    fi
    if command -v uv >/dev/null 2>&1; then
        UV_BIN=$(command -v uv)
        return
    fi
    if [[ "$DRY_RUN" == true ]]; then
        UV_BIN=uv
        return
    fi
    fail 'uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/ and rerun this script.'
}

validate_config() {
    "$PROJECT_ROOT/.venv/bin/python" -c '
from pathlib import Path
from dgx_fan.config import load_config
import sys
config = load_config(Path(sys.argv[1]))
if config.hardware.backend != "raspberry-pi":
    raise SystemExit("hardware.backend must be \"raspberry-pi\" for install.sh")
' "$CONFIG_PATH"
}

check_platform() {
    [[ -n "$TEST_ROOT" ]] && return
    [[ -r /proc/device-tree/model ]] || fail 'this installer must run on Raspberry Pi OS'
    local model
    model=$(tr -d '\000' </proc/device-tree/model)
    [[ "$model" == *'Raspberry Pi'* ]] || fail "unsupported platform: $model"
    command -v raspi-config >/dev/null 2>&1 || fail 'raspi-config is required for tty1 console auto-login'
    command -v visudo >/dev/null 2>&1 || fail 'visudo is required to validate the sudoers policy'
}

select_boot_config() {
    local firmware legacy
    firmware=$(target_path /boot/firmware/config.txt)
    legacy=$(target_path /boot/config.txt)
    if [[ -f "$firmware" ]]; then
        BOOT_CONFIG=$firmware
    elif [[ -f "$legacy" ]]; then
        BOOT_CONFIG=$legacy
    else
        fail 'cannot find /boot/firmware/config.txt or /boot/config.txt'
    fi
}

install_overlay() {
    local active line backup
    active=$(grep -Ev '^[[:space:]]*(#|$)' "$BOOT_CONFIG" | grep -F 'dtoverlay=pwm-2chan' || true)
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == "$OVERLAY" ]] || {
            fail "conflicting active pwm-2chan overlay in $BOOT_CONFIG: $line"
        }
    done <<<"$active"
    if [[ -n "$active" ]] && ! grep -Fxq "$OVERLAY" "$BOOT_CONFIG"; then
        fail "conflicting active pwm-2chan overlay in $BOOT_CONFIG"
    fi
    if grep -Fxq "$OVERLAY" "$BOOT_CONFIG"; then
        note "PWM overlay already present in $BOOT_CONFIG"
        return
    fi
    backup="${BOOT_CONFIG}.dgx-fan-$(date +%Y%m%d%H%M%S).bak"
    run_root cp -- "$BOOT_CONFIG" "$backup"
    printf '\n%s\n' "$OVERLAY" | run_root tee -a "$BOOT_CONFIG" >/dev/null
    note "added PWM overlay; backup: $backup"
}

write_helper() {
    local helper
    helper=$(target_path "/usr/local/libexec/$HELPER_NAME")
    run_root install -d -m 0755 -- "$(dirname -- "$helper")"
    run_root tee "$helper" >/dev/null <<'EOF'
#!/bin/sh
set -eu
[ "$#" -eq 0 ] || { echo "dgx-fan hardware helper accepts no arguments" >&2; exit 64; }
chip=/sys/class/pwm/pwmchip0
[ -d "$chip" ] || { echo "PWM chip not found: $chip" >&2; exit 1; }
for channel in 0 1; do
    path="$chip/pwm$channel"
    if [ ! -d "$path" ]; then
        printf '%s' "$channel" >"$chip/export"
    fi
    count=0
    while [ ! -d "$path" ] && [ "$count" -lt 20 ]; do sleep 0.05; count=$((count + 1)); done
    [ -d "$path" ] || { echo "PWM channel $channel was not exported" >&2; exit 1; }
    chgrp gpio "$path/enable" "$path/period" "$path/duty_cycle"
    chmod g+rw "$path/enable" "$path/period" "$path/duty_cycle"
done
chgrp gpio /dev/gpiochip0
chmod g+rw /dev/gpiochip0
EOF
    run_root chown root:root "$helper"
    run_root chmod 0755 "$helper"
}

write_sudoers() {
    local sudoers temporary
    sudoers=$(target_path "/etc/sudoers.d/$SUDOERS_NAME")
    temporary="${sudoers}.tmp"
    run_root install -d -m 0750 -- "$(dirname -- "$sudoers")"
    printf '%s ALL=(root) NOPASSWD: /usr/local/libexec/%s\n' "$INSTALL_USER" "$HELPER_NAME" | run_root tee "$temporary" >/dev/null
    if [[ -n "$TEST_ROOT" ]]; then
        grep -Fxq "$INSTALL_USER ALL=(root) NOPASSWD: /usr/local/libexec/$HELPER_NAME" "$temporary" || fail 'test sudoers validation failed'
    else
        run_root visudo -cf "$temporary"
    fi
    run_root chown root:root "$temporary"
    run_root chmod 0440 "$temporary"
    run_root mv -f -- "$temporary" "$sudoers"
}

write_profile_hook() {
    local hook project_quoted user_quoted
    hook=$(target_path "/etc/profile.d/$PROFILE_NAME")
    project_quoted=$(posix_quote "$PROJECT_ROOT")
    user_quoted=$(posix_quote "$INSTALL_USER")
    run_root install -d -m 0755 -- "$(dirname -- "$hook")"
    {
        printf '%s\n' '# Managed by dgx-fan install.sh. Remove with ./uninstall.sh.'
        printf 'DGX_FAN_PROJECT_ROOT=%s\n' "$project_quoted"
        printf 'DGX_FAN_INSTALL_USER=%s\n' "$user_quoted"
        cat <<'EOF'
if [ "${USER:-}" = "$DGX_FAN_INSTALL_USER" ] && [ -z "${SSH_CONNECTION:-}" ] \
    && [ "${DGX_FAN_AUTOSTART_ATTEMPTED:-}" != "1" ] \
    && [ "$(tty 2>/dev/null || true)" = "/dev/tty1" ]; then
    DGX_FAN_AUTOSTART_ATTEMPTED=1
    export DGX_FAN_AUTOSTART_ATTEMPTED
    "$DGX_FAN_PROJECT_ROOT/start.sh" || printf '%s\n' 'dgx-fan did not start; see the message above.' >&2
fi
unset DGX_FAN_PROJECT_ROOT DGX_FAN_INSTALL_USER
EOF
    } | run_root tee "$hook" >/dev/null
    run_root chown root:root "$hook"
    run_root chmod 0644 "$hook"
}

configure_console_autologin() {
    if [[ -n "$TEST_ROOT" ]]; then
        printf '%s\n' 'raspi-config nonint do_boot_behaviour B2' >>"$TEST_ROOT/command.log"
    else
        run_root raspi-config nonint do_boot_behaviour B2
    fi
}

main() {
    [[ -f "$PROJECT_ROOT/config.example.toml" ]] || fail 'run this script from a dgx-fan clone'
    [[ -x "$PROJECT_ROOT/start.sh" ]] || fail 'start.sh must be executable in this clone'
    INSTALL_USER=$(id -un)
    if [[ -n "$TEST_ROOT" && -n "${DGX_FAN_TEST_INSTALL_USER:-}" ]]; then
        INSTALL_USER=$DGX_FAN_TEST_INSTALL_USER
    fi
    [[ -n "$INSTALL_USER" && "$INSTALL_USER" != root ]] || fail 'run install.sh as the regular login user, not root'
    copy_if_missing_config
    check_platform
    ensure_uv
    if [[ "$DRY_RUN" == true ]]; then
        note "dry run: would sync $PROJECT_ROOT/.venv, validate $CONFIG_PATH, install tty1 integration, and reboot=$REBOOT"
        return
    fi
    "$UV_BIN" sync --locked --extra raspberry-pi --no-dev
    validate_config
    select_boot_config
    install_overlay
    run_root usermod -a -G gpio "$INSTALL_USER"
    write_helper
    write_sudoers
    write_profile_hook
    configure_console_autologin
    note "installed. config remains at $CONFIG_PATH"
    note 'reboot or log in again for the gpio group and tty1 console auto-login to take effect'
    if [[ "$REBOOT" == true ]]; then
        note 'rebooting now'
        run_root reboot
    fi
}

main "$@"
