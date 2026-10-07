# Modd — your local personal AI operating system

A "Jarvis" for your studio, Cravvr, reselling and trading that runs **100% on your own
hardware** — no API fees, no subscription, your data never leaves your machine — and
**learns from every conversation**, getting more useful each week.

| Piece | What it does |
|---|---|
| **Ollama** | Runs the AI models locally (Llama 3.1 8B by default) |
| **Open WebUI** | Chat interface (browser, phone on your Wi-Fi), with mic + read-aloud |
| **Modd core** (this repo) | Memory, business tools, learning, voice, dashboard, reminders |
| **SQLite** | One file (`data/modd.db`) holds all memory and business data |
| **Whisper** / **Piper** | Local speech-to-text / text-to-speech |

This is **Phase 1** (desktop). Phases 2 (Raspberry Pi, "Hey Modd") and 3 (ESP32 touchscreen)
are planned in [docs/ROADMAP.md](docs/ROADMAP.md) — Phase 1 is built so they plug straight in.

---

## What Modd can do

Just talk to it in Open WebUI (pick the **modd** model) or say it out loud:

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
trading, reselling, notifications, and a quick "ask Modd" box.

---

## Setup (desktop PC)

### Option A — Docker (recommended, Windows/Mac/Linux)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/).
2. ```sh
   git clone <this repo> && cd AMOAI
   cp .env.example .env          # set MODD_OWNER_NAME and a random MODD_API_KEY
   docker compose up -d --build
   ./scripts/pull-models.sh      # Windows: .\scripts\pull-models.ps1
   ```
3. Open **http://localhost:3000** (Open WebUI), create your local account, choose the **modd** model.
4. Teach it about you: copy `docs/about-me.example.md` → `docs/about-me.md`, edit, then
   `docker compose exec modd modd import docs/about-me.md` (or just tell it in chat).

Have an NVIDIA GPU? Uncomment the `deploy:` block under `ollama` in `docker-compose.yml`.

### Option B — Native Python

```sh
# 1. Install Ollama from https://ollama.com, then:
ollama pull llama3.1:8b && ollama pull llama3.2:3b && ollama pull nomic-embed-text

# 2. Modd (Python 3.10+)
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[voice]"
cp .env.example .env
modd setup-voice      # downloads the Piper voice
modd doctor           # checks everything
modd serve            # API + dashboard + scheduler on :8765

# 3. Open WebUI (separate terminal)
pip install open-webui && open-webui serve            # http://localhost:8080
```
In Open WebUI → **Admin Settings → Connections → OpenAI API**: add `http://localhost:8765/v1` with your
`MODD_API_KEY`. For voice, **Admin Settings → Audio**: set STT and TTS engine to *OpenAI* with the same URL/key
(TTS model `piper`).

### Picking a model

| Your PC | `MODD_CHAT_MODEL` |
|---|---|
| 8 GB RAM, no GPU | `llama3.2:3b` |
| 16 GB RAM or 8 GB GPU | `llama3.1:8b` (default) or `qwen2.5:7b` |
| 24 GB+ GPU | `qwen2.5:14b` / `qwen2.5:32b` |

The model must support tool calling (Llama 3.1+, Qwen 2.5+, Mistral Nemo all do).

---

## CLI

```
modd chat               chat in the terminal (shows which tools it used)
modd voice              push-to-talk: press Enter, talk, Modd answers out loud
modd brief              today's brief
modd remember "..."     save a fact          modd memories [search]   list / search memory
modd import FILE        bulk-import facts    modd learn / modd reflect run learning now
modd doctor             health check         modd serve               run the server
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
   Edit or delete anything via the API (`/api/memories`) or `modd memories`.

## API

Everything is also available over HTTP (`Authorization: Bearer $MODD_API_KEY`); interactive docs at
**http://localhost:8765/docs**. Highlights: `/v1/chat/completions`, `/v1/audio/transcriptions`,
`/v1/audio/speech` (OpenAI-compatible), `/api/ask`, `/api/dashboard`, `/api/clients`, `/api/sessions`,
`/api/payments`, `/api/resale`, `/api/trades`, `/api/rules`, `/api/goals`, `/api/cravvr`, `/api/revenue`,
`/api/memories`, `/api/reflect`.

## Backups

All your data is one file: `data/modd.db`. Copy it somewhere safe (it's also what you'll move to the Pi in Phase 2).

## Development

```sh
pip install -e ".[dev]" && pytest
```
Add a new ability: write a function in `modd/tools.py` with the `@tool(...)` decorator — the model can use it immediately.
