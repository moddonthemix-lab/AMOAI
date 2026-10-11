"""Quick notes: ideas you want to keep, searchable later by meaning.

    "Note: beat idea, 140 BPM, dark piano"          "Take a note — call the venue about the 20th"
    "What notes do I have about beats?"             "Read my last note"

Different from memories: memories are facts about you that AMO uses on its own; notes are yours,
kept word for word. They're on the dashboard (Notes & Memory tab) too.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .db import Database, get_db, now_iso
from .recall import VectorIndex

NOTES_INDEX = VectorIndex("notes")

NOTE_RE = re.compile(
    r"^(?:(?:hey )?amo[ ,]*)?(?:can you |please )?(?:take a note|make a note|note to self|add a note|new note|note|"
    r"jot (?:this |that |it )?down|write (?:this |that |it )?down|save (?:this |an )?idea)"
    r"(?: that| of this| of that)?\s*[:,—–-]?\s+(?P<text>.{3,})$", re.I)
LAST_RE = re.compile(r"^(?:(?:hey )?amo[ ,]*)?(?:read|what(?:'s| was| is)|show)(?: me)? (?:my )?(?:last|latest|most recent) note[?.!]*$", re.I)
FIND_RE = re.compile(
    r"^(?:(?:hey )?amo[ ,]*)?(?:what|which|any|show|list|read|find)(?: me)?(?: are| do)?(?: i have)? (?:all )?(?:my |the )?notes?"
    r"(?: do i have)?(?: (?:about|on|for|with|mentioning) (?P<q>.+?))?[?.!]*$", re.I)

_TAGS = {
    "music": r"\b(beat|song|hook|verse|lyric|bpm|melody|sample|track|bars?|chorus|piano|808)s?\b",
    "studio": r"\b(studio|session|client|mix|master|engineer)\b",
    "trading": r"\b(trade|stock|setup|ticker|strat|long|short|calls?|puts?)\b",
    "cravvr": r"\bcravvr\b",
    "reselling": r"\b(resell|flip|ebay|stockx|sneaker|inventory)\b",
    "idea": r"\bidea\b",
    "todo": r"\b(call|email|text|buy|pick up|book|send|remember to)\b",
}


def tags_for(text: str) -> str:
    tags = [t.lstrip("#").lower() for t in re.findall(r"#\w+", text)]
    tags += [name for name, rx in _TAGS.items() if re.search(rx, text, re.I)]
    return ", ".join(dict.fromkeys(tags))


def match(text: str) -> tuple[str, dict[str, Any]] | None:
    t = text.strip()
    if LAST_RE.match(t):
        return "find_notes", {"query": "", "last": True}
    m = FIND_RE.match(t)
    if m:
        return "find_notes", {"query": (m.group("q") or "").strip()}
    m = NOTE_RE.match(t)
    if m:
        body = m.group("text").strip()
        return "add_note", {"text": body[:1].upper() + body[1:]}
    return None


def add(text: str, db: Database | None = None) -> dict[str, Any]:
    from .llm import get_llm

    db = db or get_db()
    text = text.strip()
    if not text:
        raise ValueError("what should the note say?")
    vec = get_llm().embed(text)
    nid = db.insert("notes", {"text": text, "tags": tags_for(text), "embedding": json.dumps(vec) if vec else None,
                              "created_at": now_iso()})
    return {"id": nid, "text": text, "tags": tags_for(text)}


def find(query: str = "", last: bool = False, limit: int = 5, db: Database | None = None) -> dict[str, Any]:
    from .llm import get_llm

    db = db or get_db()
    if last or not query:
        rows = db.all("SELECT id, text, tags, created_at FROM notes ORDER BY id DESC LIMIT ?", (1 if last else limit,))
    else:
        scores: dict[int, float] = {}
        q = get_llm().embed(query)
        if q:
            for nid, sim in NOTES_INDEX.search(q, db, k=limit * 2, min_score=0.45):
                scores[nid] = sim
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
        stems = {w.rstrip("s") for w in words}
        for r in db.all("SELECT id, text, tags FROM notes ORDER BY id DESC LIMIT 500"):
            hay = (r["text"] + " " + r["tags"]).lower()
            hit = sum(s in hay for s in stems) / max(1, len(stems))
            if hit:
                scores[r["id"]] = scores.get(r["id"], 0) + hit
        ids = [i for i, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:limit]]
        rows = [db.one("SELECT id, text, tags, created_at FROM notes WHERE id = ?", (i,)) for i in ids]
        rows = [r for r in rows if r]
    if not rows:
        text = f"No notes about {query}." if query else "You don't have any notes yet."
    elif last:
        text = f"Your last note ({_when(rows[0]['created_at'])}): {rows[0]['text']}"
    else:
        head = f"{len(rows)} note{'s' if len(rows) != 1 else ''}" + (f" about {query}" if query else "")
        text = f"{head}: " + " · ".join(f"{r['text']} ({_when(r['created_at'])})" for r in rows)
    return {"notes": rows, "text": text}


def _when(iso: str) -> str:
    from datetime import datetime

    from .db import local_now

    d = datetime.fromisoformat(iso)
    days = (local_now().date() - d.date()).days
    return "today" if days == 0 else "yesterday" if days == 1 else f"{d:%b} {d.day}"


def index_missing(db: Database | None = None, batch: int = 40) -> int:
    """Embed notes added from the dashboard (scheduler, when idle)."""
    from .llm import get_llm

    db = db or get_db()
    done = 0
    for r in db.all("SELECT id, text FROM notes WHERE embedding IS NULL LIMIT ?", (batch,)):
        vec = get_llm().embed(r["text"])
        if not vec:
            break
        db.execute("UPDATE notes SET embedding = ? WHERE id = ?", (json.dumps(vec), r["id"]))
        done += 1
    return done

