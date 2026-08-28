#!/usr/bin/env bash
# Remove only the root-owned integration created by install.sh.
set -euo pipefail

TEST_ROOT=""
ASSUME_YES=false
readonly MARKER='# Managed by dgx-fan install.sh; do not edit.'

usage() {
    cat <<'EOF'
Usage: ./uninstall.sh --yes

Removes dgx-fan's profile hook, sudoers entry, and fixed hardware/display helpers.
It intentionally keeps config.toml, the PWM overlay, and the console auto-login setting.
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

[[ "$ASSUME_YES" == true ]] || { usage >&2; exit 64; }
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

status=0
display_helpers_managed=false
if [[ -z "$TEST_ROOT" ]]; then
    # Do not act on an unrelated unit that merely shares this fixed name.  The
    # two root-owned helpers are the ownership evidence for our transient unit.
    display_manager=/usr/local/libexec/dgx-fan-display-manager
    display_session=/usr/local/libexec/dgx-fan-display-session
    if [[ -f "$display_manager" && ! -L "$display_manager" \
        && -f "$display_session" && ! -L "$display_session" ]] \
        && run_root grep -Fxq "$MARKER" "$display_manager" \
        && run_root grep -Fxq "$MARKER" "$display_session"; then
        display_helpers_managed=true
    fi
fi
if [[ "$display_helpers_managed" == true ]]; then
    # A transient unit has no unit file to remove. Stop only the verified,
    # fixed-name unit before removing the helpers it uses.
    sudo systemctl stop dgx-fan-display.service >/dev/null 2>&1 || true
    sudo systemctl reset-failed dgx-fan-display.service >/dev/null 2>&1 || true
fi
for path in \
    "$(target_path /etc/profile.d/dgx-fan-autostart.sh)" \
    "$(target_path /etc/sudoers.d/dgx-fan)" \
    "$(target_path /usr/local/libexec/dgx-fan-prepare-hardware)" \
    "$(target_path /usr/local/libexec/dgx-fan-display-manager)" \
    "$(target_path /usr/local/libexec/dgx-fan-display-session)"; do
    if [[ -L "$path" || ( -e "$path" && ! -f "$path" ) ]]; then
        printf 'dgx-fan uninstall: preserving unsafe destination: %s\n' "$path" >&2
        status=1
    elif [[ -f "$path" ]]; then
        if [[ -n "$TEST_ROOT" ]]; then
            can_read=( test -r "$path" )
            marker_check=( grep -Fxq "$MARKER" "$path" )
        else
            can_read=( run_root test -r "$path" )
            marker_check=( run_root grep -Fxq "$MARKER" "$path" )
        fi
        if ! "${can_read[@]}"; then
            printf 'dgx-fan uninstall: preserving unreadable destination: %s\n' "$path" >&2
            status=1
            continue
        fi
        if ! "${marker_check[@]}"; then
            printf 'dgx-fan uninstall: preserving unmanaged destination: %s\n' "$path" >&2
            status=1
            continue
        fi
        run_root rm -f -- "$path"
        printf 'dgx-fan uninstall: removed %s\n' "$path"
    fi
done

printf '%s\n' 'dgx-fan uninstall: config.toml and PWM overlay were intentionally preserved.'
printf '%s\n' 'Disable console auto-login with sudo raspi-config and remove the exact overlay manually if desired.'
exit "$status"
