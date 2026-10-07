# AMO — your local personal AI operating system

A "Jarvis" for your studio, Cravvr, reselling and trading that runs **100% on your own
hardware** — no API fees, no subscription, your data never leaves your machine — and
**learns from every conversation**, getting more useful each week.

| Piece | What it does |
|---|---|
| **Ollama** | Runs the AI models locally (Llama 3.1 8B by default) |
| **Open WebUI** | Chat interface in your browser (also your phone on the same Wi-Fi), with mic + read-aloud |
| **AMO core** (this repo) | Memory, business tools, learning, voice, dashboard, reminders |
| **SQLite** | One file (`data/amo.db`) holds all memory and business data |
| **Whisper** / **Piper** | Local speech-to-text / text-to-speech |

This is **Phase 1** (your Mac). Phases 2 (Raspberry Pi, "Hey AMO") and 3 (ESP32 touchscreen)
are planned in [docs/ROADMAP.md](docs/ROADMAP.md) — Phase 1 is built so they plug straight in.

---

## What AMO can do

Just talk to it in Open WebUI (pick the **amo** model) or say it out loud:

- **Studio CRM** — "Add a client Jay Carter, artist name Lil Jay, 555-0101" · "Book Lil Jay Friday at 7pm,
  3 hours at $50" · "Mark session 12 done, he paid $100 cash app" · "Who owes me money?" ·
  "Who hasn't booked in 6 weeks?"
- **Reselling** — "Bought Jordan 4 Bred size 10 for $210" · "Sold the Jordans on StockX for $320, $30 fees,
  $15 shipping" · "What's sitting too long?" · "Reselling profit this month?"
- **Trading journal** — "Long NQ at 18000, stop 17980, ORB setup" · "Closed trade 4 at 18040, followed my
  rules" · "How do I do when I break my rules?" · "Add a rule: no trades after 11am"
- **Goals** — "Add a daily goal: make one beat" · "Done with the beat" · streaks on the dashboard
- **Cravvr** — "Add a Cravvr task: finalize menu pricing, high priority" · "What's open for Cravvr?"
- **Revenue** — "How much did I make this month?" (studio + Cravvr + reselling profit + trading P&L)
- **Memory** — tell it anything ("My engineer Marcus works Tuesdays") and it remembers. It also pulls facts
  out of conversations automatically, and every Sunday night writes a **weekly reflection** (what's working,
  what's slipping, what to focus on) that it carries into every future conversation.
- **Automatic**: 8am morning brief, reminders 2 hours before studio sessions (with a ready-to-send text
  for the client), optional phone push via [ntfy](https://ntfy.sh).

Dashboard: **http://localhost:8765** — revenue vs. target, today's sessions, goals (tap to check in),
trading, reselling, notifications, and a quick "ask AMO" box.

---

## Setup (Mac)

Works on Apple Silicon and Intel Macs (macOS 14 Sonoma or newer). Ollama runs **natively** so models
use the Apple Silicon GPU (Docker on a Mac can't use the GPU). Intel Macs run models on the CPU, so
setup picks a smaller, faster model there.

### Option A — one command (recommended)

```sh
git clone <this repo> && cd AMOAI
./scripts/setup-mac.sh
```

No Homebrew needed. It installs the Ollama app and Python (via [uv](https://docs.astral.sh/uv/));
picks a model that fits your Mac; downloads the models; installs AMO with voice and Open WebUI; creates `.env` with a random
API key and your timezone; and sets AMO + Open WebUI to **start automatically at login**.

Then open **http://localhost:3000** (Open WebUI), create your local account, and pick the **amo** model.
Dashboard: **http://localhost:8765**.

Auto-start control: `./scripts/mac-autostart.sh status | logs | uninstall`.

### Option B — Docker for AMO + Open WebUI, native Ollama

```sh
# install the Ollama app from https://ollama.com/download and open it
./scripts/pull-models.sh
cp .env.example .env          # set a random AMO_API_KEY
docker compose up -d --build  # needs Docker Desktop
```

### Teach AMO about you
```sh
cp docs/about-me.example.md docs/about-me.md   # edit it
source .venv/bin/activate && amo import docs/about-me.md
# (Docker: docker compose exec amo amo import docs/about-me.md)
```
Or just tell it things in chat — it remembers.

### Voice on the Mac (AMO talks *and* shows text)
- **AMO dashboard** (http://localhost:8765): click 🎤, talk, click ■. Your words and AMO's reply show
  as text, and with 🔊 on AMO reads the reply out loud. Click 🔊 to mute.
- **Open WebUI**: click 🎤 to talk. To hear every reply while still seeing the text, go to
  **Settings → Audio** and turn on **Auto-playback response**.
- In Terminal: `amo voice` (press Enter, talk). The first time, macOS asks to allow microphone access
  for Terminal — say yes (System Settings → Privacy & Security → Microphone).
- If Piper isn't available (e.g. on some Intel Macs), AMO speaks with the built-in macOS voice.

### Hands-free: "Hey AMO"

First run `amo mic-test` — it checks the mic, speech recognition and voice step by step.
Then double-click **AMO Listen.command** in the AMO folder (or run `amo listen`) and talk:
- "**Hey AMO**, what's on my schedule today?" — or just "Hey AMO", wait for the chime, then ask.
- Follow-ups for a few seconds after an answer don't need "Hey AMO".
- "**Good morning AMO**" reads your morning brief · "**what time is it**" · "**go to sleep**" /
  "**Hey AMO, wake up**" · "**never mind**".

Start it at login: System Settings → General → Login Items → **+** → `AMO Listen.command`.
Tuning: `amo listen -v` prints what it hears. If it wakes too easily set `AMO_WAKE_SENSITIVITY=5`
in `.env`; if it misses you, `2`. If Whisper keeps spelling the name differently, add it:
`AMO_WAKE_WORDS=amor,emu`.

### AMO's voice

AMO speaks as **Obadiah**, a clean, deadpan British voice (Piper), with a dry-wit personality to match.
It downloads automatically. Options if you ever want a change:
```sh
amo try-voice computer     # Obadiah with a robotic edge
amo voices --all           # every Piper English voice
amo set-voice amo          # back to AMO's voice
```
Without Piper installed, AMO falls back to the Mac's built-in British voice "Daniel".

### Picking a model (Apple Silicon, by unified memory)

| Your Mac | `AMO_CHAT_MODEL` in `.env` |
|---|---|
| Intel (any RAM) | `llama3.2:3b` — CPU only; try `qwen2.5:7b` if you have 16 GB+ and can live with slower replies |
| Apple Silicon 8 GB | `llama3.2:3b` |
| Apple Silicon 16 GB | `llama3.1:8b` (default) or `qwen2.5:7b` |
| Apple Silicon 32 GB | `qwen2.5:14b` |
| Apple Silicon 64 GB+ | `qwen2.5:32b` |

Compare models on your own Mac, then switch in one command:
```sh
amo bench llama3.2:3b gemma4:e2b qwen3:4b   # times each one and checks it really saves a booking
amo use gemma4:e2b                           # downloads it, saves it to .env, restarts AMO
```

The model must support tool calling (Gemma 3 doesn't; Gemma 4 does) — Llama 3.1+, Qwen 2.5+/3, Gemma 4 and Mistral Nemo all do.

---

## CLI

```
amo chat               chat in the terminal (shows which tools it used)
amo voice              push-to-talk: press Enter, talk, AMO answers out loud
amo brief              today's brief
amo remember "..."     save a fact          amo memories [search]   list / search memory
amo import FILE        bulk-import facts    amo learn / amo reflect run learning now
amo doctor             health check         amo serve               run the server
```

## How it learns

1. **Every message** — relevant memories are retrieved (keyword + embedding search) and injected,
   along with your highest-importance facts (rules, identity, preferences).
2. **After conversations** — a small background model extracts durable facts (people, prices, rules,
   plans) and saves them; duplicates are merged.
3. **Weekly** — a reflection over the week's numbers (revenue, sessions, no-shows, trading rule adherence,
   stale inventory, goal streaks) and conversations. The summary goes into every future chat; lasting
   lessons become memories.
4. **Hygiene** — low-importance auto-learned facts that are never used fade out after ~4 months.
   Edit or delete anything via the API (`/api/memories`) or `amo memories`.

## API

Everything is also available over HTTP (`Authorization: Bearer $AMO_API_KEY`); interactive docs at
**http://localhost:8765/docs**. Highlights: `/v1/chat/completions`, `/v1/audio/transcriptions`,
`/v1/audio/speech` (OpenAI-compatible), `/api/ask`, `/api/dashboard`, `/api/clients`, `/api/sessions`,
`/api/payments`, `/api/resale`, `/api/trades`, `/api/rules`, `/api/goals`, `/api/cravvr`, `/api/revenue`,
`/api/memories`, `/api/reflect`.

## Backups

All your data is one file: `data/amo.db`. Copy it somewhere safe (it's also what you'll move to the Pi in Phase 2).

## Development

```sh
pip install -e ".[dev]" && pytest
```
Add a new ability: write a function in `amo/tools.py` with the `@tool(...)` decorator — the model can use it immediately.
