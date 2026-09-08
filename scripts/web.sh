#!/usr/bin/env bash
# Run the optional browser renderer in the regular user's systemd session.
set -euo pipefail

readonly UNIT='dgx-fan-web'
PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
CONFIG_PATH="$PROJECT_ROOT/config.toml"
PYTHON_BIN="$PROJECT_ROOT/.venv/bin/python"
MONITOR_BIN="$PROJECT_ROOT/.venv/bin/dgx-fan-monitor"

usage() {
    cat <<'EOF'
Usage: ./scripts/web.sh {start|restart|stop|status}

Starts the optional browser monitor as a transient systemd user service. It is
read-only unless web.allow_control=true. It never starts, stops, or owns the
fan controller or tty8 display.
EOF
}

fail() { printf 'dgx-fan web: %s\n' "$*" >&2; exit 1; }

[[ $EUID -ne 0 ]] || fail 'run scripts/web.sh as the regular login user, not root'
[[ $# -eq 1 ]] || { usage >&2; exit 64; }
case "$1" in
    start|restart|stop|status) ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 64 ;;
esac
command -v systemctl >/dev/null 2>&1 || fail 'systemctl is required'

if [[ "$1" == stop ]]; then
    load_state=$(systemctl --user show --property=LoadState --value "$UNIT.service") ||
        fail 'could not determine browser monitor service state'
    [[ "$load_state" == not-found ]] && exit 0
    exec systemctl --user stop "$UNIT.service"
fi
if [[ "$1" == status ]]; then
    exec systemctl --user status "$UNIT.service"
fi

[[ -f "$CONFIG_PATH" ]] || fail "config not found: $CONFIG_PATH"
[[ -x "$PYTHON_BIN" && -x "$MONITOR_BIN" ]] || {
    fail 'browser environment missing; run uv sync --locked first'
}
mapfile -t WEB_VALUES < <("$PYTHON_BIN" - "$CONFIG_PATH" <<'PY'
from pathlib import Path
import sys
from dgx_fan.config import ConfigError, load_config

try:
    config = load_config(Path(sys.argv[1]))
except ConfigError as error:
    raise SystemExit(f"configuration error: {error}") from error
if not config.web.enabled:
    raise SystemExit("set web.enabled = true in config.toml before starting the browser monitor")
print(config.web.host)
print(config.web.port)
print(str(config.web.allow_control).lower())
PY
) || fail 'cannot load enabled [web] configuration'
[[ ${#WEB_VALUES[@]} -eq 2 || ${#WEB_VALUES[@]} -eq 3 ]] ||
    fail 'invalid [web] configuration output'
HOST=${WEB_VALUES[0]}
PORT=${WEB_VALUES[1]}
ALLOW_CONTROL=${WEB_VALUES[2]:-false}

print_browser_urls() {
    case "$HOST" in
        0.0.0.0)
            local -a addresses=()
            if command -v ip >/dev/null 2>&1; then
                mapfile -t addresses < <(
                    ip -4 -o addr show scope global 2>/dev/null |
                        awk '$3 == "inet" { sub(/\/.*/, "", $4); if ($4 !~ /^127\./ && !seen[$4]++) print $4 }'
                )
            fi
            if [[ ${#addresses[@]} -eq 0 ]]; then
                printf 'Browser monitor: http://<this-pi-ip>:%s/\n' "$PORT"
                return
            fi
            local address
            for address in "${addresses[@]}"; do
                printf 'Browser monitor: http://%s:%s/\n' "$address" "$PORT"
            done
            ;;
        ::1) printf 'Browser monitor: http://[::1]:%s/\n' "$PORT" ;;
        *) printf 'Browser monitor: http://%s:%s/\n' "$HOST" "$PORT" ;;
    esac
}

if [[ "$1" == restart ]] && systemctl --user is-active --quiet "$UNIT.service"; then
    systemctl --user stop "$UNIT.service"
fi
if systemctl --user is-active --quiet "$UNIT.service"; then
    fail 'browser monitor is already active; use restart or status'
fi
command -v systemd-run >/dev/null 2>&1 || fail 'systemd-run is required'

printf -v MONITOR_COMMAND '%q ' "$MONITOR_BIN" --config "$CONFIG_PATH"
readonly SERVER_PROGRAM='from dgx_fan.web_server import RequestOriginServer; import sys; RequestOriginServer(sys.argv[1], host=sys.argv[2], port=int(sys.argv[3]), allow_control=sys.argv[4] == "true").serve()'
systemd-run --user --collect --service-type=exec --unit="$UNIT" \
    --property=Restart=no "$PYTHON_BIN" -c "$SERVER_PROGRAM" "$MONITOR_COMMAND" "$HOST" "$PORT" "$ALLOW_CONTROL"
print_browser_urls
