#!/usr/bin/env sh
# Download the local models AMO uses. Safe to re-run.
set -e
. ./.env 2>/dev/null || true
CHAT=${AMO_CHAT_MODEL:-llama3.1:8b}
FAST=${AMO_FAST_MODEL:-llama3.2:3b}
EMBED=${AMO_EMBED_MODEL:-nomic-embed-text}

if command -v ollama >/dev/null; then
  OLLAMA="ollama"   # native (macOS: uses the Apple Silicon GPU)
elif command -v docker >/dev/null && [ -n "$(docker compose ps -q ollama 2>/dev/null)" ]; then
  OLLAMA="docker compose exec ollama ollama"
else
  echo "Ollama not found. On a Mac: brew install ollama && brew services start ollama" >&2
  exit 1
fi
for m in "$CHAT" "$FAST" "$EMBED"; do
  echo "→ pulling $m"
  $OLLAMA pull "$m"
done
echo "Done. Models: $CHAT (chat), $FAST (background learning), $EMBED (memory search)."
