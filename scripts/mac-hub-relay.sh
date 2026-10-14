#!/usr/bin/env bash
# macOS blocks background apps (AMO's server) from reaching the Family Hub on your Wi-Fi
# ("No route to host"). Daemons started by the system aren't blocked, so this runs a tiny relay as one:
# it listens on 127.0.0.1 only and passes AMO's hub traffic straight through to the hub.
#   ./scripts/mac-hub-relay.sh install [--root] | uninstall | status
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LABEL="com.amo.hubrelay"
PLIST="/Library/LaunchDaemons/$LABEL.plist"
PORT=8771
ME="$(id -un)"

set_env() { # key value: add or replace a line in .env
  touch .env
  if grep -qE "^$1=" .env; then sed -i '' "s|^$1=.*|$1=$2|" .env; else printf '\n%s=%s\n' "$1" "$2" >> .env; fi
}

case "${1:-install}" in
  install)
    grep -qE '^HUB_URL=http' .env 2>/dev/null || { echo "Add HUB_URL=http://<hub address>:8770 to $ROOT/.env first."; exit 1; }
    [[ -x .venv/bin/amo ]] || { echo "Run ./scripts/setup-mac.sh first."; exit 1; }
    USER_KEY="<key>UserName</key><string>$ME</string>"
    [[ "${2:-}" == "--root" ]] && USER_KEY=""
    mkdir -p data/logs
    echo "Installing the hub relay (macOS will ask for your Mac password)…"
    sudo tee "$PLIST" >/dev/null <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$ROOT/.venv/bin/amo</string><string>hub-relay</string><string>--port</string><string>$PORT</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  $USER_KEY
  <key>EnvironmentVariables</key><dict><key>AMO_ENV_FILE</key><string>$ROOT/.env</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/data/logs/$LABEL.log</string>
  <key>StandardErrorPath</key><string>$ROOT/data/logs/$LABEL.log</string>
</dict></plist>
PL
    sudo chown root:wheel "$PLIST"
    sudo chmod 644 "$PLIST"
    sudo launchctl bootout "system/$LABEL" 2>/dev/null || true
    sleep 1
    sudo launchctl bootstrap system "$PLIST"
    set_env AMO_HUB_RELAY "$PORT"
    sleep 2
    if curl -fs -m 5 "http://127.0.0.1:$PORT/api/health" >/dev/null; then
      echo "✅ The relay reaches the hub."
      launchctl kickstart -k "gui/$(id -u)/com.amo.server" 2>/dev/null && echo "Restarted AMO."
      echo "Check with: $ROOT/.venv/bin/amo hub"
    else
      echo "⚠ The relay is running but can't reach the hub yet. Details: tail $ROOT/data/logs/$LABEL.log"
      [[ -n "$USER_KEY" ]] && echo "  If it says 'No route to host', try: $0 install --root"
    fi
    ;;
  uninstall)
    sudo launchctl bootout "system/$LABEL" 2>/dev/null || true
    sudo rm -f "$PLIST"
    [[ -f .env ]] && sed -i '' '/^AMO_HUB_RELAY=/d' .env
    launchctl kickstart -k "gui/$(id -u)/com.amo.server" 2>/dev/null || true
    echo "Removed the hub relay."
    ;;
  status)
    sudo launchctl print "system/$LABEL" 2>/dev/null | grep -E "state|pid" || echo "not installed"
    curl -fs -m 5 "http://127.0.0.1:$PORT/api/health" && echo || echo "the relay doesn't reach the hub"
    ;;
  *) echo "usage: $0 install [--root] | uninstall | status"; exit 1 ;;
esac
