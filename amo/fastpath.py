"""Requests common and unambiguous enough to handle without the AI model.

Small local models occasionally *say* they saved something without calling the tool.
For the most common requests AMO just does it directly — instant and reliable.
"""

from __future__ import annotations

import re
from typing import Any

_LEAD = r"^(?:hey\s+amo,?\s*|amo,?\s*)?(?:please\s+|can you\s+|could you\s+|go ahead and\s+|i want to\s+)?"

GOAL_RE = re.compile(
    _LEAD + r"(?:add|create|set|make|start|new)\s+(?:me\s+)?(?:up\s+)?(?:a\s+|an\s+)?(?:new\s+)?"
    r"(?P<cad>daily|weekly|monthly)?\s*goal\s*(?:to\s+|:|-|—|called\s+|of\s+|for\s+|that\s+)?\s*(?P<title>.+?)[.!]*$",
    re.I,
)
REMEMBER_RE = re.compile(
    _LEAD + r"(?:remember|make a note|note down|take a note)\s*(?:that|:)?\s+(?P<fact>.{6,}?)[.!]*$", re.I
)


_KIND_WORDS = (r"(?P<kind>daily goal|weekly goal|monthly goal|goal|client|cravvr task|task|resale item|item|"
               r"trading rule|rule|trade|session|booking|payment|memory)")
DELETE_RE = re.compile(
    _LEAD + r"(?:delete|remove|get rid of|drop|erase|scrap)\s+(?:the\s+|my\s+|that\s+|this\s+)?"
    + _KIND_WORDS + r"\s*(?:called\s+|named\s+|for\s+|:|-|—)?\s*(?P<name>.+?)[.!]*$", re.I)
DELETE_AFTER_RE = re.compile(  # "delete the make one beat goal"
    _LEAD + r"(?:delete|remove|get rid of|drop|erase|scrap)\s+(?:the\s+|my\s+|that\s+)?(?P<name>.+?)\s+"
    + _KIND_WORDS + r"[.!]*$", re.I)


# "delete that trade" refers to the conversation — leave those to the AI, which has the context.
_CONTEXT_WORDS = {"that", "this", "it", "them", "those", "these", "last", "last one", "the last one",
                  "latest", "the latest", "one", "that one", "this one", "previous", "same"}


def _kind(word: str) -> str:
    w = word.lower()
    return "goals" if "goal" in w else {"task": "cravvr", "cravvr task": "cravvr", "item": "resale",
                                        "resale item": "resale", "booking": "sessions"}.get(w, w)


def match(text: str) -> tuple[str, dict[str, Any]] | None:
    t = text.strip()
    for rx in (DELETE_RE, DELETE_AFTER_RE):
        m = rx.match(t)
        name = m.group("name").strip(" :-—\"'“”").lower() if m else ""
        if m and name and name not in _CONTEXT_WORDS and len(name.split()) <= 10:
            return "delete_record", {"kind": _kind(m.group("kind")), "item": m.group("name").strip(" :-—\"'“”")}
    m = GOAL_RE.match(t)
    if m and len(m.group("title").split()) <= 12:
        title = m.group("title").strip(" :-—")
        return "add_goal", {"title": title[:1].upper() + title[1:], "cadence": (m.group("cad") or "daily").lower()}
    m = REMEMBER_RE.match(t)
    if m:
        return "remember", {"content": m.group("fact").strip()}
    return None
