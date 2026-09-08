#!/usr/bin/env bash
# Remove only the root-owned integration created by install.sh.
set -euo pipefail

TEST_ROOT=""
ASSUME_YES=false
readonly MARKER='# Managed by dgx-fan install.sh; do not edit.'

usage() {
    cat <<'EOF'
Usage: ./uninstall.sh [--yes]

Removes dgx-fan's profile hook, sudoers entry, and fixed hardware/display helpers.
It intentionally keeps config.toml, the PWM overlay, and the console auto-login setting.
Without --yes, asks for confirmation and defaults to No.
EOF
}

while (($#)); do
    case "$1" in
        --yes) ASSUME_YES=true ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'dgx-fan uninstall: unknown option: %s\n' "$1" >&2; exit 64 ;;
    esac
    shift
done

[[ $EUID -ne 0 ]] || { echo 'dgx-fan uninstall: run as the regular login user, not root' >&2; exit 1; }
if [[ -n "${DGX_FAN_TEST_ROOT:-}" ]]; then
    [[ "${DGX_FAN_INSTALL_TESTING:-}" == "1" ]] || { echo 'DGX_FAN_TEST_ROOT is test-only' >&2; exit 1; }
    [[ "${DGX_FAN_TEST_ROOT}" = /* ]] || { echo 'DGX_FAN_TEST_ROOT must be absolute' >&2; exit 1; }
    [[ "${DGX_FAN_TEST_ROOT}" != / ]] || { echo 'DGX_FAN_TEST_ROOT must not be /' >&2; exit 1; }
    [[ -d "${DGX_FAN_TEST_ROOT}" && ! -L "${DGX_FAN_TEST_ROOT}" ]] || { echo 'DGX_FAN_TEST_ROOT must be a real directory' >&2; exit 1; }
    TEST_ROOT=$(realpath -e -- "${DGX_FAN_TEST_ROOT}")
    [[ "$TEST_ROOT" == "${DGX_FAN_TEST_ROOT%/}" ]] || { echo 'DGX_FAN_TEST_ROOT must not traverse a symlink' >&2; exit 1; }
    [[ $(stat -c '%u' -- "$TEST_ROOT") == "$EUID" ]] || { echo 'DGX_FAN_TEST_ROOT must be owned by the caller' >&2; exit 1; }
fi

target_path() {
    if [[ -n "$TEST_ROOT" ]]; then printf '%s%s' "$TEST_ROOT" "$1"; else printf '%s' "$1"; fi
}
run_root() {
    if [[ -n "$TEST_ROOT" ]]; then "$@"; else sudo "$@"; fi
}

confirm_uninstall() {
    local choice
    [[ "$ASSUME_YES" == true ]] && return
    while true; do
        read -r -p 'Remove the managed dgx-fan integration? [y/N] ' choice || {
            printf '%s\n' 'dgx-fan uninstall: cancelled; no files were changed.'
            return 1
        }
        case "${choice,,}" in
            y|yes) return 0 ;;
            ''|n|no|q|quit|cancel)
                printf '%s\n' 'dgx-fan uninstall: cancelled; no files were changed.'
                return 1
                ;;
            *) printf '%s\n' 'Invalid choice. Enter y or n.' >&2 ;;
        esac
    done
}

confirm_uninstall || exit 0

PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
WEB_SCRIPT="$PROJECT_ROOT/scripts/web.sh"
display_helpers_managed=false
display_helpers_present=false
display_manager=$(target_path /usr/local/libexec/dgx-fan-display-manager)
display_session=$(target_path /usr/local/libexec/dgx-fan-display-session)
display_acquired=$(target_path /usr/local/libexec/dgx-fan-display-tty-acquired)
display_cleanup=$(target_path /usr/local/libexec/dgx-fan-display-cleanup)
for display_path in "$display_manager" "$display_session" "$display_acquired" "$display_cleanup"; do
    [[ -e "$display_path" || -L "$display_path" ]] && display_helpers_present=true
done
if [[ -f "$display_manager" && ! -L "$display_manager" \
    && -f "$display_session" && ! -L "$display_session" \
    && -f "$display_acquired" && ! -L "$display_acquired" \
    && -f "$display_cleanup" && ! -L "$display_cleanup" ]] \
    && [[ $(run_root stat -c '%h' -- "$display_manager") == 1 ]] \
    && [[ $(run_root stat -c '%h' -- "$display_session") == 1 ]] \
    && [[ $(run_root stat -c '%h' -- "$display_acquired") == 1 ]] \
    && [[ $(run_root stat -c '%h' -- "$display_cleanup") == 1 ]] \
    && run_root grep -Fxq "$MARKER" "$display_manager" \
    && run_root grep -Fxq "$MARKER" "$display_session" \
    && run_root grep -Fxq "$MARKER" "$display_acquired" \
    && run_root grep -Fxq "$MARKER" "$display_cleanup"; then
    display_helpers_managed=true
fi
stop_status=0
if [[ ! -x "$WEB_SCRIPT" ]]; then
    printf 'dgx-fan uninstall: web stop helper is missing or not executable: %s\n' "$WEB_SCRIPT" >&2
    stop_status=1
else
    web_status=0
    "$WEB_SCRIPT" stop || web_status=$?
    if (( web_status != 0 )); then
        printf '%s\n' 'dgx-fan uninstall: web stop failed; managed integration will be preserved for retry.' >&2
        stop_status=$web_status
    fi
fi
if [[ "$display_helpers_managed" == true ]]; then
    display_status=0
    run_root "$display_manager" stop || display_status=$?
    if (( display_status != 0 )); then
        printf '%s\n' 'dgx-fan uninstall: display stop/cleanup failed; managed integration will be preserved for retry.' >&2
        (( stop_status == 0 )) && stop_status=$display_status
    fi
elif [[ "$display_helpers_present" == true ]]; then
    printf '%s\n' 'dgx-fan uninstall: display integration is partial or untrusted; cannot perform a safe clean stop.' >&2
    (( stop_status == 0 )) && stop_status=1
fi
if (( stop_status != 0 )); then
    exit "$stop_status"
fi

managed_paths=( \
    "$(target_path /etc/profile.d/dgx-fan-autostart.sh)" \
    "$(target_path /etc/sudoers.d/dgx-fan)" \
    "$(target_path /etc/dgx-fan-installation)" \
    "$(target_path /usr/local/libexec/dgx-fan-prepare-hardware)" \
    "$(target_path /usr/local/libexec/dgx-fan-display-manager)" \
    "$(target_path /usr/local/libexec/dgx-fan-display-session)" \
    "$(target_path /usr/local/libexec/dgx-fan-display-tty-acquired)" \
    "$(target_path /usr/local/libexec/dgx-fan-display-cleanup)" \
)
preflight_status=0
for path in "${managed_paths[@]}"; do
    if [[ -L "$path" || ( -e "$path" && ! -f "$path" ) ]]; then
        printf 'dgx-fan uninstall: preserving unsafe destination: %s\n' "$path" >&2
        preflight_status=1
    elif [[ -f "$path" ]]; then
        if [[ $(run_root stat -c '%h' -- "$path") != 1 ]]; then
            printf 'dgx-fan uninstall: preserving hardlinked destination: %s\n' "$path" >&2
            preflight_status=1
            continue
        fi
        if [[ -n "$TEST_ROOT" ]]; then
            can_read=( test -r "$path" )
            marker_check=( grep -Fxq "$MARKER" "$path" )
        else
            can_read=( run_root test -r "$path" )
            marker_check=( run_root grep -Fxq "$MARKER" "$path" )
        fi
        if ! "${can_read[@]}"; then
            printf 'dgx-fan uninstall: preserving unreadable destination: %s\n' "$path" >&2
            preflight_status=1
            continue
        fi
        if ! "${marker_check[@]}"; then
            printf 'dgx-fan uninstall: preserving unmanaged destination: %s\n' "$path" >&2
            preflight_status=1
            continue
        fi
    fi
done
if (( preflight_status != 0 )); then
    printf '%s\n' 'dgx-fan uninstall: no managed integration files were removed.' >&2
    exit "$preflight_status"
fi
for path in "${managed_paths[@]}"; do
    if [[ -f "$path" ]]; then
        run_root rm -f -- "$path"
        printf 'dgx-fan uninstall: removed %s\n' "$path"
    fi
done

printf '%s\n' 'dgx-fan uninstall: config.toml and PWM overlay were intentionally preserved.'
printf '%s\n' 'Disable console auto-login with sudo raspi-config and remove the exact overlay manually if desired.'
exit 0
