#!/usr/bin/env bash
# macOS blocks background apps (AMO's server) from reaching the Family Hub on your Wi-Fi
# ("No route to host"). Daemons started by the system aren't blocked, so this runs a tiny relay as one:
# it listens on 127.0.0.1 only and passes AMO's hub traffic straight through to the hub.
#   ./scripts/mac-hub-relay.sh install | uninstall | status
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LABEL="com.amo.hubrelay"
PLIST="/Library/LaunchDaemons/$LABEL.plist"
# The service runs a locked copy (owned by the system, standard library only), never AMO's own folder:
# AMO can edit its own code, and that must never become code running with system rights.
LIBDIR="/Library/Application Support/AMO"
RELAY="$LIBDIR/hubrelay.py"
PORT=8771

set_env() { # key value: add or replace a line in .env
  touch .env
  if grep -qE "^$1=" .env; then sed -i '' "s|^$1=.*|$1=$2|" .env; else printf '\n%s=%s\n' "$1" "$2" >> .env; fi
}

case "${1:-install}" in
  install)
    grep -qE '^HUB_URL=http' .env 2>/dev/null || { echo "Add HUB_URL=http://<hub address>:8770 to $ROOT/.env first."; exit 1; }
    HUB="$(grep -E '^HUB_URL=' .env | tail -1 | cut -d= -f2- | tr -d '"'"'"' ')"
    PY=/usr/bin/python3
    "$PY" -c "import asyncio" 2>/dev/null || { echo "Needs Apple's Python: run  xcode-select --install  then try again."; exit 1; }
    LOG="/Library/Logs/$LABEL.log"
    echo "Installing the hub relay (macOS will ask for your Mac password)…"
    sudo mkdir -p "$LIBDIR"
    sudo cp amo/hubrelay.py "$RELAY"
    sudo chown -R root:wheel "$LIBDIR"
    sudo chmod 755 "$LIBDIR"
    sudo chmod 644 "$RELAY"
    sudo tee "$PLIST" >/dev/null <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>-I</string><string>$RELAY</string><string>--port</string><string>$PORT</string><string>--to</string><string>$HUB</string></array>
  <key>WorkingDirectory</key><string>/</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
PL
    sudo chown root:wheel "$PLIST"
    sudo chmod 644 "$PLIST"
    sudo launchctl bootout "system/$LABEL" 2>/dev/null || true
    sleep 1
    sudo launchctl bootstrap system "$PLIST"
    set_env AMO_HUB_RELAY "$PORT"
    ok=""
    for _ in $(seq 1 10); do  # Python can take a few seconds to start
      sleep 1
      curl -fs -m 5 "http://127.0.0.1:$PORT/api/health" >/dev/null && { ok=1; break; }
    done
    if [[ -n "$ok" ]]; then
      echo "✅ The relay reaches the hub."
      launchctl kickstart -k "gui/$(id -u)/com.amo.server" 2>/dev/null && echo "Restarted AMO."
      echo "Check with: $ROOT/.venv/bin/amo hub"
    else
      echo "⚠ The relay is running but can't reach the hub yet. Details: tail $LOG"
    fi
    ;;
  uninstall)
    sudo launchctl bootout "system/$LABEL" 2>/dev/null || true
    sudo rm -f "$PLIST"
    sudo rm -rf "$LIBDIR"
    [[ -f .env ]] && sed -i '' '/^AMO_HUB_RELAY=/d' .env
    launchctl kickstart -k "gui/$(id -u)/com.amo.server" 2>/dev/null || true
    echo "Removed the hub relay."
    ;;
  status)
    sudo launchctl print "system/$LABEL" 2>/dev/null | grep -E "state|pid" || echo "not installed"
    curl -fs -m 5 "http://127.0.0.1:$PORT/api/health" && echo || echo "the relay doesn't reach the hub"
    ;;
  *) echo "usage: $0 install | uninstall | status"; exit 1 ;;
esac
