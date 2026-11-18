#!/usr/bin/env bash
# Runs the bot forever. If it crashes or exits, it restarts after 5 seconds.
# Usage: ./run_bot.sh   (ideally inside a tmux session)
cd "$(dirname "$0")" || exit 1

while true; do
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] Starting bot..."
    python3 main.py run
    echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] Bot stopped (exit code $?). Restarting in 5 seconds... (Ctrl+C to quit)"
    sleep 5
done
