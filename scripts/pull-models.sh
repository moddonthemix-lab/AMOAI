#!/usr/bin/env sh
# Download the local models AMO uses. Safe to re-run.
set -e
. ./.env 2>/dev/null || true
CHAT=${AMO_CHAT_MODEL:-llama3.1:8b}
FAST=${AMO_FAST_MODEL:-llama3.2:3b}
EMBED=${AMO_EMBED_MODEL:-nomic-embed-text}

APP_BIN="/Applications/Ollama.app/Contents/Resources/ollama"
if command -v ollama >/dev/null; then
  OLLAMA="ollama"   # native (macOS: uses the Apple Silicon GPU)
elif [ -x "$APP_BIN" ]; then
  OLLAMA="$APP_BIN"
elif command -v docker >/dev/null && [ -n "$(docker compose ps -q ollama 2>/dev/null)" ]; then
  OLLAMA="docker compose exec ollama ollama"
else
  echo "Ollama not found. On a Mac: install the app from https://ollama.com/download" >&2
  exit 1
fi
for m in "$CHAT" "$FAST" "$EMBED"; do
  echo "→ pulling $m"
  $OLLAMA pull "$m"
done
echo "Done. Models: $CHAT (chat), $FAST (background learning), $EMBED (memory search)."
