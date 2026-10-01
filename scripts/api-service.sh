#!/bin/zsh
# Manage the SP2L read-only API + WebUI on http://127.0.0.1:8765 (macOS LaunchAgent).
#   scripts/collector-service.sh install|uninstall|restart|status|logs
set -euo pipefail
LABEL="org.sp2l.api"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/sp2l"
PY="$REPO/.venv/bin/python"
DOMAIN="gui/$(id -u)"

write_plist() {
  mkdir -p "$LOGDIR" "$(dirname "$PLIST")"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string><string>-m</string><string>sp2l</string>
    <string>--config</string><string>$REPO/config/runtime.yaml</string>
    <string>api</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>8765</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Standard</string>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>StandardOutPath</key><string>$LOGDIR/api.out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/api.err.log</string>
</dict>
</plist>
PLIST
  plutil -lint "$PLIST" >/dev/null
}

case "${1:-status}" in
  install)
    write_plist
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    launchctl enable "$DOMAIN/$LABEL"
    echo "installed $LABEL -> $PLIST" ;;
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "uninstalled $LABEL" ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL" ;;
  status)
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|runs =|last exit code" || echo "$LABEL not loaded" ;;
  logs)
    tail -n 40 "$LOGDIR/api.err.log" ;;
  *) echo "usage: $0 install|uninstall|restart|status|logs"; exit 2 ;;
esac
