"""Teach AMO how to say names and words — it says them that way everywhere, from then on.

    "No, it's pronounced kah-REEM"         (the word is picked from what AMO just said)
    "Karim is pronounced kah-reem"         "Say Nguyen like win"        "Pronounce Saoirse as SEER-sha"
"""

from __future__ import annotations

import re
import time
from difflib import SequenceMatcher
from typing import Any

from .db import Database, get_db, now_iso

_LEAD = r"^(?:(?:hey )?amo[ ,]*)?(?:no+[ ,.]+|nope[ ,.]+|actually[ ,]+)*"
PATTERNS = [
    re.compile(_LEAD + r"(?:you'?re|you are) (?:saying|pronouncing) (?P<word>.+?) wrong[ ,.;-]*"
                       r"(?:it'?s|say it) (?:pronounced )?(?:like )?(?P<say>.+)$", re.I),
    re.compile(_LEAD + r"(?:it'?s|its|that'?s) (?:pronounced|said) (?:like )?(?P<say>.+)$", re.I),
    re.compile(_LEAD + r"(?P<word>[\w'’ -]{2,40}?) is (?:pronounced|said) (?:like )?(?P<say>.+)$", re.I),
    re.compile(_LEAD + r"(?:say|pronounce) (?P<word>[\w'’ -]{2,40}?) (?:like|as) (?P<say>.+)$", re.I),
]
_cache: dict[str, Any] = {"at": 0.0, "rules": []}


def _letters(s: str) -> str:
    return re.sub(r"[^a-z]", "", s.lower())


def guess_word(say: str, last_reply: str) -> str | None:
    """Which word of AMO's last reply was said wrong? The one that looks most like the pronunciation
    (names first)."""
    words = {w.strip("'’") for w in re.findall(r"[A-Za-z][A-Za-z'’]+", last_reply or "") if len(w) > 2}
    if not words:
        return None
    target = _letters(say)

    def score(w: str) -> float:
        return SequenceMatcher(None, _letters(w), target).ratio() + (0.15 if w[0].isupper() else 0)

    best = max(words, key=score)
    return best if score(best) >= 0.45 else None


def match(text: str, last_reply: str = "") -> tuple[str, dict[str, Any]] | None:
    t = text.strip().rstrip(".!")
    for rx in PATTERNS:
        m = rx.match(t)
        if not m:
            continue
        say = m.group("say").strip(" \"'“”")
        word = (m.groupdict().get("word") or "").strip(" \"'“”") or guess_word(say, last_reply)
        if not word:
            return "set_pronunciation", {"word": "", "say": say}
        return "set_pronunciation", {"word": word, "say": say}
    return None


def save(word: str, say: str, db: Database | None = None) -> dict[str, Any]:
    if not word:
        raise ValueError("which word should I say like that?")
    db = db or get_db()
    db.execute("INSERT INTO pronunciations (word, say, created_at) VALUES (?, ?, ?) "
               "ON CONFLICT(word) DO UPDATE SET say = excluded.say", (word.strip(), say.strip(), now_iso()))
    _cache["at"] = 0.0
    from .selfreview import log_correction

    log_correction("pronunciation", say, word, db)
    return {"saved": True, "word": word, "say": say}


def rules(db: Database | None = None) -> list[tuple[re.Pattern, str]]:
    if time.monotonic() - _cache["at"] > 30:
        try:
            rows = (db or get_db()).all("SELECT word, say FROM pronunciations ORDER BY length(word) DESC")
            _cache["rules"] = [(re.compile(rf"\b{re.escape(r['word'])}\b", re.I), r["say"]) for r in rows]
        except Exception:  # noqa: BLE001 — no database (e.g. a body device): no custom words
            _cache["rules"] = []
        _cache["at"] = time.monotonic()
    return _cache["rules"]


def apply(text: str, db: Database | None = None) -> str:
    """Swap in the taught pronunciations (only for speech — the screen still shows the real spelling)."""
    for rx, say in rules(db):
        text = rx.sub(say, text)
    return text
