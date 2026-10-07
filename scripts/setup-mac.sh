#!/usr/bin/env bash
# One-shot native setup for macOS (Apple Silicon recommended). Safe to re-run.
#   ./scripts/setup-mac.sh            install everything
#   ./scripts/setup-mac.sh --no-webui skip Open WebUI (use the AMO dashboard / CLI only)
set -euo pipefail
cd "$(dirname "$0")/.."
WEBUI=1; [[ "${1:-}" == "--no-webui" ]] && WEBUI=0

say() { printf "\n\033[1m▸ %s\033[0m\n" "$*"; }
[[ "$(uname)" == "Darwin" ]] || { echo "This script is for macOS."; exit 1; }

if ! command -v brew >/dev/null; then
  say "Installing Homebrew"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
fi

say "Installing Ollama, Python 3.11 and ffmpeg"
brew install ollama python@3.11 ffmpeg portaudio
PY="$(brew --prefix python@3.11)/bin/python3.11"

say "Starting Ollama (runs in the background, uses the Apple GPU)"
brew services start ollama >/dev/null
for _ in $(seq 1 30); do curl -fs http://localhost:11434/api/tags >/dev/null && break; sleep 1; done

if [[ ! -f .env ]]; then
  say "Creating .env"
  cp .env.example .env
  KEY="$(openssl rand -hex 16)"
  sed -i '' "s/^AMO_API_KEY=.*/AMO_API_KEY=$KEY/" .env
  TZ_NAME="$(readlink /etc/localtime | sed 's#.*/zoneinfo/##')"
  [[ -n "$TZ_NAME" ]] && sed -i '' "s#^AMO_TIMEZONE=.*#AMO_TIMEZONE=$TZ_NAME#" .env
  # Pick a chat model that fits this Mac's memory.
  MEM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
  if   (( MEM_GB >= 32 )); then MODEL="qwen2.5:14b"
  elif (( MEM_GB >= 16 )); then MODEL="llama3.1:8b"
  else MODEL="llama3.2:3b"; fi
  sed -i '' "s/^AMO_CHAT_MODEL=.*/AMO_CHAT_MODEL=$MODEL/" .env
  echo "  ${MEM_GB}GB RAM → chat model $MODEL (change AMO_CHAT_MODEL in .env anytime)"
fi

say "Downloading models (first time takes a while)"
./scripts/pull-models.sh

say "Installing AMO"
[[ -d .venv ]] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[voice]"
.venv/bin/amo setup-voice

if (( WEBUI )); then
  say "Installing Open WebUI (separate environment)"
  [[ -d .venv-webui ]] || "$PY" -m venv .venv-webui
  .venv-webui/bin/pip install -q --upgrade pip
  .venv-webui/bin/pip install -q open-webui
fi

say "Starting AMO at login"
./scripts/mac-autostart.sh install

say "Checking everything"
.venv/bin/amo doctor || true

cat <<MSG

✅ AMO is installed.
   Dashboard:   http://localhost:8765
$( (( WEBUI )) && echo "   Open WebUI:  http://localhost:3000  (first start takes ~1 min; create your account, pick the 'amo' model)")
   Terminal:    source .venv/bin/activate && amo chat     (voice: amo voice)

Teach AMO about you:  cp docs/about-me.example.md docs/about-me.md, edit it, then  amo import docs/about-me.md
MSG
