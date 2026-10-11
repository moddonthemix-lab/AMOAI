"""What AMO knows about how you like things: answer style preferences you've told it, and the
clients and tickers you deal with most (used in the prompt, and to help Whisper spell names).

    "From now on, give me the numbers first."     "Keep it short."     "That's too long."
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from .config import settings
from .db import Database, get_db, local_now

# "from now on, …" / "going forward …" / "I prefer short answers" / "always call me Modd"
PREF_RE = re.compile(
    r"^(?:(?:hey )?amo[ ,]*)?(?:ok(?:ay)?[ ,]+)?(?:from now on|going forward|in the future|moving forward)[ ,:-]+(?P<pref>.{3,})$"
    r"|^(?:(?:hey )?amo[ ,]*)?i (?:like|prefer|want) (?P<pref2>(?:my |your )?(?:(?:short|shorter|long|longer|quick|brief|detailed|simple) )?(?:answers?|replies|responses|it when you)\b.*"
    r"|you to (?:answer|reply|respond|talk|speak|be|keep|explain|call me|use|stop|always|never)\b.{2,})$"
    r"|^(?:(?:hey )?amo[ ,]*)?(?P<pref3>(?:always|never|please always|please never|don'?t ever) "
    r"(?:call me|answer|reply|talk|speak|say|give me|tell me|curse|swear|explain|start|end|mention|read)\b.{2,})$",
    re.I)
# Quick feedback on the last answer
FEEDBACK = [
    (re.compile(r"^(?:that(?:'s| was) )?(?:way |too )+(?:long|wordy|much)[.!]*$|^(?:shorter|be (?:more )?brief|keep it short"
                r"|less talking|too much talking)(?: please)?[.!]*$", re.I),
     "keep answers short — a sentence or two"),
    (re.compile(r"^just (?:give|tell) me the (?:number|numbers|answer|bottom line)s?(?: please)?[.!]*$", re.I),
     "lead with the number or the answer, skip the explanation"),
    (re.compile(r"^(?:more detail(?:s)?|explain more|go deeper|that(?:'s| was) too short)(?: please)?[.!]*$", re.I),
     "give a bit more detail and reasoning"),
]


def match(text: str) -> tuple[str, dict[str, Any]] | None:
    """('set_preference', {...}) for preference statements and quick feedback."""
    t = text.strip().rstrip(".!")
    m = PREF_RE.match(t)
    if m:
        pref = (m.group("pref") or m.group("pref2") or m.group("pref3")).strip(" ,.")
        if re.search(r"\b(?:is|are|=)\s+\$?\d|\b(?:rate|price|phone|email|address|balance)\b", pref, re.I):
            return None  # "from now on Jay's rate is 60" — a fact to save, not how to answer → the AI
        return "set_preference", {"preference": pref[:1].lower() + pref[1:]}
    for rx, pref in FEEDBACK:
        if rx.match(text.strip()):
            return "set_preference", {"preference": pref, "feedback": True}
    return None


def save_preference(preference: str, feedback: bool = False, db: Database | None = None) -> dict[str, Any]:
    from .memory import Memory

    db = db or get_db()
    m = Memory(db).add(f"{settings.owner_name}'s preference: {preference}", "preference", 5, source="manual")
    if feedback:
        from .selfreview import log_correction

        log_correction("style", "", preference, db)
    return {"saved": True, "id": m["id"], "preference": preference, "duplicate": m.get("duplicate", False)}


# ------------------------------------------------------------------ favorites
def favorites(db: Database | None = None) -> dict[str, list[str]]:
    """Clients and tickers you deal with most (recomputed once a day)."""
    db = db or get_db()
    today = local_now().date().isoformat()
    raw = db.get_kv("favorites")
    if raw:
        fav = json.loads(raw)
        if fav.get("day") == today:
            return fav
    since = (local_now() - timedelta(days=120)).isoformat()
    clients = [r["artist_name"] or r["name"] for r in db.all(
        "SELECT c.name, c.artist_name, COUNT(s.id) AS n FROM clients c JOIN studio_sessions s ON s.client_id = c.id "
        "WHERE s.starts_at >= ? GROUP BY c.id ORDER BY n DESC, MAX(s.starts_at) DESC LIMIT 6", (since,))]
    real_names = [r["name"] for r in db.all(
        "SELECT c.name FROM clients c JOIN studio_sessions s ON s.client_id = c.id WHERE s.starts_at >= ? "
        "AND c.artist_name IS NOT NULL AND c.artist_name != c.name GROUP BY c.id ORDER BY COUNT(s.id) DESC LIMIT 4",
        (since,))]
    tickers: dict[str, int] = {}
    for r in db.all("SELECT symbol FROM watchlist"):
        tickers[r["symbol"]] = tickers.get(r["symbol"], 0) + 3
    for r in db.all("SELECT symbol, COUNT(*) AS n FROM trades WHERE opened_at >= ? GROUP BY symbol", (since,)):
        tickers[r["symbol"]] = tickers.get(r["symbol"], 0) + r["n"]
    top = [s.replace("=F", "").replace("-USD", "") for s, _ in sorted(tickers.items(), key=lambda kv: -kv[1])[:8]]
    fav = {"day": today, "clients": clients, "names": real_names, "tickers": top}
    db.set_kv("favorites", json.dumps(fav))
    return fav


def prompt_line(db: Database | None = None) -> str:
    fav = favorites(db)
    parts = []
    if fav["clients"]:
        parts.append("clients " + ", ".join(fav["clients"]))
    if fav["tickers"]:
        parts.append("tickers " + ", ".join(fav["tickers"]))
    return f"\nWho and what {settings.owner_name} deals with most: " + "; ".join(parts) + ".\n" if parts else ""


def whisper_prompt(db: Database | None = None) -> str:
    """A natural sentence with your clients' names and tickers, so Whisper spells them right."""
    try:
        fav = favorites(db)
    except Exception:  # noqa: BLE001
        return "Hey AMO."
    names = (fav["clients"] + fav["names"])[:6]
    s = "Hey AMO"
    if names:
        s += ", book " + (", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0])
    if fav["tickers"]:
        s += ". How do " + ", ".join(fav["tickers"][:5]) + " look"
    return s + "?"
