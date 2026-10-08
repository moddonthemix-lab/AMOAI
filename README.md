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
- **Web** — anything current: "what's the latest on the Fed?", "how do I price a mixing session?" —
  AMO searches the web (DuckDuckGo, no key), reads the pages and names its source
- **The Strat** — "What's your input on Amazon or Tesla setups?", "How does NVDA look?",
  "Pull up the weekly chart for NQ" (see below)
- **Revenue** — "How much did I make this month?" (studio + Cravvr + reselling profit + trading P&L)
- **Memory** — tell it anything ("My engineer Marcus works Tuesdays") and it remembers. It also pulls facts
  out of conversations automatically, and every Sunday night writes a **weekly reflection** (what's working,
  what's slipping, what to focus on) that it carries into every future conversation.
- **Automatic**: 8am morning brief, reminders 2 hours before studio sessions (with a ready-to-send text
  for the client), optional phone push via [ntfy](https://ntfy.sh).

Dashboard: **http://localhost:8765** — revenue vs. target, today's sessions, goals (tap to check in),
trading, reselling, notifications, and a quick "ask AMO" box.

---

## The Strat

AMO reads charts with The Strat (Rob Smith), computed from live Yahoo Finance data — stocks, ETFs,
futures (NQ, ES, CL, GC…) and crypto (BTC, ETH):

- candle numbers **1 / 2U / 2D / 3** on Monthly, Weekly and Daily ("the break decides the number")
- **timeframe continuity**: price vs the Quarterly, Monthly, Weekly, Daily and 60-minute opens (FTFC),
  the **most 2s**, and lower-timeframe override
- setups **2-1-2, 3-1-2, 1-2-2 Rev Strat, 2-2, 3-2-2, 2-2-2, hammer/shooter** — trigger (break of the
  last bar's high/low), target (the previous range), stop, risk:reward, triggered or waiting
- **AMO's take**: an A/B/C grade for the best setup in the direction of continuity, or "stand aside"

**Trading coach.** Log a trade ("short NQ at 18000, stop 18020") and AMO checks it against your own
rules and the Strat read on the spot: *"⚠ against continuity — NQ is bullish; ⚠ breaks your rule 'No
trades in the first 5 minutes' (it's 9:32)."* Ask first — *"Should I go long Amazon at 256 with a stop
at 251 and a target of 268?"* — for a pre-trade checklist (continuity, best setup, risk:reward,
your rules). *"How's my trading this week?"* reviews P&L, win rate, results with vs against
continuity, what rule breaks cost you, best/worst setup and one lesson. Rules it checks
automatically: first-N-minutes, no trades before/after a time, max trades per day, always use a
stop, minimum risk:reward, only with continuity — anything else it reminds you of.

Rules follow thestrat.ai's *3 Universal Truths* and thestrat-indicators.com. Every number is
calculated — never guessed by the AI. The **Charts** tab on the dashboard draws the candles with
their Strat numbers, key levels, continuity badges and the full thesis. Not financial advice.

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

While it works on an answer, AMO says a quick line — "Okay, let me think." for questions,
"Got it." / "I'll work on that now." for tasks. Change them in `.env` (`AMO_ACK_THINK`,
`AMO_ACK_ACTION`, phrases separated by `|`) or turn them off with `AMO_ACKS=0`.

Start it at login: System Settings → General → Login Items → **+** → `AMO Listen.command`.
Tuning: `amo listen -v` prints what it hears. If it wakes too easily set `AMO_WAKE_SENSITIVITY=5`
in `.env`; if it misses you, `2`. If Whisper keeps spelling the name differently, add it:
`AMO_WAKE_WORDS=amor,emu`.

**Speak while thinking:** AMO starts talking as soon as the first sentence of the answer is
written, instead of waiting for the whole thing. (`AMO_STREAM_SPEECH=0` turns streaming off.)

**Interrupting AMO** — if it misheard or misunderstood you, just talk over it. Only deliberate
phrases at the *start* of what you say cut it off (a word mid-sentence, or AMO hearing itself, doesn't):

| Say | AMO |
|---|---|
| "No, I said Thursday" / "I meant Tesla" / "No, book him Thursday" | stops, undoes anything it just saved from the misheard request, does what you meant |
| "You misheard me" / "That's not what I said" | stops, undoes it, asks "Sorry — what did you say?" |
| "Stop" / "Wait" / "Hold on" / "Never mind" | stops (say a new request right after if you like) |
| "AMO, what's my schedule?" | stops and answers the new question |

The face (`/face`) shows what AMO heard you say, so you can spot a mishearing straight away.
Typing works too: "undo that" (anything it added in the last 30 minutes) or "no, I meant Thursday".
Undo removes new things (goals, bookings, payments, trades, memories, watchlist tickers, text drafts);
for a change to an existing item, just tell AMO what it should be. `AMO_BARGE_IN=0` turns interrupting off.

### Calendar, texting clients, backups

- **Calendar**: `amo calendar` subscribes Apple Calendar to AMO (studio sessions + Cravvr due dates);
  it refreshes itself every 15 minutes. From another device: `http://Your-Mac.local:8765/calendar.ics?key=YOUR_KEY`.
- **Texting clients** (iMessage, from the Mac's Messages app): *"Text Jay that the session moved to 8"*
  → AMO shows the draft → say **"send it"** or **"cancel"**. Nothing is ever sent without your OK
  (drafts expire after 10 minutes). macOS asks once to let AMO control Messages — allow it.
- **Backups**: every night after 3am AMO copies its data to iCloud Drive → *AMO Backups* (or
  `~/AMO/backups`), keeping 14 days. `amo backup` backs up now; `amo restore FILE` restores one.

### AMO's face

Open **http://localhost:8765/face** (also linked from the dashboard). Two glowing eyes and a mouth:
blinks and glances around when idle, eyes widen when listening, look up with "…" while thinking,
the mouth moves with the actual loudness of AMO's voice, and **every word appears one at a time as
it's spoken**. Click to go full screen. Driven by whatever is speaking — AMO Listen on the Mac or a
body device — so it's ready for the body's screen too.

### AMO speaks up on its own

While AMO Listen (or a body device) is running, AMO talks without being asked — never during quiet
hours (`AMO_QUIET_HOURS=22-8`):
- **Strat watchlist** — "add Amazon and Tesla to my watchlist", "how's my watchlist looking?".
  During market hours AMO checks every 15 minutes and says *"Heads up: AMZN just triggered the
  weekly 2D-2U reversal above 253.56, with continuity…"* (each alert once). Crypto 24/7.
- **Studio** — a heads-up 15 minutes before a session, unpaid balances at the end of the day,
  clients who've gone quiet on Monday mornings.
- **Daily rhythm** — the morning brief out loud, an evening check-in on your goals
  (`AMO_CHECKIN_HOUR=21`).
Everything also lands in the dashboard's notifications. Turn it all off with `AMO_PROACTIVE=0`.

### AMO's body (Raspberry Pi or any computer on your Wi-Fi)

The body is just ears and a mouth — the thinking stays on your Mac (the brain).

1. On the Mac: `amo lan on` — prints the brain's address and key (other devices need the key; your
   Mac never does).
2. On the device: install AMO (`pip install -e ".[voice]"`), then
   `amo device --brain http://Your-Mac.local:8765 --key THE_KEY`

The device streams "Got it" and each sentence's audio from the brain as it's ready, supports
interruptions and "good morning", and works with any USB mic/speaker. `amo lan off` closes it again.

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
