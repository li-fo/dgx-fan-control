#!/usr/bin/env bash
# Launch the installed physical-display bridge; the TUI itself stays unprivileged.
set -euo pipefail

readonly MANAGER=/usr/local/libexec/dgx-fan-display-manager

usage() {
    cat <<'EOF'
Usage: ./scripts/display.sh {start|restart|stop|status}

Controls the transient dgx-fan-display.service on tty8. This script does not
accept a project path or arbitrary systemd arguments.
EOF
}

[[ $EUID -ne 0 ]] || {
    printf '%s\n' 'dgx-fan display: run scripts/display.sh as the regular login user, not root' >&2
    exit 1
}
[[ $# -eq 1 ]] || { usage >&2; exit 64; }
case "$1" in
    start|restart|stop|status) ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 64 ;;
esac
[[ -x "$MANAGER" ]] || {
    printf '%s\n' 'dgx-fan display: bridge missing; run ./install.sh first' >&2
    exit 1
}
exec sudo -n "$MANAGER" "$1"
