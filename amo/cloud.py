"""Cloud AI through the Family Hub's shared brain (amo/family/family_brain.py).

What goes to the cloud: only answers that don't need AMO's tools —
    chat     conversation, advice, explanations, writing   → Claude Haiku   (job="chat")
    code     coding questions and pasted errors            → Claude Sonnet  (job="code")
    big      "big job: …" when AMO_CLOUD_BIG is on          → Claude Opus    (job="big")
Saves, lookups, charts, timers, the printer, the web, anything with your data stays on the local
model with its tools, exactly as before.

Cloud is only tried when all of these are set (in AMO's .env):
    CLOUD_FOR_PRIVATE_CHATS=true    your chats with AMO may go to the cloud (default false)
    ANTHROPIC_API_KEY=…             the family's key — never in code, chats or the hub
    HUB_TOKEN=… / HUB_URL=…         the hub checks the monthly budget and records every cent
Otherwise nothing changes. When the hub says no (budget used up, no internet, hub asleep), AMO answers
on its own local model, streaming, with its tools, as it always did.

Rules from the hub (docs/ADDING_CLOUD_AI.md): Claude is only ever called through family_brain, so every
cent is checked and recorded; a refused request is never retried on the local model; the API key is
never logged or shown.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

# "big job: plan my whole album rollout", "think hard about …" — Opus, only when AMO_CLOUD_BIG is on.
BIG_RE = re.compile(r"^(?:(?:hey )?amo[ ,]*)?(?:(?:this is a |here'?s a |new )?big job|think (?:really )?hard"
                    r"(?: about)?|deep think)\b[\s:,.-]*", re.I)
# Needs something live from the internet → the local model's web_search (Haiku has no web here).
LIVE_RE = re.compile(r"\b(?:news|latest|current(?:ly)?|right now|today|tonight|tomorrow|yesterday|this (?:week|weekend|"
                     r"month|year)|weather|forecast|score|scores|who won|price|prices|cost of|stock|search|look (?:it )?up|"
                     r"google|online|internet|website|http|www\.|\.com|open now|near me|release date)\b", re.I)

CLOUD_NOTE = ("Right now you're answering with the family's cloud brain (Claude), so your tools aren't available "
              "in this reply.")
NO_TOOLS = ("\n- In this reply you can't use tools: never say you saved, booked, logged, looked up or changed "
            "anything. If {owner} wants that done, say so in one line and ask them to tell you again directly "
            "(e.g. \"book Ty Friday at 2\").\n")


def possible() -> bool:
    """Is cloud allowed to be tried at all? (The hub still decides each time, by budget.)"""
    on = os.environ.get("CLOUD_FOR_PRIVATE_CHATS", "").strip().lower() in ("1", "true", "yes", "on")
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    return bool(on and key and os.environ.get("HUB_TOKEN"))


def big_enabled() -> bool:
    return os.environ.get("AMO_CLOUD_BIG", "").strip().lower() in ("1", "true", "yes", "on")


def job_for(text: str, kind: str | None) -> tuple[str, str] | None:
    """(job, prompt) when this message can be answered without tools, else None."""
    t = (text or "").strip()
    if not t:
        return None
    m = BIG_RE.match(t)
    if m and big_enabled() and t[m.end():].strip():
        return "big", t[m.end():].strip()
    if kind != "chat" or LIVE_RE.search(t):
        return None
    from . import tools

    needs = set(tools.route(t)) - set(tools.CORE) - {"web_search", "read_webpage"}
    return None if needs else ("chat", t)


def history_for(history: list[dict[str, Any]], limit: int = 12) -> list[dict[str, str]]:
    """Earlier turns as plain user/assistant text, alternating and starting with you (what Claude expects)."""
    out: list[dict[str, str]] = []
    for m in history:
        content = m.get("content")
        if m.get("role") not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + content}
        else:
            out.append({"role": m["role"], "content": content})
    out = out[-limit:]
    while out and out[0]["role"] != "user":
        out.pop(0)
    while out and out[-1]["role"] != "assistant":  # the new message is added as the next user turn
        out.pop()
    return out


def system_for(system: str) -> str:
    """AMO's own system prompt, minus the parts that aren't true in the cloud."""
    system = system.replace("You run 100% locally on their own hardware.", CLOUD_NOTE)
    marker = "\nWhat you know about"
    note = NO_TOOLS.format(owner=settings.owner_name)
    return system.replace(marker, note + marker, 1) if marker in system else system + note


_brain = None


def _make_brain(hub: Any = None, **fakes: Any) -> Any:
    """fakes: cloud_client / internet_check stand-ins (tests)."""
    from .family.family_brain import FamilyBrain, Thought
    from .family.hub_client import HubClient

    class AmoBrain(FamilyBrain):
        """The shared brain, except that "answer locally" hands the turn back to AMO's own local path
        (its chosen model, tools and streaming) instead of a plain non-streaming Ollama call."""

        def _local(self, prompt, system, history, reason, schema=None):  # noqa: ANN001
            return Thought(brain="local", reason=reason)

    # A short timeout: if the hub's computer is asleep, answer locally in seconds, not half a minute.
    return AmoBrain(hub or HubClient(timeout=5), **fakes)


def brain() -> Any:
    global _brain
    if _brain is None:
        _brain = _make_brain()
    return _brain


def think(prompt: str, job: str, system: str, history: list[dict[str, Any]]) -> Any:
    """A Thought from the cloud (answered or refused), or None: answer locally as usual."""
    from .family.family_brain import BrainUnavailable

    try:
        t = brain().think(prompt, job=job, system=system, history=history_for(history))
    except BrainUnavailable as e:
        log.warning("shared brain unavailable: %s", e)
        return None
    except Exception as e:  # noqa: BLE001 — never lose the reply over the cloud; the type only, never details
        log.warning("cloud answer failed (%s); answering locally", type(e).__name__)
        return None
    if t.brain != "cloud":
        if t.reason:
            log.info("answering locally: %s", t.reason)
        return None
    return t


def refusal_text(t: Any) -> str:
    """A refused request: say so plainly. It is not retried on the local model."""
    why = (t.reason or "").strip().rstrip(".")
    return ("I can't help with that one" + (f" — {why[0].lower() + why[1:]}" if why else "") + ".")
