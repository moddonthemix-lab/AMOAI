"""Spoken acknowledgements ("Got it", "Okay, let me think") played the moment you finish
talking, while the AI model works on the real answer in parallel.

Phrases are pre-rendered in AMO's voice and cached, so they play instantly.
Customise in .env (separate phrases with |):
  AMO_ACK_THINK=Okay, let me think.|Let me think.
  AMO_ACK_ACTION=Got it.|I'll work on that now.
  AMO_ACKS=0   to turn them off
"""

from __future__ import annotations

import random
import re
import threading

from ..config import settings

_QUESTION = re.compile(
    r"^(what|what's|whats|how|how's|who|who's|when|where|why|which|do|does|did|is|are|am|was|"
    r"were|can|could|should|would|will|tell me|show me|give me|list|any|have i|has)\b"
)
_ACTION = re.compile(
    r"\b(book|add|log|record|mark|sold|bought|buy|sell|list|cancel|resched\w*|move|change|update|"
    r"set|create|remind|remember|save|close|paid|pay|check in|done with|finished|delete|forget)\b"
)

_last: dict[str, str] = {}
_cache: dict[str, bytes] = {}
_lock = threading.Lock()


def phrases(kind: str) -> list[str]:
    raw = {"think": settings.ack_think, "action": settings.ack_action, "still": settings.ack_still}[kind]
    return [p.strip() for p in raw.split("|") if p.strip()]


def kind_of(request: str) -> str:
    t = request.lower().strip()
    if _QUESTION.match(t) or t.endswith("?"):
        return "think"
    return "action" if _ACTION.search(t) else "think"


def pick(request: str) -> str | None:
    """The phrase to say for this request (never the same one twice in a row)."""
    if not settings.acks:
        return None
    kind = kind_of(request)
    options = phrases(kind) or phrases("think")
    if not options:
        return None
    if len(options) > 1 and _last.get(kind) in options:
        options = [p for p in options if p != _last[kind]]
    choice = random.choice(options)
    _last[kind] = choice
    return choice


def still(n: int) -> str | None:
    """The n-th "still working" line (0 = first) for a long wait, or None when there are no more."""
    if not settings.acks:
        return None
    options = phrases("still")
    return options[n] if n < len(options) else None


def wait_with_updates(worker, speak, first_after: float | None = None) -> None:
    """Wait for `worker` (a started thread); say "Still working on it." if it takes a while,
    then "Almost there." after a longer wait."""
    delay = settings.ack_still_after if first_after is None else first_after
    n = 0
    while True:
        worker.join(delay)
        if not worker.is_alive():
            return
        line = still(n)
        if line is None:
            worker.join()
            return
        speak(line)
        n += 1
        delay = delay * 2  # 7s → then ~14s later


def audio(phrase: str) -> bytes:
    """WAV for a phrase in AMO's current voice, cached after the first time."""
    from .tts import synthesize

    key = f"{settings.voice}|{phrase}"
    with _lock:
        if key not in _cache:
            _cache[key] = synthesize(phrase)
        return _cache[key]


def prewarm() -> None:
    """Render every phrase ahead of time (call in a background thread at startup)."""
    for kind in ("think", "action", "still"):
        for p in phrases(kind):
            try:
                audio(p)
            except Exception:  # noqa: BLE001 — voice not available: acks are optional
                return
