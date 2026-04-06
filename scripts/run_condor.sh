#!/bin/bash
# run_condor.sh — Launcher for weather_condor.py
# Used by launchd to keep the bot running with auto-restart on crash.

BOT_DIR="/Users/vigneshwaranarumugam/Documents/Trading Github/forecastbot"
VENV="$BOT_DIR/venv/bin/activate"
LOG="$BOT_DIR/data/condor_launcher.log"

cd "$BOT_DIR" || exit 1

echo "$(date '+%Y-%m-%d %H:%M:%S') — Starting weather_condor.py" >> "$LOG"

source "$VENV"
exec python3 weather_condor.py 2>&1 | tee -a "$LOG"
