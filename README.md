# Modd — a local personal "Jarvis"

A private assistant that knows your studio business, Cravvr, your reselling
operation, your trading rules and your goals. It runs entirely on your own
hardware through [Ollama](https://ollama.com), so there's no AI subscription
and none of your data leaves the machine.

```
you> booked Jay for Friday 7pm, 3 hours at $50
modd> Done — Jay's booked Fri Oct 9, 7–10pm, $150.

you> sold the Bred 4s on StockX for 300, fees were 30, shipping 15
modd> Marked sold. Profit $45 on the $210 buy.

you> took 10 AAPL at 100 out at 105 but I moved my stop
modd> Logged +$50. That broke "never move a stop" — 3rd break this month.
```

## Roadmap

| Phase | What | Status |
|---|---|---|
| **1. Desktop** | Local AI, memory database, voice, studio CRM, reselling tracker, trading journal, daily goals, revenue | ✅ this repo |
| **2. Raspberry Pi 5** | Same code on the Pi, always-on as a service, wake word "Hey Modd" | next |
| **3. ESP32 touchscreen** | Studio dashboard, client notifications, daily goals, revenue tracking | JSON API ready (`modd serve`) |

## Phase 1 setup (desktop)

1. Install Ollama and pull a model that supports tool calling:
   ```bash
   ollama pull llama3.1:8b        # or qwen2.5:7b — both handle tools well
   ```
2. Install Modd (Python 3.10+):
   ```bash
   python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
   pip install -e .                 # core: standard library only
   pip install -e '.[voice]'        # optional: mic + speech
   ```
3. Check it and start talking:
   ```bash
   modd doctor
   modd chat          # type to Modd
   modd voice         # press Enter, speak; add --hands-free to skip Enter
   ```

Settings are environment variables; see [`.env.example`](.env.example).
Your data lives in a single SQLite file, `~/.modd/modd.db` (change with
`MODD_HOME`). Back it up and you've backed up everything.

### Voice

- Speech-to-text: [faster-whisper](https://github.com/SYSTRAN/faster-whisper), local.
- Text-to-speech: [Piper](https://github.com/rhasspy/piper) if installed and
  `MODD_PIPER_VOICE` points to a voice `.onnx` file (sounds best); otherwise
  your OS voice through pyttsx3.

## What it remembers and tracks

The model reads and writes everything through tools, so numbers come from
the database, not from the model's imagination. You can also use the CLI
directly, without the model:

| Area | Examples |
|---|---|
| **Memory** | `modd remember Jay pays cash only --area studio` · `modd recall jay` · `modd forget 3` |
| **Studio CRM** | `modd client add Jay --phone 555-0100` · `modd session book Jay 2026-10-09T19:00 --hours 3 --rate 50` · `modd session done 1 --paid 150` · `modd session unpaid` |
| **Reselling** | `modd item add "Jordan 4 Bred" 210 --list-price 320` · `modd item sell 1 300 --fees 30 --shipping 15` · `modd item list --status sold` |
| **Trading** | `modd rule add Never move a stop` · `modd trade log AAPL 10 100 --exit 105 --broke-rules --setup breakout` · `modd trade close 4 520` · `modd trade stats` |
| **Goals** | `modd goal add Mix 2 songs --area studio` · `modd goal done 1` |
| **Revenue** | `modd revenue week` — studio payments + reselling profit + trading P&L |
| **Today** | `modd today` — goals, today's sessions, unpaid invoices, open trades, revenue |

Memory search uses SQLite full-text search, and the relevant memories,
your trading rules and a snapshot of today go into every prompt. When you
tell Modd something worth keeping, it saves it on its own.

## JSON API (for Phase 3 devices)

```bash
MODD_API_HOST=0.0.0.0 MODD_API_TOKEN=change-me modd serve
```

| Method | Path | |
|---|---|---|
| GET | `/api/health` | liveness |
| GET | `/api/dashboard` | everything the touchscreen shows |
| GET | `/api/revenue?period=day\|week\|month\|year` | revenue breakdown |
| GET | `/api/goals?day=YYYY-MM-DD` | goals |
| GET | `/api/sessions?start=&end=&status=` | studio sessions |
| POST | `/api/goals` `{"text": "...", "area": "studio"}` | add goal |
| POST | `/api/goals/<id>/done` | tick off a goal |
| POST | `/api/ask` `{"text": "..."}` | talk to Modd |

Send `Authorization: Bearer <MODD_API_TOKEN>` when a token is set. Always
set one before binding to `0.0.0.0`.

## Phase 2 notes (Raspberry Pi 5)

- Pi 5 8GB: use a small tool-capable model (`llama3.2:3b` or `qwen2.5:3b`) and
  `MODD_WHISPER_MODEL=tiny.en`. Or keep the big model on the desktop and set
  `MODD_OLLAMA_URL` to it.
- Copy `~/.modd/modd.db` over and the Pi knows everything the desktop did.
- Run `modd voice --hands-free` (later: the wake-word loop) and `modd serve`
  as systemd services.
- Wake word: put [openWakeWord](https://github.com/dscripka/openWakeWord) with a
  custom-trained "Hey Modd" model in front of `Voice.listen()`.

## Development

```bash
pip install -e '.[dev]'
pytest
```

Layout: `modd/db.py` (schema and queries) · `modd/tools.py` (functions the
model can call) · `modd/assistant.py` (prompt and tool loop) · `modd/llm.py`
(Ollama client) · `modd/voice.py` · `modd/server.py` · `modd/cli.py`.
