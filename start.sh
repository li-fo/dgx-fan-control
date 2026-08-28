#!/usr/bin/env bash
# Launch dgx-fan from this clone after the fixed root-owned hardware helper runs.
set -euo pipefail

PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
CONFIG_PATH="$PROJECT_ROOT/config.toml"
HELPER=/usr/local/libexec/dgx-fan-prepare-hardware
LOCK_PATH=/run/dgx-fan/instance.lock

[[ $EUID -ne 0 ]] || {
    printf '%s\n' 'dgx-fan: run start.sh as the regular login user, not root' >&2
    exit 1
}

if [[ "${1:-}" == "--dry-run" && $# -eq 1 ]]; then
    printf 'sudo -n %q\n' "$HELPER"
    printf 'flock -n %q\n' "$LOCK_PATH"
    printf '%q --config %q\n' "$PROJECT_ROOT/.venv/bin/dgx-fan" "$CONFIG_PATH"
    exit 0
fi
[[ $# -eq 0 ]] || { printf 'Usage: %s [--dry-run]\n' "$0" >&2; exit 64; }
[[ -f "$CONFIG_PATH" ]] || { printf 'dgx-fan: config not found: %s\n' "$CONFIG_PATH" >&2; exit 1; }
[[ -x "$PROJECT_ROOT/.venv/bin/dgx-fan" ]] || {
    printf 'dgx-fan: environment missing; run ./install.sh first\n' >&2
    exit 1
}

sudo -n "$HELPER"
exec 9>"$LOCK_PATH"
if ! flock -n 9; then
    printf '%s\n' 'dgx-fan: another foreground or physical-display instance already owns fan control' >&2
    exit 1
fi
exec "$PROJECT_ROOT/.venv/bin/dgx-fan" --config "$CONFIG_PATH"
