#!/usr/bin/env bash
# Guide an operator through choosing the existing primary display and optional web monitor.
set -euo pipefail

PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly PROJECT_ROOT
readonly START_SCRIPT="$PROJECT_ROOT/start.sh"
readonly DISPLAY_SCRIPT="$PROJECT_ROOT/display.sh"
readonly WEB_SCRIPT="$PROJECT_ROOT/web.sh"

usage() {
    cat <<'EOF'
Usage: ./dgx-fan-control.sh

Interactively starts the DGX Fan Controller in the current terminal or on the
HDMI display, with an optional read-only browser monitor. Existing start.sh,
display.sh, and web.sh commands remain available for direct operation.
EOF
}

choose_primary() {
    while true; do
        printf '\nChoose the primary TUI display:\n'
        printf '  1) Current terminal\n'
        printf '  2) HDMI display (tty8)\n'
        printf '  h) Help\n'
        printf '  q) Cancel\n'
        read -r -p 'Choice [1/2]: ' choice || return 1
        case "${choice,,}" in
            1|terminal|t) PRIMARY=terminal; return 0 ;;
            2|hdmi|d) PRIMARY=hdmi; return 0 ;;
            h|help)
                printf 'Current terminal stays in the foreground. HDMI starts the installed tty8 display service.\n'
                ;;
            q|quit|cancel) return 1 ;;
            *) printf 'Invalid choice. Enter 1, 2, h, or q.\n' >&2 ;;
        esac
    done
}

choose_web() {
    while true; do
        read -r -p 'Start the optional read-only web monitor? [y/N] ' choice || return 1
        case "${choice,,}" in
            ''|n|no) WEB_REQUESTED=false; return 0 ;;
            y|yes) WEB_REQUESTED=true; return 0 ;;
            h|help)
                printf 'The web monitor is independent and remains running after a normal primary TUI exit.\n'
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

if [[ $# -eq 1 && ( "$1" == -h || "$1" == --help ) ]]; then
    usage
    exit 0
fi
[[ $# -eq 0 ]] || {
    usage >&2
    exit 64
}

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
        "$DISPLAY_SCRIPT" start
        start_web_if_requested
        ;;
esac
