#!/bin/sh
set -eu
cd /root/forward
pkill -9 -f '/root/forward/.venv/bin/python forward.py' 2>/dev/null || true
pkill -9 -f '/root/forward/forward.py' 2>/dev/null || true
sleep 2
mkdir -p logs
nohup ./.venv/bin/python forward.py >> logs/restart.log 2>&1 &
echo $! > bot.pid
sleep 5
printf 'Started forward with PID: '
cat bot.pid
