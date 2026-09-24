#!/usr/bin/env bash
# Guide an operator through choosing the primary display and optional web monitor.
set -euo pipefail

PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly PROJECT_ROOT
readonly START_SCRIPT="$PROJECT_ROOT/scripts/start.sh"
readonly DISPLAY_SCRIPT="$PROJECT_ROOT/scripts/display.sh"
readonly WEB_SCRIPT="$PROJECT_ROOT/scripts/web.sh"
readonly INSTALL_SCRIPT="$PROJECT_ROOT/install.sh"
readonly INSTALL_MARKER='# Managed by dgx-fan install.sh; do not edit.'
TEST_ROOT=''
INSTALL_STATE=''
INSTALLED_ROOT=''

usage() {
    cat <<'EOF'
Usage: ./dgx-fan-control.sh
       ./dgx-fan-control.sh stop

Interactively starts the DGX Fan Controller in the current terminal or on the
HDMI display, with an optional browser monitor (read-only unless control is
enabled in config.toml). Direct scripts remain available under ./scripts/.

Before an interactive start, the launcher verifies that this clone owns a
complete installation. It asks before installing, repairing, or switching
from another clone; the default answer is No.

The stop command non-interactively stops the optional browser monitor first,
then the managed HDMI display. A foreground current-terminal TUI remains
owned by its terminal and must be closed with Ctrl+Q.
EOF
}

configure_test_root() {
    [[ -n "${DGX_FAN_TEST_ROOT:-}" ]] || return 0
    [[ "${DGX_FAN_INSTALL_TESTING:-}" == 1 ]] || {
        printf '%s\n' 'DGX_FAN_TEST_ROOT is test-only' >&2
        exit 1
    }
    [[ "$DGX_FAN_TEST_ROOT" = /* && "$DGX_FAN_TEST_ROOT" != / \
        && -d "$DGX_FAN_TEST_ROOT" && ! -L "$DGX_FAN_TEST_ROOT" ]] || {
        printf '%s\n' 'DGX_FAN_TEST_ROOT must be an absolute real directory other than /' >&2
        exit 1
    }
    TEST_ROOT=$(realpath -e -- "$DGX_FAN_TEST_ROOT")
    [[ "$TEST_ROOT" == "${DGX_FAN_TEST_ROOT%/}" ]] || {
        printf '%s\n' 'DGX_FAN_TEST_ROOT must not traverse a symlink' >&2
        exit 1
    }
    [[ $(stat -c '%u' -- "$TEST_ROOT") == "$EUID" ]] || {
        printf '%s\n' 'DGX_FAN_TEST_ROOT must be owned by the caller' >&2
        exit 1
    }
}

target_path() {
    if [[ -n "$TEST_ROOT" ]]; then printf '%s%s' "$TEST_ROOT" "$1"; else printf '%s' "$1"; fi
}

read_install_record() {
    local record line marker version root extra
    record=$(target_path /etc/dgx-fan-installation)
    [[ -f "$record" && ! -L "$record" && -r "$record" ]] || return 1
    {
        IFS= read -r marker
        IFS= read -r version
        IFS= read -r root
        if IFS= read -r extra; then return 1; fi
    } <"$record" || return 1
    [[ "$marker" == "$INSTALL_MARKER" && "$version" == version=1 && "$root" = /* \
        && "$root" != *$'\r'* ]] || return 1
    INSTALLED_ROOT=$root
}

managed_file_ready() {
    local path=$1
    [[ -f "$path" && ! -L "$path" && -r "$path" ]] && grep -Fxq "$INSTALL_MARKER" "$path"
}

installation_prerequisites_ready() {
    local helper=$1 manager=$2 session=$3 acquired=$4 cleanup=$5 graphical=$6 profile=$7 sudoers=$8
    [[ -f "$PROJECT_ROOT/config.toml" && -x "$PROJECT_ROOT/.venv/bin/dgx-fan" \
        && -x "$START_SCRIPT" && -x "$DISPLAY_SCRIPT" && -x "$WEB_SCRIPT" \
        && -x "$helper" && -x "$manager" && -x "$session" && -x "$acquired" && -x "$cleanup" && -x "$graphical" \
        && -x "$PROJECT_ROOT/scripts/graphical_session.py" ]] \
        && managed_file_ready "$helper" \
        && managed_file_ready "$manager" \
        && managed_file_ready "$session" \
        && managed_file_ready "$acquired" \
        && managed_file_ready "$cleanup" \
        && managed_file_ready "$graphical" \
        && managed_file_ready "$profile" \
        && sudoers_metadata_ready "$sudoers" \
        && sudo_authorizations_ready "$helper" "$manager" \
        && operator_prerequisites_ready
}

sudoers_metadata_ready() {
    local path=$1 owner group mode links expected_owner=0 expected_group=0
    [[ -f "$path" && ! -L "$path" ]] || return 1
    if [[ -n "$TEST_ROOT" ]]; then
        expected_owner=$EUID
        expected_group=$(id -g)
    fi
    read -r owner group mode links < <(stat -c '%u %g %a %h' -- "$path") || return 1
    [[ "$owner" == "$expected_owner" && "$group" == "$expected_group" \
        && "$mode" == 440 && "$links" == 1 ]]
}

sudo_authorizations_ready() {
    local helper=$1 manager=$2
    sudo -n -l -- "$helper" >/dev/null 2>&1 \
        && sudo -n -l -- "$manager" start >/dev/null 2>&1
}

operator_prerequisites_ready() {
    if [[ -n "$TEST_ROOT" ]]; then
        [[ "${DGX_FAN_TEST_PREREQUISITES_READY:-}" == 1 ]]
        return
    fi
    id -nG | tr ' ' '\n' | grep -Fxq gpio \
        && [[ -d /sys/class/pwm/pwmchip0 && -e /dev/gpiochip0 ]]
}

probe_installation() {
    local record helper manager session acquired cleanup graphical profile sudoers any=false
    record=$(target_path /etc/dgx-fan-installation)
    helper=$(target_path /usr/local/libexec/dgx-fan-prepare-hardware)
    manager=$(target_path /usr/local/libexec/dgx-fan-display-manager)
    session=$(target_path /usr/local/libexec/dgx-fan-display-session)
    acquired=$(target_path /usr/local/libexec/dgx-fan-display-tty-acquired)
    cleanup=$(target_path /usr/local/libexec/dgx-fan-display-cleanup)
    graphical=$(target_path /usr/local/libexec/dgx-fan-graphical-acquire)
    profile=$(target_path /etc/profile.d/dgx-fan-autostart.sh)
    sudoers=$(target_path /etc/sudoers.d/dgx-fan)
    for path in "$record" "$helper" "$manager" "$session" "$acquired" "$cleanup" "$graphical" "$profile" "$sudoers"; do
        [[ -e "$path" || -L "$path" ]] && any=true
    done
    INSTALLED_ROOT=''
    if read_install_record; then
        if [[ "$INSTALLED_ROOT" != "$PROJECT_ROOT" ]]; then
            INSTALL_STATE=other
            return
        fi
        if installation_prerequisites_ready "$helper" "$manager" "$session" "$acquired" "$cleanup" "$graphical" "$profile" "$sudoers"; then
            INSTALL_STATE=current
            return
        fi
    fi
    if [[ "$any" == false ]]; then INSTALL_STATE=missing; else INSTALL_STATE=partial; fi
}

confirm_installation() {
    local prompt choice
    case "$INSTALL_STATE" in
        missing) prompt='No managed installation was found. Install this clone now? [y/N] ' ;;
        partial) prompt='The managed installation is incomplete. Repair it from this clone now? [y/N] ' ;;
        other) prompt="Another clone is installed at $INSTALLED_ROOT. Switch to this clone now? [y/N] " ;;
    esac
    while true; do
        read -r -p "$prompt" choice || return 1
        case "${choice,,}" in
            y|yes) return 0 ;;
            ''|n|no|q|quit|cancel) return 1 ;;
            *) printf 'Invalid choice. Enter y or n.\n' >&2 ;;
        esac
    done
}

ensure_current_installation() {
    probe_installation
    [[ "$INSTALL_STATE" == current ]] && return
    if ! confirm_installation; then
        printf '%s\n' 'Cancelled; no installation changes were made.'
        return 1
    fi
    [[ -x "$INSTALL_SCRIPT" ]] || {
        printf 'dgx-fan launcher: installer is missing or not executable: %s\n' "$INSTALL_SCRIPT" >&2
        return 1
    }
    "$INSTALL_SCRIPT" --no-launch || return
    probe_installation
    if [[ "$INSTALL_STATE" != current ]]; then
        printf '%s\n' 'dgx-fan launcher: installation completed but is not ready; reboot or log in again, then rerun the launcher.' >&2
        return 1
    fi
}

choose_primary() {
    while true; do
        printf '\nChoose the primary TUI display:\n'
        printf '  1) Current terminal\n'
        printf '  2) HDMI: labwc + fullscreen LXTerminal\n'
        printf '  3) HDMI: Linux console (tty8 fallback)\n'
        printf '  h) Help\n'
        printf '  q) Cancel\n'
        read -r -p 'Choice [1/2/3]: ' choice || return 1
        case "${choice,,}" in
            1|terminal|t) PRIMARY=terminal; return 0 ;;
            2|hdmi|d) PRIMARY=hdmi; return 0 ;;
            3|console|c) PRIMARY=console; return 0 ;;
            h|help)
                printf 'Current terminal stays in the foreground. HDMI uses LXTerminal; console retains the older tty8 renderer.\n'
                ;;
            q|quit|cancel) return 1 ;;
            *) printf 'Invalid choice. Enter 1, 2, 3, h, or q.\n' >&2 ;;
        esac
    done
}

choose_web() {
    while true; do
        read -r -p 'Start the optional web monitor? [y/N] ' choice || return 1
        case "${choice,,}" in
            ''|n|no) WEB_REQUESTED=false; return 0 ;;
            y|yes) WEB_REQUESTED=true; return 0 ;;
            h|help)
                printf 'The web monitor is independent, read-only by default, and remains running after a normal primary TUI exit.\n'
                ;;
            q|quit|cancel) return 1 ;;
            *) printf 'Invalid choice. Enter y, n, h, or q.\n' >&2 ;;
        esac
    done
}

start_web_if_requested() {
    WEB_STARTED=false
    if [[ "$WEB_REQUESTED" == true ]]; then
        if "$WEB_SCRIPT" start; then
            WEB_STARTED=true
        else
            printf '%s\n' 'dgx-fan launcher: web monitor did not start; continuing with the selected primary display' >&2
        fi
    fi
}

cleanup_started_web() {
    if [[ "$WEB_STARTED" == true ]]; then
        "$WEB_SCRIPT" stop || printf '%s\n' 'dgx-fan launcher: could not stop the web monitor started by this invocation' >&2
    fi
}

stop_managed_components() {
    local status=0 child_status
    if [[ ! -x "$WEB_SCRIPT" ]]; then
        printf 'dgx-fan launcher: could not stop the web monitor; script is missing or not executable: %s\n' "$WEB_SCRIPT" >&2
        status=1
    elif "$WEB_SCRIPT" stop; then
        :
    else
        child_status=$?
        printf '%s\n' 'dgx-fan launcher: could not stop the web monitor' >&2
        status=$child_status
    fi
    if [[ ! -x "$DISPLAY_SCRIPT" ]]; then
        printf 'dgx-fan launcher: could not stop the HDMI display; script is missing or not executable: %s\n' "$DISPLAY_SCRIPT" >&2
        (( status == 0 )) && status=1
    elif "$DISPLAY_SCRIPT" stop; then
        :
    else
        child_status=$?
        printf '%s\n' 'dgx-fan launcher: could not stop the HDMI display' >&2
        (( status == 0 )) && status=$child_status
    fi
    return "$status"
}

if [[ $# -eq 1 && ( "$1" == -h || "$1" == --help ) ]]; then
    usage
    exit 0
fi
if [[ $# -eq 1 && "$1" == stop ]]; then
    stop_managed_components
    exit $?
fi
[[ $# -eq 0 ]] || {
    usage >&2
    exit 64
}

configure_test_root
ensure_current_installation || exit $?

for required_script in "$START_SCRIPT" "$DISPLAY_SCRIPT" "$WEB_SCRIPT"; do
    [[ -x "$required_script" ]] || {
        printf 'dgx-fan launcher: required script is missing or not executable: %s\n' "$required_script" >&2
        exit 1
    }
done

PRIMARY=''
WEB_REQUESTED=false
WEB_STARTED=false
if ! choose_primary; then
    printf '%s\n' 'Cancelled.'
    exit 0
fi
if ! choose_web; then
    printf '%s\n' 'Cancelled.'
    exit 0
fi

case "$PRIMARY" in
    terminal)
        start_web_if_requested
        primary_status=0
        "$START_SCRIPT" || primary_status=$?
        if (( primary_status != 0 )); then
            cleanup_started_web
        fi
        exit "$primary_status"
        ;;
    hdmi)
        "$DISPLAY_SCRIPT" restart
        start_web_if_requested
        ;;
    console)
        "$DISPLAY_SCRIPT" restart-console
        start_web_if_requested
        ;;
esac
