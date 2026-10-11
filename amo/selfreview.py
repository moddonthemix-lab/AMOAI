"""AMO's weekly self-review: what you had to correct, what it learned from that, and facts that may
be out of date.

    "This week you corrected me 3 times — twice on times: I'll assume PM for 1 to 6 o'clock.
     I learned to say Karim right. Might be outdated: 'Jay's rate is $50' (from March) — still true?"

Every correction is logged (cutting AMO off with "no, I said…", "undo that", pronunciation fixes,
"too long"), so the patterns are real, not guessed by the model.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from .config import settings
from .db import Database, get_db, local_now, now_iso

_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", re.I)
_NAME = re.compile(r"\b[A-Z][a-z]+\b")


WRONG_RE = re.compile(r"^(?:(?:hey )?amo[ ,]*)?(?:no[ ,]+)?(?:that'?s|that is|that was|you'?re|you are|you got (?:it|that))"
                      r" (?:wrong|not right|incorrect|not what i (?:asked|wanted|meant))|^(?:wrong|nope,? wrong)[.!]*$", re.I)


def log_correction(kind: str, said: str, meant: str, db: Database | None = None, reply: str = "") -> None:
    """kind: misheard | undo | pronunciation | style | wrong (you said it was wrong) | failed (AMO couldn't)"""
    try:
        (db or get_db()).insert("corrections", {"kind": kind, "said": (said or "")[:300],
                                                "meant": (meant or "")[:300], "reply": (reply or "")[:500],
                                                "created_at": now_iso()})
    except Exception:  # noqa: BLE001 — logging must never break a reply
        pass


def _times(text: str) -> list[tuple[int, str]]:
    return [(int(h), (ap or "").lower().replace(".", "")) for h, _, ap in _TIME.findall(text or "")]


def classify(said: str, meant: str) -> str:
    """What kind of mistake was it? times | names | amounts | request"""
    ts, tm = _times(said), _times(meant)
    if tm and (ts != tm or any(ap for _, ap in tm)):
        if any(ap for _, ap in tm) or ts:
            return "times"
    ns, nm = set(_NAME.findall(said or "")), set(_NAME.findall(meant or ""))
    if nm - ns:
        return "names"
    if re.search(r"\$?\d", meant or "") and re.findall(r"\d+", meant or "") != re.findall(r"\d+", said or ""):
        return "amounts"
    return "request"


def mistakes(db: Database | None = None, limit: int = 20) -> list[dict[str, Any]]:
    return (db or get_db()).all("SELECT * FROM corrections WHERE kind NOT IN ('style', 'pronunciation') "
                                "ORDER BY id DESC LIMIT ?", (limit,))


def mistakes_text(db: Database | None = None, limit: int = 20) -> str:
    rows = mistakes(db, limit)
    if not rows:
        return "No mistakes logged. (Corrections, \"that's wrong\", undo and failed requests show up here.)"
    label = {"misheard": "misheard", "undo": "undone", "wrong": "you said wrong", "failed": "couldn't do it"}
    out = []
    for r in rows:
        line = f"{r['created_at'][:16].replace('T', ' ')}  [{label.get(r['kind'], r['kind'])}]  “{r['said']}”"
        if r["meant"]:
            line += f" → meant “{r['meant']}”"
        if r.get("reply"):
            line += f"\n      AMO: {r['reply'][:140]}"
        out.append(line)
    return "\n".join(out)


def review(db: Database | None = None, now: datetime | None = None, days: int = 7) -> dict[str, Any]:
    """The weekly self-review: counts, learned rules (saved as preferences), stale facts."""
    from .memory import Memory

    db = db or get_db()
    now = now or local_now()
    since = (now - timedelta(days=days)).isoformat()
    rows = db.all("SELECT * FROM corrections WHERE created_at >= ? ORDER BY id", (since,))
    owner = settings.owner_name
    kinds: dict[str, int] = {}
    learned: list[str] = []
    for r in rows:
        k = classify(r["said"], r["meant"]) if r["kind"] in ("misheard", "undo") else r["kind"]
        kinds[k] = kinds.get(k, 0) + 1

    # Learn from repeated time mix-ups: "at 5" kept meaning 5 PM → assume PM for 1–6.
    pm_fixes = sum(1 for r in rows if r["kind"] in ("misheard", "undo")
                   for h, ap in _times(r["meant"]) if ap == "pm" and 1 <= h <= 6)
    if pm_fixes >= 2:
        m = Memory(db).add(f"{owner}'s preference: a time from 1 to 6 without AM/PM means PM", "preference", 5)
        if not m.get("duplicate"):
            learned.append("I'll assume PM for 1 to 6 o'clock unless you say otherwise")
    # Names Whisper keeps getting wrong → remember the right spelling (also helps the hint).
    for r in rows:
        if r["kind"] in ("misheard", "undo") and classify(r["said"], r["meant"]) == "names":
            for name in set(_NAME.findall(r["meant"])) - set(_NAME.findall(r["said"])) - {"I", "AMO"}:
                m = Memory(db).add(f"{name} is a name {owner} uses (spell it {name})", "person", 4)
                if not m.get("duplicate"):
                    learned.append(f"I'll listen for “{name}”")
    said_right = [r["meant"] for r in rows if r["kind"] == "pronunciation"]

    total = sum(v for k, v in kinds.items() if k not in ("pronunciation",))
    parts = []
    if total:
        detail = ", ".join(f"{n} on {label}" for label, n in sorted(kinds.items(), key=lambda kv: -kv[1])
                           if label not in ("pronunciation",) and n)
        parts.append(f"This week you corrected me {total} time{'s' if total != 1 else ''} ({detail}).")
    else:
        parts.append("No corrections this week.")
    if learned:
        parts.append("What I learned: " + "; ".join(dict.fromkeys(learned)) + ".")
    if said_right:
        parts.append("I learned to say " + ", ".join(dict.fromkeys(said_right)) + " right.")
    stale = Memory(db).stale()
    if stale:
        items = "; ".join(f"“{m['content']}” (from {m['created_at'][:7]})" for m in stale[:3])
        parts.append(f"These might be out of date: {items}. Tell me if anything changed.")
    return {"text": " ".join(parts), "corrections": total, "by_kind": kinds, "learned": learned,
            "stale": [m["content"] for m in stale]}
