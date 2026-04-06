#!/bin/bash
# manage_services.sh — Install, uninstall, or check status of forecastbot launchd services.
# Usage: ./manage_services.sh [install|uninstall|status|start|stop]

SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCH_DIR="$HOME/Library/LaunchAgents"

CONDOR_PLIST="com.forecastbot.condor.plist"
WATCHDOG_PLIST="com.forecastbot.gateway-watchdog.plist"

case "$1" in
    install)
        echo "Installing launchd services..."
        cp "$SCRIPTS_DIR/$CONDOR_PLIST" "$LAUNCH_DIR/"
        cp "$SCRIPTS_DIR/$WATCHDOG_PLIST" "$LAUNCH_DIR/"
        launchctl load "$LAUNCH_DIR/$CONDOR_PLIST"
        launchctl load "$LAUNCH_DIR/$WATCHDOG_PLIST"
        echo "Done. Both services loaded and running."
        echo ""
        echo "  Condor bot:       launchctl list | grep condor"
        echo "  Gateway watchdog: launchctl list | grep gateway"
        ;;

    uninstall)
        echo "Stopping and removing launchd services..."
        launchctl unload "$LAUNCH_DIR/$CONDOR_PLIST" 2>/dev/null
        launchctl unload "$LAUNCH_DIR/$WATCHDOG_PLIST" 2>/dev/null
        rm -f "$LAUNCH_DIR/$CONDOR_PLIST"
        rm -f "$LAUNCH_DIR/$WATCHDOG_PLIST"
        echo "Done. Services removed."
        ;;

    start)
        echo "Starting services..."
        launchctl load "$LAUNCH_DIR/$CONDOR_PLIST" 2>/dev/null
        launchctl load "$LAUNCH_DIR/$WATCHDOG_PLIST" 2>/dev/null
        echo "Started."
        ;;

    stop)
        echo "Stopping services..."
        launchctl unload "$LAUNCH_DIR/$CONDOR_PLIST" 2>/dev/null
        launchctl unload "$LAUNCH_DIR/$WATCHDOG_PLIST" 2>/dev/null
        echo "Stopped. Run '$0 start' to resume."
        ;;

    status)
        echo "=== Condor Bot ==="
        launchctl list | grep forecastbot.condor || echo "  Not loaded"
        echo ""
        echo "=== Gateway Watchdog ==="
        launchctl list | grep forecastbot.gateway || echo "  Not loaded"
        echo ""
        echo "=== IB Gateway Port 4001 ==="
        if nc -z 127.0.0.1 4001 2>/dev/null; then
            echo "  UP"
        else
            echo "  DOWN"
        fi
        echo ""
        echo "=== Bot Process ==="
        pgrep -f weather_condor.py > /dev/null && echo "  Running (PID $(pgrep -f weather_condor.py))" || echo "  Not running"
        ;;

    *)
        echo "Usage: $0 [install|uninstall|status|start|stop]"
        exit 1
        ;;
esac
