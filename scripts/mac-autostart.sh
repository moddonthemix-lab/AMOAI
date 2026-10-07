#!/usr/bin/env bash
# Run AMO (and Open WebUI, if installed) automatically at login via launchd.
#   ./scripts/mac-autostart.sh install | restart | uninstall | status | logs
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$ROOT/data/logs"
AMO_LABEL="com.amo.server"
WEBUI_LABEL="com.amo.openwebui"
OLLAMA_LABEL="com.amo.ollama"
OLLAMA_APP_BIN="/Applications/Ollama.app/Contents/Resources/ollama"

env_val() { grep -E "^$1=" "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2- ; }

plist() { # label, program args..., then env pairs after "--"
  local label=$1; shift
  local args="" envs=""
  while [[ $# -gt 0 && "$1" != "--" ]]; do args+="<string>$1</string>"; shift; done
  [[ "${1:-}" == "--" ]] && shift
  while [[ $# -gt 1 ]]; do envs+="<key>$1</key><string>$2</string>"; shift 2; done
  cat > "$AGENTS/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>$args</array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>$envs</dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGS/$label.log</string>
  <key>StandardErrorPath</key><string>$LOGS/$label.log</string>
</dict></plist>
PLIST
  local target="gui/$(id -u)/$label"
  launchctl bootout "$target" 2>/dev/null || true
  # bootout returns before the job is fully gone; bootstrapping too early fails with "5: Input/output error".
  for _ in $(seq 1 20); do launchctl print "$target" >/dev/null 2>&1 || break; sleep 0.5; done
  for attempt in 1 2 3; do
    launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist" 2>/dev/null && break
    (( attempt == 3 )) && { echo "  couldn't start $label — try: $0 restart"; return 0; }
    sleep 2
  done
  echo "  started $label"
}

case "${1:-install}" in
  install)
    mkdir -p "$AGENTS" "$LOGS"
    [[ -x .venv/bin/amo ]] || { echo "Run ./scripts/setup-mac.sh first."; exit 1; }
    # Keep Ollama running at login too — unless the Ollama menu-bar app already handles it.
    if ! pgrep -xq Ollama; then
      OBIN="$(command -v ollama || echo "$OLLAMA_APP_BIN")"
      if [[ -x "$OBIN" ]]; then
        pkill -f "ollama serve" 2>/dev/null || true   # replace any manually started server
        sleep 1
        plist "$OLLAMA_LABEL" "$OBIN" serve
      fi
    fi
    # AMO_LAN=1 (set by `amo lan on`) lets a body device on your Wi-Fi reach the brain.
    HOST=127.0.0.1; [[ "$(env_val AMO_LAN)" == "1" ]] && HOST=0.0.0.0
    plist "$AMO_LABEL" "$ROOT/.venv/bin/amo" serve --host "$HOST" --port 8765
    if [[ -x .venv-webui/bin/open-webui ]]; then
      KEY="$(env_val AMO_API_KEY)"
      plist "$WEBUI_LABEL" "$ROOT/.venv-webui/bin/open-webui" serve --port 3000 -- \
        DATA_DIR "$ROOT/data/open-webui" \
        OLLAMA_BASE_URL http://localhost:11434 \
        OPENAI_API_BASE_URLS http://localhost:8765/v1 OPENAI_API_KEYS "$KEY" DEFAULT_MODELS amo \
        AUDIO_STT_ENGINE openai AUDIO_STT_OPENAI_API_BASE_URL http://localhost:8765/v1 \
        AUDIO_STT_OPENAI_API_KEY "$KEY" AUDIO_STT_MODEL whisper-1 \
        AUDIO_TTS_ENGINE openai AUDIO_TTS_OPENAI_API_BASE_URL http://localhost:8765/v1 \
        AUDIO_TTS_OPENAI_API_KEY "$KEY" AUDIO_TTS_MODEL piper AUDIO_TTS_VOICE default \
        ANONYMIZED_TELEMETRY false \
        ENABLE_TITLE_GENERATION false ENABLE_TAGS_GENERATION false \
        ENABLE_FOLLOW_UP_GENERATION false ENABLE_AUTOCOMPLETE_GENERATION false
    fi ;;
  uninstall)
    for l in "$AMO_LABEL" "$WEBUI_LABEL" "$OLLAMA_LABEL"; do
      launchctl bootout "gui/$(id -u)/$l" 2>/dev/null || true
      rm -f "$AGENTS/$l.plist"
    done
    echo "AMO will no longer start at login." ;;
  restart)
    # Restart without reloading the plists (picks up .env changes).
    for l in "$OLLAMA_LABEL" "$AMO_LABEL" "$WEBUI_LABEL"; do
      [[ -f "$AGENTS/$l.plist" ]] || continue
      launchctl bootstrap "gui/$(id -u)" "$AGENTS/$l.plist" 2>/dev/null || true
      launchctl kickstart -k "gui/$(id -u)/$l" && echo "  restarted $l"
    done ;;
  status) launchctl list | grep -E "com\.amo\." || echo "not running" ;;
  logs)   tail -n 50 -f "$LOGS"/*.log ;;
  *) echo "usage: $0 install|restart|uninstall|status|logs"; exit 1 ;;
esac
