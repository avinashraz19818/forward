#!/bin/sh
# Restart the forwarder bot.
#
#   - resolves its own directory instead of assuming /root/forward
#   - asks for a graceful stop (SIGTERM) before escalating to SIGKILL, so the
#     bot can flush user_configs.json / user_msg_maps.json on the way out
#   - verifies the new process actually survived before reporting success
set -eu

APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$APP_DIR"

SCRIPT="forward.py"
PID_FILE="bot.pid"
PYTHON="./.venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"

mkdir -p logs

# ---- stop ---------------------------------------------------------------
stop_pid() {
    _pid=$1
    [ -n "$_pid" ] || return 0
    kill -0 "$_pid" 2>/dev/null || return 0

    echo "Stopping PID $_pid (SIGTERM)..."
    kill -TERM "$_pid" 2>/dev/null || true

    i=0
    while [ "$i" -lt 15 ]; do
        kill -0 "$_pid" 2>/dev/null || return 0
        i=$((i + 1))
        sleep 1
    done

    echo "Still alive after 15s - sending SIGKILL"
    kill -9 "$_pid" 2>/dev/null || true
    sleep 1
}

if [ -f "$PID_FILE" ]; then
    stop_pid "$(cat "$PID_FILE" 2>/dev/null || true)"
    rm -f "$PID_FILE"
fi

# Catch strays (e.g. a pid file that was lost). Match on the full script path so
# we never kill an unrelated process that merely mentions "forward.py".
for stray in $(pgrep -f "$APP_DIR/$SCRIPT" 2>/dev/null || true); do
    stop_pid "$stray"
done

# ---- start --------------------------------------------------------------
echo "Starting $SCRIPT from $APP_DIR ..."
nohup "$PYTHON" -u "$SCRIPT" >> "logs/restart.log" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

sleep 5
if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "Started forward with PID: $NEW_PID"
else
    echo "ERROR: process exited during startup. Last 30 log lines:" >&2
    tail -30 logs/restart.log >&2 || true
    rm -f "$PID_FILE"
    exit 1
fi
