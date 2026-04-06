#!/bin/bash
# watchdog_gateway.sh — Checks if IB Gateway is reachable on port 4001.
# If not, sends a Telegram alert and logs the outage.
# Runs every 60s via launchd.

BOT_DIR="/Users/vigneshwaranarumugam/Documents/Trading Github/forecastbot"
LOG="$BOT_DIR/data/gateway_watchdog.log"
STATE_FILE="/tmp/ib_gateway_down"

# Load Telegram credentials from .env
TELEGRAM_TOKEN=$(grep TELEGRAM_BOT_TOKEN "$BOT_DIR/.env" | cut -d= -f2)
TELEGRAM_CHAT_ID=$(grep TELEGRAM_CHAT_ID "$BOT_DIR/.env" | cut -d= -f2)

send_telegram() {
    local msg="$1"
    if [ -n "$TELEGRAM_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
            -d chat_id="$TELEGRAM_CHAT_ID" \
            -d text="$msg" \
            -d parse_mode="Markdown" > /dev/null 2>&1
    fi
}

# Check port 4001
if nc -z 127.0.0.1 4001 2>/dev/null; then
    # Gateway is UP
    if [ -f "$STATE_FILE" ]; then
        DOWN_SINCE=$(cat "$STATE_FILE")
        NOW=$(date +%s)
        DOWNTIME=$(( (NOW - DOWN_SINCE) / 60 ))
        echo "$(date '+%Y-%m-%d %H:%M:%S') — Gateway RECOVERED (was down ~${DOWNTIME}min)" >> "$LOG"
        send_telegram "IB Gateway RECOVERED (was down ~${DOWNTIME}min)"
        rm -f "$STATE_FILE"
    fi
else
    # Gateway is DOWN
    if [ ! -f "$STATE_FILE" ]; then
        # First detection — record timestamp and alert
        date +%s > "$STATE_FILE"
        echo "$(date '+%Y-%m-%d %H:%M:%S') — Gateway DOWN — port 4001 unreachable" >> "$LOG"
        send_telegram "IB Gateway DOWN — port 4001 unreachable. Please restart manually."
    fi
    # Subsequent checks while down — just log silently (no spam)
fi
