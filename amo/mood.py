"""AMO's mood: the "mood" personality reads the moment and adjusts how AMO talks.

Chill is the baseline. Each turn AMO looks at what you just said and how your day is going,
and picks a mood:

  steady  you sound stressed, tired or frustrated, or you're red on the day → calm, supportive, brief
  firm    you broke a trading rule today → calm but straight; holds you to your rules
  hype    you're celebrating, or you're green on the day → upbeat, celebrates the win
  quiet   late at night → low-key and short
  chill   everything else → laid-back, a little slang

Built to grow: add a Mood to MOODS and a signal to read() — the prompt and the instant
confirmations pick it up automatically. The current mood is kept in kv "mood" (for the face,
the dashboard, or later learning what works for you).
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .db import Database, get_db, local_now


@dataclass(frozen=True)
class Mood:
    name: str
    tone: str                   # how to talk, for the system prompt
    openers: tuple[str, ...]    # said before an instant confirmation ("Bet. Booked Jay…")


MOODS: dict[str, Mood] = {m.name: m for m in [
    Mood("chill", "laid-back and easy-going, like a cool friend from the studio who has everything "
                  "handled. Short, a little casual slang now and then (\"bet\", \"say less\", \"locked in\", "
                  "\"you're good\") — never forced, at most one per reply.",
         ("Bet.", "Say less.", "Locked in.", "You're good.", "Got you.")),
    Mood("hype", "upbeat and genuinely happy for them — celebrate the win in a few words, then keep "
                 "the momentum going (what's next). Energetic but not cheesy, no shouting.",
         ("Let's go!", "That's what I'm talking about.", "Big moves.", "Love that.")),
    Mood("steady", "calm, warm and supportive. They're having a rough moment: keep it short and "
                   "gentle, no jokes or slang, take things off their plate, and point to one simple next "
                   "step. Never lecture.",
         ("Got you.", "Handled.", "Done — one less thing.")),
    Mood("firm", "calm but straight, like a mentor who cares. They've broken their own trading rules "
                 "today: still help, but say plainly when something goes against their rules and remind "
                 "them why the rules exist. No jokes, no guilt-tripping.",
         ("Done.", "Okay.")),
    Mood("quiet", "low-key and brief — it's late. Answer in as few words as possible, softly, and "
                  "don't start new topics.",
         ("Done.", "Got it.")),
]}

_STRESS = re.compile(
    r"\b(stress(?:ed|ing)?|tired|exhausted|frustrat\w*|annoy\w*|overwhelm\w*|rough (?:day|week)|bad day|"
    r"long day|ugh+|sad|down bad|struggling|can'?t (?:do|take) this|burn(?:ed|t) out|worried|anxious|"
    r"lost (?:money|it all|again)|took a (?:loss|l)\b|fml)\b", re.I)
_HYPE = re.compile(
    r"\b(let'?s go+|lets go+|hyped|crushed it|killed it|nailed it|big win|huge win|we won|i won|made "
    r"(?:money|bank|\$?\d+)|got paid|paid me|in the green|green day|best day|finally (?:did|got|finished))\b"
    r"|🔥|🚀|💰", re.I)


def read(user_text: str = "", db: Database | None = None, now: datetime | None = None) -> tuple[Mood, str]:
    """Pick the mood for this turn. Returns (mood, why) — why is a short reason for the prompt."""
    db = db or get_db()
    now = now or local_now()
    text = user_text or ""
    if _STRESS.search(text):
        return MOODS["steady"], "they sound stressed or worn out"
    if _HYPE.search(text):
        return MOODS["hype"], "they're celebrating"

    today = now.date().isoformat()
    try:
        broke = db.scalar("SELECT COUNT(*) FROM trades WHERE followed_rules = 0 AND "
                          "substr(opened_at, 1, 10) = ?", (today,)) or 0
        from .trading import TradingJournal

        closed = TradingJournal(db).closed_since(today)
        net = sum(t.get("pnl") or 0 for t in closed)
    except Exception:  # noqa: BLE001 — mood must never break a reply
        broke, closed, net = 0, [], 0
    if broke:
        return MOODS["firm"], f"they broke their trading rules {broke} time{'s' if broke > 1 else ''} today"
    if closed and net < 0:
        return MOODS["steady"], f"they're down ${abs(net):,.0f} on trades today"
    if closed and net > 0:
        return MOODS["hype"], f"they're up ${net:,.0f} on trades today"
    if now.hour >= 23 or now.hour < 6:
        return MOODS["quiet"], "it's late"
    return MOODS["chill"], "a normal moment"


def prompt(mood: Mood, why: str) -> str:
    return ("\nPersonality: you adapt to {owner}'s mood. Your baseline is chill; right now the mood is "
            f"\"{mood.name}\" ({why}). Be {mood.tone} Whatever the mood, stay accurate with numbers, dates "
            "and money, and never use *actions in asterisks*.\n")


def set_current(mood: Mood, why: str, db: Database | None = None) -> None:
    try:
        (db or get_db()).set_kv("mood", json.dumps({"name": mood.name, "why": why,
                                                    "at": local_now().isoformat()}))
    except Exception:  # noqa: BLE001
        pass


def current(db: Database | None = None) -> Mood:
    try:
        raw = (db or get_db()).get_kv("mood")
        return MOODS.get(json.loads(raw)["name"], MOODS["chill"]) if raw else MOODS["chill"]
    except Exception:  # noqa: BLE001
        return MOODS["chill"]


def opener(db: Database | None = None) -> str:
    return random.choice(current(db).openers)


def describe(db: Database | None = None) -> dict[str, Any]:
    raw = (db or get_db()).get_kv("mood")
    return json.loads(raw) if raw else {"name": "chill", "why": "a normal moment"}
