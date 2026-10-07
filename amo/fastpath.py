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


def match(text: str) -> tuple[str, dict[str, Any]] | None:
    t = text.strip()
    m = GOAL_RE.match(t)
    if m and len(m.group("title").split()) <= 12:
        title = m.group("title").strip(" :-—")
        return "add_goal", {"title": title[:1].upper() + title[1:], "cadence": (m.group("cad") or "daily").lower()}
    m = REMEMBER_RE.match(t)
    if m:
        return "remember", {"content": m.group("fact").strip()}
    return None
