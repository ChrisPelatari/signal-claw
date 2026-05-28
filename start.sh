#!/usr/bin/env bash
# Bootstrap script for signal-claw. Invoked by @reboot crontab.
#
# Sources config.env (which exports SIGNAL_ACCOUNT, SIGNAL_HOMELINE, etc.)
# from this same directory, waits for network, then exec's the Python daemon.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="${REPO}/config.env"
STATE_DIR="${STATE_DIR:-${HOME}/.local/share/signal-claude}"
mkdir -p "$STATE_DIR"

CRON_LOG="${STATE_DIR}/cron.log"
exec >>"$CRON_LOG" 2>&1
echo "[$(date -Iseconds)] start.sh invoked (repo=$REPO)"

if [[ ! -f "$CONFIG" ]]; then
    echo "[$(date -Iseconds)] ERROR: $CONFIG not found. Copy config.example.env to config.env and fill in values."
    exit 1
fi
# shellcheck disable=SC1090
source "$CONFIG"

# Wait up to ~60s for network reachability before launching signal-cli.
for i in $(seq 1 30); do
    if getent hosts signal.org >/dev/null 2>&1 || ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1; then
        echo "[$(date -Iseconds)] network up after ${i} tries"
        break
    fi
    sleep 2
done

export PATH="${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin"
exec /usr/bin/python3 "${REPO}/daemon.py"
