#!/usr/bin/env bash
# One-shot native setup for macOS — Apple Silicon or Intel. Safe to re-run.
# No Homebrew needed (Homebrew compiles everything from source on Intel Macs now).
#   ./scripts/setup-mac.sh            install everything
#   ./scripts/setup-mac.sh --no-webui skip Open WebUI (use the AMO dashboard / CLI only)
set -euo pipefail
cd "$(dirname "$0")/.."
WEBUI=1; [[ "${1:-}" == "--no-webui" ]] && WEBUI=0
PYVER=3.11   # Open WebUI supports 3.11–3.12

step() { printf "\n\033[1m▸ %s\033[0m\n" "$*"; }
warn() { printf "\033[33m  ! %s\033[0m\n" "$*"; }
[[ "$(uname)" == "Darwin" ]] || { echo "This script is for macOS."; exit 1; }
ARCH="$(uname -m)"   # arm64 = Apple Silicon, x86_64 = Intel
MEM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
echo "Mac: $ARCH, ${MEM_GB}GB RAM"

# ---------------------------------------------------------------- Ollama
OLLAMA_APP_BIN="/Applications/Ollama.app/Contents/Resources/ollama"
if ! command -v ollama >/dev/null && [[ ! -x "$OLLAMA_APP_BIN" ]]; then
  step "Installing Ollama (official app)"
  TMP="$(mktemp -d)"
  curl -fL --progress-bar -o "$TMP/Ollama.zip" https://ollama.com/download/Ollama-darwin.zip \
    || { echo "Download failed. Install Ollama from https://ollama.com/download, then re-run this script."; exit 1; }
  ditto -xk "$TMP/Ollama.zip" /Applications/
  rm -rf "$TMP"
fi
step "Starting Ollama"
ollama_up() { curl -fs http://localhost:11434/api/tags >/dev/null; }
wait_ollama() { for _ in $(seq 1 "$1"); do ollama_up && return 0; sleep 1; done; return 1; }
if ! ollama_up; then
  # Open by path: a just-copied app isn't registered with macOS yet, so `open -a Ollama` fails.
  [[ -d /Applications/Ollama.app ]] && open -g /Applications/Ollama.app 2>/dev/null || true
  if ! wait_ollama 20; then
    # Fall back to running the server directly (AMO's login item keeps it running later).
    BIN="$(command -v ollama || echo "$OLLAMA_APP_BIN")"
    mkdir -p data/logs
    nohup "$BIN" serve >data/logs/ollama.log 2>&1 &
    wait_ollama 30 || { echo "Ollama didn't start — see data/logs/ollama.log, or open Ollama from Applications and re-run."; exit 1; }
  fi
fi
echo "  Ollama is running"

# ---------------------------------------------------------------- uv (Python manager)
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
  step "Installing uv (fast Python installer, no Homebrew needed)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# ---------------------------------------------------------------- .env
if [[ ! -f .env ]]; then
  step "Creating .env"
  cp .env.example .env
  sed -i '' "s/^AMO_API_KEY=.*/AMO_API_KEY=$(openssl rand -hex 16)/" .env
  TZ_NAME="$(readlink /etc/localtime | sed 's#.*/zoneinfo/##')"
  [[ -n "$TZ_NAME" ]] && sed -i '' "s#^AMO_TIMEZONE=.*#AMO_TIMEZONE=$TZ_NAME#" .env
  # Intel Macs run models on the CPU only, so keep the model small there.
  if [[ "$ARCH" == "x86_64" ]]; then MODEL="llama3.2:3b"
  elif (( MEM_GB >= 32 )); then MODEL="qwen2.5:14b"
  elif (( MEM_GB >= 16 )); then MODEL="llama3.1:8b"
  else MODEL="llama3.2:3b"; fi
  sed -i '' "s/^AMO_CHAT_MODEL=.*/AMO_CHAT_MODEL=$MODEL/" .env
  echo "  chat model: $MODEL (change AMO_CHAT_MODEL in .env anytime)"
fi

step "Downloading AI models (first time takes a while)"
./scripts/pull-models.sh

# ---------------------------------------------------------------- AMO
step "Installing AMO"
[[ -x .venv/bin/python ]] || uv venv --python "$PYVER" .venv
uv pip install --python .venv/bin/python -q -e .

step "Installing voice (Whisper speech-to-text, Piper text-to-speech)"
for pkg in "numpy>=1.26" "sounddevice>=0.4" "faster-whisper>=1.0"; do
  uv pip install --python .venv/bin/python -q "$pkg" || warn "$pkg failed to install — voice input may not work"
done
if uv pip install --python .venv/bin/python -q "piper-tts>=1.3" 2>/dev/null; then
  .venv/bin/amo setup-voice || warn "couldn't download AMO's voice — it will use the macOS voice"
else
  warn "Piper isn't available for this Mac — AMO will speak with the built-in macOS voice (that's fine)"
fi

if (( WEBUI )); then
  step "Installing Open WebUI (separate environment, takes a few minutes)"
  [[ -x .venv-webui/bin/python ]] || uv venv --python "$PYVER" .venv-webui
  uv pip install --python .venv-webui/bin/python -q open-webui
fi

step "Starting AMO at login"
./scripts/mac-autostart.sh install

step "Checking everything"
.venv/bin/amo doctor || true

cat <<MSG

✅ AMO is installed.
   Dashboard:   http://localhost:8765
$( (( WEBUI )) && echo "   Open WebUI:  http://localhost:3000  (first start takes 1–2 min; create your account, pick the 'amo' model)")
   Terminal:    source .venv/bin/activate && amo chat     (voice: amo voice)

Teach AMO about you:  cp docs/about-me.example.md docs/about-me.md, edit it, then  amo import docs/about-me.md
MSG
