"""How AMO gets more useful every week.

1. Fact extraction (after conversations): a small local model reads new,
   unprocessed messages and pulls out durable facts — people, prices,
   preferences, rules, plans — and stores them as memories.
2. Weekly reflection: summarizes the week's numbers and conversations into
   patterns and suggestions; the latest reflection is injected into every chat,
   and its key insights are saved as memories.
3. Memory hygiene: memories never recalled and low-importance decay out.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta
from typing import Any

from .config import settings
from .db import Database, days_ago, get_db, local_now, now_iso
from .finance import Finance
from .goals import Goals
from .llm import get_llm
from .memory import Memory
from .reselling import Reselling
from .trading import TradingJournal

log = logging.getLogger(__name__)
_learn_lock = threading.Lock()

EXTRACT_PROMPT = """You maintain the long-term memory of a personal assistant for {owner}.
Read the conversation excerpt and extract NEW durable facts worth remembering for months:
people (clients, family, partners) and details about them, prices/rates, business plans,
preferences, routines, trading rules, goals, decisions.

Do NOT extract: one-off requests, small talk, things the assistant said, data already
recorded through tools (individual bookings, trades, sales), or anything uncertain.

Already known (don't repeat):
{known}

Return JSON: {{"facts": [{{"content": "standalone sentence about {owner}", "category": "general|preference|person|studio|cravvr|reselling|trading|goal|health", "importance": 1-5}}]}}
Return {{"facts": []}} if nothing qualifies.

Conversation:
{convo}"""

REFLECT_PROMPT = """You are {assistant}, {owner}'s personal AI. Write your weekly reflection.

This week's numbers:
{numbers}

What {owner} talked to you about this week:
{convo}

Write:
1. "summary": 4-8 tight bullet points — what happened, what's working, what's slipping,
   patterns (e.g. trading losses when rules are broken, clients going quiet, stale inventory),
   and the 3 most valuable things to focus on next week.
2. "insights": 0-5 durable lessons worth remembering long-term (standalone sentences).

Return JSON: {{"summary": "- ...\\n- ...", "insights": ["...", "..."]}}"""


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("model did not return JSON")
    return json.loads(text[start : end + 1])


def learn_from_conversations(db: Database | None = None, batch: int = 30) -> list[dict[str, Any]]:
    """Extract facts from unprocessed messages. Returns the memories added."""
    db = db or get_db()
    if not _learn_lock.acquire(blocking=False):
        return []  # another learner is already running
    try:
        rows = db.all(
            "SELECT id, role, content FROM conversations WHERE learned = 0 ORDER BY id LIMIT ?",
            (batch,),
        )
        if not any(r["role"] == "user" for r in rows):
            return []
        mem = Memory(db)
        convo = "\n".join(f"{r['role'].upper()}: {r['content'][:1500]}" for r in rows)
        known = "\n".join(f"- {m['content']}" for m in mem.list(limit=60)) or "- (nothing)"
        msg = get_llm().chat(
            [{"role": "user", "content": EXTRACT_PROMPT.format(
                owner=settings.owner_name, known=known, convo=convo)}],
            model=settings.fast_model,
            fmt="json",
            options={"temperature": 0},
        )
        facts = _parse_json(msg.get("content", "")).get("facts", [])
        added = []
        for f in facts:
            if isinstance(f, dict) and f.get("content"):
                m = mem.add(f["content"], f.get("category", "general"),
                            int(f.get("importance", 3) or 3), source="chat")
                if not m.get("duplicate"):
                    added.append(m)
        ids = [r["id"] for r in rows]
        db.execute(f"UPDATE conversations SET learned = 1 WHERE id IN ({','.join('?' * len(ids))})",
                   tuple(ids))
        if added:
            log.info("learned %d new facts", len(added))
        return added
    finally:
        _learn_lock.release()


def weekly_numbers(db: Database) -> dict[str, Any]:
    since = days_ago(7)
    return {
        "revenue_7d": Finance(db).revenue("week"),
        "studio_sessions_7d": db.scalar(
            "SELECT COUNT(*) FROM studio_sessions WHERE status = 'completed' AND starts_at >= ?", (since,)),
        "no_shows_7d": db.scalar(
            "SELECT COUNT(*) FROM studio_sessions WHERE status = 'no_show' AND starts_at >= ?", (since,)),
        "trading_7d": TradingJournal(db).stats(since),
        "reselling_7d": Reselling(db).summary(since),
        "stale_inventory": len(Reselling(db).stale(30)),
        "goals": [{"title": g["title"], "streak": g["streak"], "progress": g["progress"]}
                  for g in Goals(db).active()],
    }


def weekly_reflection(db: Database | None = None) -> dict[str, Any]:
    db = db or get_db()
    end = local_now()
    start = end - timedelta(days=7)
    rows = db.all(
        "SELECT role, content FROM conversations WHERE role = 'user' AND created_at >= ? ORDER BY id",
        (start.isoformat(),),
    )
    convo = "\n".join(f"- {r['content'][:300]}" for r in rows[-80:]) or "- (no conversations)"
    numbers = json.dumps(weekly_numbers(db), default=str, indent=1)
    msg = get_llm().chat(
        [{"role": "user", "content": REFLECT_PROMPT.format(
            assistant=settings.assistant_name, owner=settings.owner_name, numbers=numbers, convo=convo)}],
        fmt="json",
        options={"temperature": 0.3},
    )
    data = _parse_json(msg.get("content", ""))
    summary = data.get("summary", "").strip()
    if isinstance(summary, list):
        summary = "\n".join(f"- {s}" for s in summary)
    rid = db.insert("reflections", {"period_start": start.isoformat(), "period_end": end.isoformat(),
                                    "summary": summary, "created_at": now_iso()})
    mem = Memory(db)
    for insight in data.get("insights", [])[:5]:
        if isinstance(insight, str) and insight.strip():
            mem.add(insight, "general", 3, source="reflection")
    decayed = decay_memories(db)
    return {"id": rid, "summary": summary, "insights": data.get("insights", []), "decayed": decayed}


def decay_memories(db: Database | None = None, days: int = 120) -> int:
    """Archive low-importance, auto-learned memories that have never been used."""
    db = db or get_db()
    return db.execute(
        "UPDATE memories SET archived = 1 WHERE archived = 0 AND importance <= 2 AND source != 'manual' "
        "AND use_count = 0 AND created_at < ?",
        (days_ago(days),),
    )
