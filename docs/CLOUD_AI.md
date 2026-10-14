# Cloud AI (the Family Hub's shared brain)

AMO can answer some things with Claude in the cloud, through the Family Hub's shared brain
(`amo/family/family_brain.py`, copied unchanged from the hub repo). The hub sets the monthly budget and
records every cent. Everything is off until you switch it on.

## What goes to the cloud

| What you ask | Who answers |
|---|---|
| Conversation, advice, explanations, writing ("explain how compound interest works") | ☁️ Claude Haiku |
| Coding questions and pasted errors | ☁️ Claude Sonnet |
| "big job: …" / "think hard about …" (only with `AMO_CLOUD_BIG=true`) | ☁️ Claude Opus |
| Saves, lookups, your schedule, money, charts and The Strat, alerts, timers, the printer, weather, news, anything live | 🏠 local model with its tools, as before |

If the hub says no (budget used up, no internet, hub asleep), AMO answers locally like it always has.
If Claude declines a request, AMO says so and does **not** try again on the local model.

## Setup

1. On the hub computer, make AMO a token (once; running it again replaces the old token):
   `python3 -m hub agent token amo`
2. In `~/AMO/.env` add:
   ```
   CLOUD_FOR_PRIVATE_CHATS=true
   ANTHROPIC_API_KEY=…the family key…
   HUB_URL=http://192.168.1.208:8770
   HUB_TOKEN=…from step 1…
   ```
   The key lives only in `.env` (git ignores it): never in code, chats or the hub.
3. Hub → Admin → ☁️ Cloud AI budgets: give `amo` a budget (start with $1).
4. `~/AMO/scripts/update-mac.sh` (installs `anthropic`), then `amo cloud --test`.

`amo cloud` shows the status and this month's budget. `amo stats` shows cloud vs local answers and
the spend. The dashboard marks each answer ☁️ or 🏠.

## Rules (from the hub's ADDING_CLOUD_AI.md)

- Claude is only called through `family_brain`, so spending is always checked and recorded.
- `amo/family/*.py` are never edited in AMO; fixes go to the hub repo and are copied in again.
- A refused request is never retried on the local model.
- The API key is never logged, printed, committed or posted.
- Your memories go into the cloud prompt only when `CLOUD_FOR_PRIVATE_CHATS=true`.

AMO's one adaptation (in `amo/cloud.py`, not in the shared file): when the brain decides "local", AMO
answers with its own local path (its chosen model, tools and streaming) instead of the shared brain's
plain Ollama call.

## Asked of the hub repo

`family_brain.think()` has no tool support and doesn't stream. If it accepted tools (and streamed),
AMO could send its lookups and actions to the cloud too, under the same budget. Until then those stay
local.
