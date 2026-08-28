#!/usr/bin/env bash
# Install the local-console launcher for dgx-fan on Raspberry Pi OS.
set -euo pipefail

readonly OVERLAY='dtoverlay=pwm-2chan,pin=18,pin2=19,func=2,func2=2'
readonly HELPER_NAME='dgx-fan-prepare-hardware'
readonly DISPLAY_MANAGER_NAME='dgx-fan-display-manager'
readonly DISPLAY_SESSION_NAME='dgx-fan-display-session'
readonly DISPLAY_TTY_ACQUIRED_NAME='dgx-fan-display-tty-acquired'
readonly DISPLAY_CLEANUP_NAME='dgx-fan-display-cleanup'
readonly PROFILE_NAME='dgx-fan-autostart.sh'
readonly SUDOERS_NAME='dgx-fan'

PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
CONFIG_PATH="$PROJECT_ROOT/config.toml"
REBOOT=false
DRY_RUN=false
TEST_ROOT=""
readonly HELPER_MARKER='# Managed by dgx-fan install.sh; do not edit.'
readonly PROFILE_MARKER='# Managed by dgx-fan install.sh; do not edit.'
readonly SUDOERS_MARKER='# Managed by dgx-fan install.sh; do not edit.'

[[ $EUID -ne 0 ]] || { printf '%s\n' 'dgx-fan install: run as the regular login user, not root' >&2; exit 1; }
[[ "$PROJECT_ROOT" != *$'\n'* && "$PROJECT_ROOT" != *$'\r'* ]] || {
    printf '%s\n' 'dgx-fan install: project path must not contain a newline' >&2
    exit 1
}

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
    [[ "${DGX_FAN_TEST_ROOT}" != / ]] || fail 'DGX_FAN_TEST_ROOT must not be /'
    [[ -d "${DGX_FAN_TEST_ROOT}" && ! -L "${DGX_FAN_TEST_ROOT}" ]] || fail 'DGX_FAN_TEST_ROOT must be a real directory'
    TEST_ROOT=$(realpath -e -- "${DGX_FAN_TEST_ROOT}")
    [[ "$TEST_ROOT" == "${DGX_FAN_TEST_ROOT%/}" ]] || fail 'DGX_FAN_TEST_ROOT must not traverse a symlink'
    [[ $(stat -c '%u' -- "$TEST_ROOT") == "$EUID" ]] || fail 'DGX_FAN_TEST_ROOT must be owned by the caller'
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
hardware = config.hardware
problems = []
if hardware.backend != "raspberry-pi":
    problems.append("hardware.backend must be raspberry-pi")
if hardware.pwm_gpio_bcm != (18, 19):
    problems.append("hardware.pwm_gpio_bcm must be [18, 19]")
if hardware.pwm_chip_path != "/sys/class/pwm/pwmchip0":
    problems.append("hardware.pwm_chip_path must be /sys/class/pwm/pwmchip0")
if hardware.gpio_chip_path != "/dev/gpiochip0":
    problems.append("hardware.gpio_chip_path must be /dev/gpiochip0")
if problems:
    raise SystemExit("; ".join(problems))
' "$CONFIG_PATH"
}

check_platform() {
    [[ -n "$TEST_ROOT" ]] && return
    [[ -r /proc/device-tree/model ]] || fail 'this installer must run on Raspberry Pi OS'
    local model
    model=$(tr -d '\000' </proc/device-tree/model)
    [[ "$model" == *'Raspberry Pi 4'* ]] || fail "unsupported platform (Raspberry Pi 4 required): $model"
    command -v raspi-config >/dev/null 2>&1 || fail 'raspi-config is required for tty1 console auto-login'
    command -v visudo >/dev/null 2>&1 || fail 'visudo is required to validate the sudoers policy'
    for required_tool in systemctl systemd-run openvt runuser chvt deallocvt; do
        command -v "$required_tool" >/dev/null 2>&1 || fail "$required_tool is required for physical-display integration"
    done
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
    local found backup context line
    found=false
    while IFS=$'\t' read -r context line; do
        [[ -z "$line" ]] && continue
        if [[ "$context" != global && "$context" != all ]]; then
            fail "pwm-2chan overlay is conditional under [$context] in $BOOT_CONFIG; reconcile it manually"
        fi
        [[ "$line" == "$OVERLAY" ]] || fail "conflicting active pwm-2chan overlay in $BOOT_CONFIG: $line"
        found=true
    done < <(awk '
        /^[[:space:]]*(#|$)/ { next }
        /^[[:space:]]*\[[^]]+\][[:space:]]*$/ {
            section=$0
            sub(/^[[:space:]]*\[/, "", section)
            sub(/\][[:space:]]*$/, "", section)
            next
        }
        {
            line=$0
            sub(/^[[:space:]]+/, "", line)
            sub(/[[:space:]]+$/, "", line)
            if (line ~ /^dtoverlay=pwm-2chan/) {
                print (section == "" ? "global" : section) "\t" line
            }
        }
    ' "$BOOT_CONFIG")
    if [[ "$found" == true ]]; then
        note "PWM overlay already present in $BOOT_CONFIG"
        return
    fi
    backup="${BOOT_CONFIG}.dgx-fan-$(date +%Y%m%d%H%M%S).bak"
    run_root cp -- "$BOOT_CONFIG" "$backup"
    printf '\n[all]\n%s\n' "$OVERLAY" | run_root tee -a "$BOOT_CONFIG" >/dev/null
    note "added PWM overlay; backup: $backup"
}

write_helper() {
    local helper rendered
    helper=$(target_path "/usr/local/libexec/$HELPER_NAME")
    rendered=$(mktemp)
    trap 'rm -f -- "$rendered"' RETURN
    {
        cat <<'EOF'
#!/bin/sh
EOF
        printf '%s\n' "$HELPER_MARKER"
        cat <<'EOF'
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
    } >"$rendered"
    install_managed_file "$rendered" "$helper" "$HELPER_MARKER" 0755
    trap - RETURN
    rm -f -- "$rendered"
}

write_display_helpers() {
    local manager session acquired cleanup rendered project_quoted user_quoted marker_quoted tty_active_quoted openvt_quoted runuser_quoted chvt_quoted deallocvt_quoted systemctl_quoted systemd_run_quoted acquired_quoted cleanup_quoted session_quoted
    manager=$(target_path "/usr/local/libexec/$DISPLAY_MANAGER_NAME")
    session=$(target_path "/usr/local/libexec/$DISPLAY_SESSION_NAME")
    acquired=$(target_path "/usr/local/libexec/$DISPLAY_TTY_ACQUIRED_NAME")
    cleanup=$(target_path "/usr/local/libexec/$DISPLAY_CLEANUP_NAME")
    project_quoted=$(posix_quote "$PROJECT_ROOT")
    user_quoted=$(posix_quote "$INSTALL_USER")
    marker_quoted=$(posix_quote "$(target_path /run/dgx-fan-display-tty8)")
    tty_active_quoted=$(posix_quote "$(target_path /sys/class/tty/tty0/active)")
    openvt_quoted=$(posix_quote "$(target_path /usr/bin/openvt)")
    runuser_quoted=$(posix_quote "$(target_path /usr/sbin/runuser)")
    chvt_quoted=$(posix_quote "$(target_path /usr/bin/chvt)")
    deallocvt_quoted=$(posix_quote "$(target_path /usr/bin/deallocvt)")
    systemctl_quoted=$(posix_quote "$(target_path /usr/bin/systemctl)")
    systemd_run_quoted=$(posix_quote "$(target_path /usr/bin/systemd-run)")
    acquired_quoted=$(posix_quote "$acquired")
    cleanup_quoted=$(posix_quote "$cleanup")
    session_quoted=$(posix_quote "$session")
    rendered=$(mktemp)
    trap 'rm -f -- "$rendered"' RETURN
    {
        cat <<EOF
#!/bin/sh
$HELPER_MARKER
set -eu
project_root=$project_quoted
install_user=$user_quoted
openvt=$openvt_quoted
acquired=$acquired_quoted
tty_active=$tty_active_quoted
previous_vt=\$(cat "\$tty_active" 2>/dev/null || true)
case "\$previous_vt" in tty[1-9]|tty[1-9][0-9]*) ;; *) echo "cannot validate active virtual terminal" >&2; exit 1;; esac
exec "\$openvt" -c 8 -s -w -- "\$acquired" "\$install_user" "\$project_root/start.sh" "\$previous_vt"
EOF
    } >"$rendered"
    install_managed_file "$rendered" "$session" "$HELPER_MARKER" 0755
    rm -f -- "$rendered"
    rendered=$(mktemp)
    trap 'rm -f -- "$rendered"' RETURN
    {
        cat <<EOF
#!/bin/sh
$HELPER_MARKER
set -eu
marker=$marker_quoted
runuser=$runuser_quoted
[ "\$#" -eq 3 ] || exit 64
case "\$1" in *[!A-Za-z0-9._-]*|'') exit 64;; esac
[ -x "\$2" ] || exit 1
case "\$3" in tty[1-9]|tty[1-9][0-9]*) ;; *) exit 64;; esac
[ ! -e "\$marker" ] && [ ! -L "\$marker" ] || { echo "stale tty8 ownership marker" >&2; exit 1; }
umask 077
: >"\$marker"
printf '%s\n' "\$3" >"\$marker"
exec "\$runuser" -u "\$1" -- "\$2"
EOF
    } >"$rendered"
    install_managed_file "$rendered" "$acquired" "$HELPER_MARKER" 0755
    rm -f -- "$rendered"
    rendered=$(mktemp)
    trap 'rm -f -- "$rendered"' RETURN
    {
        cat <<EOF
#!/bin/sh
$HELPER_MARKER
set -eu
marker=$marker_quoted
chvt=$chvt_quoted
deallocvt=$deallocvt_quoted
if [ -L "\$marker" ] || { [ -e "\$marker" ] && [ ! -f "\$marker" ]; }; then
    echo "unsafe tty8 ownership marker: \$marker" >&2; exit 1
fi
if [ -f "\$marker" ]; then
    previous_vt=\$(cat "\$marker" 2>/dev/null || true)
    case "\$previous_vt" in tty[1-9]|tty[1-9][0-9]*) ;; *) echo "invalid tty8 ownership marker" >&2; exit 1;; esac
    "\$chvt" "\${previous_vt#tty}" || exit 1
    "\$deallocvt" 8 || exit 1
    rm -f -- "\$marker"
fi
EOF
    } >"$rendered"
    install_managed_file "$rendered" "$cleanup" "$HELPER_MARKER" 0755
    rm -f -- "$rendered"
    rendered=$(mktemp)
    trap 'rm -f -- "$rendered"' RETURN
    {
        cat <<EOF
#!/bin/sh
$HELPER_MARKER
set -eu
unit=dgx-fan-display.service
session=$session_quoted
cleanup=$cleanup_quoted
systemctl=$systemctl_quoted
systemd_run=$systemd_run_quoted
wait_unloaded() {
  count=0
  while [ "\$count" -lt 20 ]; do
    [ "\$("\$systemctl" show --property=LoadState --value "\$unit" 2>/dev/null || true)" = not-found ] && return 0
    sleep 0.1
    count=\$((count + 1))
  done
  echo "timed out waiting for \$unit to unload" >&2; return 1
}
run_cleanup() {
  "\$cleanup"
}
case "\${1:-}" in
  start)
    [ "\$#" -eq 1 ] || exit 64
    state=\$("\$systemctl" is-active "\$unit" 2>/dev/null || true)
    case "\$state" in active|activating|deactivating|reloading) echo "dgx-fan display is already \$state" >&2; exit 1;; esac
    "\$systemctl" reset-failed "\$unit" >/dev/null 2>&1 || true
    "\$systemctl" stop "\$unit" >/dev/null 2>&1 || true
    wait_unloaded
    run_cleanup
    exec "\$systemd_run" --quiet --collect --service-type=exec --unit=dgx-fan-display --property=TimeoutStopSec=10s --property=ExecStopPost="\$cleanup" "\$session"
    ;;
  restart)
    [ "\$#" -eq 1 ] || exit 64
    "\$systemctl" stop "\$unit" >/dev/null 2>&1 || true
    wait_unloaded
    run_cleanup
    "\$systemctl" reset-failed "\$unit" >/dev/null 2>&1 || true
    exec "\$systemd_run" --quiet --collect --service-type=exec --unit=dgx-fan-display --property=TimeoutStopSec=10s --property=ExecStopPost="\$cleanup" "\$session"
    ;;
  stop)
    [ "\$#" -eq 1 ] || exit 64
    "\$systemctl" stop "\$unit"
    wait_unloaded
    run_cleanup
    ;;
  status)
    [ "\$#" -eq 1 ] || exit 64
    exec "\$systemctl" status --no-pager "\$unit"
    ;;
  *) echo "Usage: dgx-fan-display-manager {start|restart|stop|status}" >&2; exit 64 ;;
esac
EOF
    } >"$rendered"
    install_managed_file "$rendered" "$manager" "$HELPER_MARKER" 0755
    trap - RETURN
    rm -f -- "$rendered"
}

write_sudoers() {
    local sudoers temporary
    sudoers=$(target_path "/etc/sudoers.d/$SUDOERS_NAME")
    temporary=$(mktemp)
    trap 'rm -f -- "$temporary"' RETURN
    {
        printf '%s\n' "$SUDOERS_MARKER"
        printf '%s ALL=(root) NOPASSWD: /usr/local/libexec/%s, /usr/local/libexec/%s *\n' "$INSTALL_USER" "$HELPER_NAME" "$DISPLAY_MANAGER_NAME"
    } >"$temporary"
    if [[ -n "$TEST_ROOT" ]]; then
        grep -Fq "/usr/local/libexec/$HELPER_NAME" "$temporary" || fail 'test sudoers validation failed'
    else
        run_root visudo -cf "$temporary"
    fi
    install_managed_file "$temporary" "$sudoers" "$SUDOERS_MARKER" 0440
    trap - RETURN
    rm -f -- "$temporary"
}

write_profile_hook() {
    local hook project_quoted user_quoted rendered
    hook=$(target_path "/etc/profile.d/$PROFILE_NAME")
    project_quoted=$(posix_quote "$PROJECT_ROOT")
    user_quoted=$(posix_quote "$INSTALL_USER")
    rendered=$(mktemp)
    trap 'rm -f -- "$rendered"' RETURN
    {
        printf '%s\n' "$PROFILE_MARKER"
        printf 'DGX_FAN_PROJECT_ROOT=%s\n' "$project_quoted"
        printf 'DGX_FAN_INSTALL_USER=%s\n' "$user_quoted"
        cat <<'EOF'
if [ "${USER:-}" = "$DGX_FAN_INSTALL_USER" ] && [ -z "${SSH_CONNECTION:-}" ] \
    && [ "${DGX_FAN_AUTOSTART_ATTEMPTED:-}" != "1" ] \
    && [ "$(tty 2>/dev/null || true)" = "/dev/tty1" ]; then
    DGX_FAN_AUTOSTART_ATTEMPTED=1
    export DGX_FAN_AUTOSTART_ATTEMPTED
    "$DGX_FAN_PROJECT_ROOT/display.sh" start || printf '%s\n' 'dgx-fan physical display did not start; see the message above.' >&2
fi
unset DGX_FAN_PROJECT_ROOT DGX_FAN_INSTALL_USER
EOF
    } >"$rendered"
    install_managed_file "$rendered" "$hook" "$PROFILE_MARKER" 0644
    trap - RETURN
    rm -f -- "$rendered"
}

ensure_managed_destination() {
    local destination=$1 marker=$2
    if [[ -L "$destination" ]]; then
        fail "refusing symlink destination: $destination"
    fi
    if [[ -e "$destination" ]]; then
        [[ -f "$destination" ]] || fail "refusing non-regular destination: $destination"
        [[ $(run_root stat -c '%h' -- "$destination") == 1 ]] || fail "refusing hardlinked destination: $destination"
        if [[ -n "$TEST_ROOT" ]]; then
            grep -Fxq "$marker" "$destination" || fail "refusing unmanaged destination: $destination"
        else
            run_root grep -Fxq "$marker" "$destination" || fail "refusing unmanaged destination: $destination"
        fi
    fi
}

install_managed_file() {
    local source=$1 destination=$2 marker=$3 mode=$4 parent
    ensure_managed_destination "$destination" "$marker"
    parent=$(dirname -- "$destination")
    if [[ -e "$parent" || -L "$parent" ]]; then
        [[ -d "$parent" && ! -L "$parent" ]] || fail "refusing unsafe parent directory: $parent"
    else
        run_root install -d -m 0755 -- "$parent"
    fi
    if [[ -n "$TEST_ROOT" ]]; then
        install -m "$mode" -- "$source" "$destination"
    else
        run_root install -o root -g root -m "$mode" -- "$source" "$destination"
    fi
}

preflight_managed_destinations() {
    local helper manager session acquired cleanup sudoers hook
    helper=$(target_path "/usr/local/libexec/$HELPER_NAME")
    manager=$(target_path "/usr/local/libexec/$DISPLAY_MANAGER_NAME")
    session=$(target_path "/usr/local/libexec/$DISPLAY_SESSION_NAME")
    acquired=$(target_path "/usr/local/libexec/$DISPLAY_TTY_ACQUIRED_NAME")
    cleanup=$(target_path "/usr/local/libexec/$DISPLAY_CLEANUP_NAME")
    sudoers=$(target_path "/etc/sudoers.d/$SUDOERS_NAME")
    hook=$(target_path "/etc/profile.d/$PROFILE_NAME")
    ensure_destination_parent "$helper"
    ensure_destination_parent "$manager"
    ensure_destination_parent "$session"
    ensure_destination_parent "$acquired"
    ensure_destination_parent "$cleanup"
    ensure_destination_parent "$sudoers"
    ensure_destination_parent "$hook"
    ensure_managed_destination "$helper" "$HELPER_MARKER"
    ensure_managed_destination "$manager" "$HELPER_MARKER"
    ensure_managed_destination "$session" "$HELPER_MARKER"
    ensure_managed_destination "$acquired" "$HELPER_MARKER"
    ensure_managed_destination "$cleanup" "$HELPER_MARKER"
    ensure_managed_destination "$sudoers" "$SUDOERS_MARKER"
    ensure_managed_destination "$hook" "$PROFILE_MARKER"
}

ensure_destination_parent() {
    local destination=$1 parent
    parent=$(dirname -- "$destination")
    if [[ -e "$parent" || -L "$parent" ]]; then
        [[ -d "$parent" && ! -L "$parent" ]] || fail "refusing unsafe parent directory: $parent"
        local owner mode expected_owner
        owner=$(run_root stat -c '%u' -- "$parent")
        mode=$(run_root stat -c '%a' -- "$parent")
        expected_owner=0
        [[ -n "$TEST_ROOT" ]] && expected_owner=$EUID
        [[ "$owner" == "$expected_owner" && $((8#$mode & 0022)) -eq 0 ]] || fail "refusing writable or wrong-owner parent directory: $parent"
    fi
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
    [[ -x "$PROJECT_ROOT/display.sh" ]] || fail 'display.sh must be executable in this clone'
    INSTALL_USER=$(id -un)
    if [[ -n "$TEST_ROOT" && -n "${DGX_FAN_TEST_INSTALL_USER:-}" ]]; then
        INSTALL_USER=$DGX_FAN_TEST_INSTALL_USER
    fi
    [[ -n "$INSTALL_USER" && "$INSTALL_USER" != root ]] || fail 'run install.sh as the regular login user, not root'
    copy_if_missing_config
    check_platform
    ensure_uv
    if [[ "$DRY_RUN" == true ]]; then
        note "dry run: would sync $PROJECT_ROOT/.venv, validate $CONFIG_PATH, install tty1/tty8 integration, and reboot=$REBOOT"
        return
    fi
    "$UV_BIN" sync --project "$PROJECT_ROOT" --locked --extra raspberry-pi --no-dev
    validate_config
    select_boot_config
    preflight_managed_destinations
    install_overlay
    run_root usermod -a -G gpio "$INSTALL_USER"
    write_helper
    write_display_helpers
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
