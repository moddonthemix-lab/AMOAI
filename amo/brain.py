"""Two-tier brain: each request goes to the model that does that kind of request best.

    chat     small talk, feelings, general questions      ("hey", "I'm tired today")
    lookup   reading your own data                         ("what's my schedule tomorrow", "who owes me")
    action   saving or changing something                  ("book Ty tomorrow at 2", "Jay paid 150")

`amo eval --models a,b` measures every model on each kind and recommends the fastest one that's
about as accurate as the best; `amo brain` shows / sets the split (AMO_MODEL_CHAT / _LOOKUP / _ACTION).
Actions default to the most accurate model — a wrong save costs more than a slow one.
"""

from __future__ import annotations

import re
from typing import Any

from .config import settings

KINDS = ("chat", "lookup", "action")

_ACTION = re.compile(
    r"\b(?:book|add|log|record|mark|sold|bought|buy|sell|listed|cancel|resched\w*|move|change|update|rename|"
    r"set|create|remind|remember|save|close|closed|paid|pay|check(?:ed)? in|done with|finished|delete|remove|"
    r"forget|put|note|track|long|short)\b", re.I)
_QUESTION = re.compile(
    r"^(?:(?:hey )?amo[ ,]*)?(?:what|what's|whats|how|how's|who|who's|whom|when|where|which|do i|did i|have i|"
    r"am i|is there|are there|any|show|list|tell me|give me|read me)\b", re.I)
# Your own data — anything AMO keeps track of — or a person you know ("who's Marcus").
_MINE = re.compile(
    r"\b(?:my|i|me|i'm|i've|owe|owes|schedule|sessions?|clients?|goals?|inventory|trades?|trading|positions?|"
    r"rules?|cravvr|revenue|profit|money|notes?|timers?|reminders?|watchlist|reselling|studio|business)\b", re.I)
_FOLLOW_UP = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|do it|go ahead|please|no|nope|that one|the first one|"
                        r"the second one|both)\b", re.I)


def kind(text: str, previous: str = "") -> str:
    """'chat', 'lookup' or 'action' for a user message (`previous`: the user's message before it)."""
    t = (text or "").strip()
    if _FOLLOW_UP.match(t) and len(t.split()) <= 4 and previous:
        return kind(previous)  # "yes" answers whatever was being done
    if _QUESTION.match(t) or t.endswith("?"):
        return "lookup" if _MINE.search(t) or re.match(r"^(?:who|who's|whos)\s+[A-Z]", t) else "chat"
    if _ACTION.search(t):
        return "action"
    return "chat"


def model_for(k: str) -> str:
    return {"chat": settings.model_chat, "lookup": settings.model_lookup,
            "action": settings.model_action}.get(k) or settings.chat_model


def pick(history: list[dict[str, Any]]) -> tuple[str, str]:
    """(kind, model) for the last user message in `history`."""
    users = [m["content"] for m in history if m.get("role") == "user" and isinstance(m.get("content"), str)]
    if not users:
        return "chat", settings.chat_model
    k = kind(users[-1], users[-2] if len(users) > 1 else "")
    return k, model_for(k)


def models_in_use() -> list[str]:
    return list(dict.fromkeys(model_for(k) for k in KINDS))


def two_tier() -> bool:
    return len(models_in_use()) > 1


# ------------------------------------------------------------------ choosing the split from evals
def recommend(summaries: list[dict[str, Any]], margin: float = 5.0, min_cases: int = 5) -> dict[str, Any]:
    """Per kind: the fastest model whose accuracy is within `margin` points of the best on that kind.
    Models that crashed on most AI cases (not downloaded, can't use tools) are left out."""
    usable = [s for s in summaries if not broken(s)]
    out: dict[str, Any] = {"split": {}, "table": {}, "kept": []}
    for k in KINDS:
        rows = []
        for s in usable:
            rs = [r for r in s["results"] if r.get("kind") == k and r["path"] in ("model", "coder")]
            if rs:
                acc = 100 * sum(r["ok"] for r in rs) / len(rs)
                times = sorted(r["first_ms"] for r in rs)
                rows.append({"model": s["model"], "acc": round(acc, 1), "n": len(rs),
                             "ms": times[len(times) // 2]})
        out["table"][k] = rows
        if not rows:
            continue
        if min(r["n"] for r in rows) < min_cases:  # too few cases reached the AI to tell — leave it as it is
            out["split"][k] = model_for(k)
            out["kept"].append(k)
            continue
        best = max(r["acc"] for r in rows)
        # a wrong save costs more than a slow one: actions only take a model that's as good as the best
        room = 0 if k == "action" else margin
        good = [r for r in rows if r["acc"] >= best - room]
        out["split"][k] = min(good, key=lambda r: r["ms"])["model"]
    return out


def broken(summary: dict[str, Any]) -> str | None:
    """The error, if a model crashed on most of the cases the AI answers."""
    crashed = [r for r in summary["results"] if any(w.startswith("crashed") for w in r["why"])]
    ai = [r for r in summary["results"] if r["path"] in ("model", "coder") or "ai" in r.get("tags", [])]
    if ai and len(crashed) >= len(ai) / 2:
        return crashed[0]["why"][0].replace("crashed: ", "")
    return None
