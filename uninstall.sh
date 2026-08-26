#!/usr/bin/env bash
# Remove only the root-owned integration created by install.sh.
set -euo pipefail

TEST_ROOT=""
ASSUME_YES=false

usage() {
    cat <<'EOF'
Usage: ./uninstall.sh --yes

Removes dgx-fan's profile hook, sudoers entry, and fixed hardware helper.
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
if [[ -n "${DGX_FAN_TEST_ROOT:-}" ]]; then
    [[ "${DGX_FAN_INSTALL_TESTING:-}" == "1" ]] || { echo 'DGX_FAN_TEST_ROOT is test-only' >&2; exit 1; }
    [[ "${DGX_FAN_TEST_ROOT}" = /* ]] || { echo 'DGX_FAN_TEST_ROOT must be absolute' >&2; exit 1; }
    TEST_ROOT=${DGX_FAN_TEST_ROOT%/}
fi

target_path() {
    if [[ -n "$TEST_ROOT" ]]; then printf '%s%s' "$TEST_ROOT" "$1"; else printf '%s' "$1"; fi
}
run_root() {
    if [[ -n "$TEST_ROOT" ]]; then "$@"; else sudo "$@"; fi
}

for path in \
    "$(target_path /etc/profile.d/dgx-fan-autostart.sh)" \
    "$(target_path /etc/sudoers.d/dgx-fan)" \
    "$(target_path /usr/local/libexec/dgx-fan-prepare-hardware)"; do
    if [[ -e "$path" || -L "$path" ]]; then
        run_root rm -f -- "$path"
        printf 'dgx-fan uninstall: removed %s\n' "$path"
    fi
done

printf '%s\n' 'dgx-fan uninstall: config.toml and PWM overlay were intentionally preserved.'
printf '%s\n' 'Disable console auto-login with sudo raspi-config and remove the exact overlay manually if desired.'
