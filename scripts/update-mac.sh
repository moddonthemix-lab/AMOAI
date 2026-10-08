#!/usr/bin/env bash
# Update AMO to the latest version. Keeps your settings (.env), data, models and Open WebUI account.
#   ./scripts/update-mac.sh                 update in place
#   ./scripts/update-mac.sh --move ~/AMO    update, then move AMO to a permanent folder
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
REPO="moddonthemix-lab/amoai"
BRANCH="${AMO_BRANCH:-claude/adoring-babbage-istu44}"
export PATH="$HOME/.local/bin:$PATH"
step() { printf "\n\033[1m▸ %s\033[0m\n" "$*"; }

MOVE_TO=""
if [[ "${1:-}" == "--move" ]]; then
  MOVE_TO="${2:?usage: $0 --move /path/to/new/folder}"
  MOVE_TO="${MOVE_TO/#\~/$HOME}"
fi

step "Downloading the latest AMO"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH" | tar -xz -C "$TMP"
SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
# The download only contains code (no .env, data, models or Python environments),
# so copying it over the install can't touch your settings or data.
cp -R "$SRC/." "$ROOT/"
chmod +x "$ROOT"/scripts/*.sh
echo "  code updated"

if [[ -n "$MOVE_TO" && "$MOVE_TO" != "$ROOT" ]]; then
  step "Moving AMO to $MOVE_TO"
  if [[ -e "$MOVE_TO" ]]; then
    echo "  $MOVE_TO already exists. Rename or delete it first, then re-run."; exit 1
  fi
  ./scripts/mac-autostart.sh uninstall
  mkdir -p "$(dirname "$MOVE_TO")"
  mv "$ROOT" "$MOVE_TO"
  cd "$MOVE_TO"
  # Python environments contain absolute paths, so rebuild them in the new place (fast: cached).
  rm -rf .venv .venv-webui
  ./scripts/setup-mac.sh
  echo
  echo "AMO now lives in: $MOVE_TO"
  echo "Next time, update with:  $MOVE_TO/scripts/update-mac.sh"
  exit 0
fi

step "Updating packages"
if command -v uv >/dev/null && [[ -x .venv/bin/python ]]; then
  uv pip install --python .venv/bin/python -q -e .
elif [[ -x .venv/bin/pip ]]; then
  .venv/bin/pip install -q -e .
fi

step "Updating voice (speaking + listening)"
if command -v uv >/dev/null && [[ -x .venv/bin/python ]]; then
  uv pip install --python .venv/bin/python -q numpy "piper-tts>=1.3" 2>/dev/null \
    || echo "  Piper isn't available for this Mac — AMO will use the built-in Mac voice"
  for pkg in "sounddevice>=0.4" "faster-whisper>=1.0"; do
    uv pip install --python .venv/bin/python -q "$pkg" 2>/dev/null || echo "  couldn't install $pkg (needed for voice input)"
  done
fi
# One-time: switch to AMO's final voice (clean Obadiah). Later voice choices are left alone.
if [[ ! -f data/.voice-amo ]]; then
  .venv/bin/amo set-voice amo >/dev/null 2>&1 || true
  mkdir -p data && touch data/.voice-amo
fi
# One-time: interrupting is back on (stricter now — only "No, I said…", "Stop", "Wait" at the start).
if [[ ! -f data/.barge-in-v2 ]]; then
  [[ -f .env ]] && sed -i '' 's/^AMO_BARGE_IN=0$/AMO_BARGE_IN=1/' .env 2>/dev/null || true
  mkdir -p data && touch data/.barge-in-v2
fi
.venv/bin/amo setup-voice 2>/dev/null || true

step "Restarting AMO"
./scripts/mac-autostart.sh restart
sleep 3
if curl -fs localhost:8765/api/health >/dev/null; then
  echo -e "\n✅ AMO is updated and running."
else
  echo -e "\n⚠ AMO didn't answer yet — give it a few seconds, or check: ./scripts/mac-autostart.sh logs"
fi
