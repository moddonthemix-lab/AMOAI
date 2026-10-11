"""Warm-up: have everything loaded before the first question.

At server start (and whenever Ollama has dropped the model, e.g. after it restarted) AMO:
- loads the chat model and has it read AMO's system prompt once, so Ollama caches it — the first
  real question then only needs to read your new words (≈1 s to the first word instead of 5–30 s)
- loads the embedding model (memory search)
- loads Piper and renders the "Got it" / "Let me think" phrases (voice/acks.py)
- loads Whisper for devices that send speech to the brain (if voice support is installed)
"""

from __future__ import annotations

import logging
import threading
import time

import httpx

from .config import settings

log = logging.getLogger(__name__)
_lock = threading.Lock()
_last_check = 0.0


def loaded_models() -> list[str] | None:
    """Models Ollama has in memory right now (None if Ollama isn't reachable)."""
    try:
        r = httpx.get(f"{settings.ollama_url.rstrip('/')}/api/ps", timeout=5)
        r.raise_for_status()
        return [m.get("name") or m.get("model") for m in r.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return None


def _same(a: str, b: str) -> bool:
    norm = (lambda m: m if ":" in m else m + ":latest")
    return norm(a) == norm(b)


def warm_llm(channel: str = "voice") -> float | None:
    """Load the chat model and cache AMO's system prompt. Returns seconds taken (None on failure)."""
    from . import tools
    from .agent import Agent
    from .llm import get_llm

    if not _lock.acquire(blocking=False):
        return None  # already warming
    try:
        start = time.monotonic()
        agent = Agent()
        convo = [{"role": "system", "content": agent.build_system_prompt("", channel)},
                 {"role": "user", "content": agent.turn_note("") + "hi"}]
        get_llm().chat(convo, tools=tools.schemas(tools.route("")), options={"num_predict": 1})
        took = time.monotonic() - start
        log.info("chat model warm (%.1fs)", took)
        return took
    except Exception as e:  # noqa: BLE001 — Ollama not up yet, model missing…
        log.info("warm-up skipped: %s", e)
        return None
    finally:
        _lock.release()


def warm_embeddings() -> None:
    try:
        from .llm import get_llm

        get_llm().embed("warm up")
    except Exception:  # noqa: BLE001
        pass


def warm_whisper() -> None:
    try:
        import numpy as np

        from .voice.stt import transcribe_array

        transcribe_array(np.zeros(8000, dtype="float32"))
    except Exception:  # noqa: BLE001 — voice support not installed on this machine
        pass


def warm_all(voice: bool = True) -> None:
    """Everything, in the background order that matters most: the brain first."""
    warm_llm()
    warm_embeddings()
    if voice:
        try:
            from .voice import acks

            acks.prewarm()
        except Exception:  # noqa: BLE001
            pass
        warm_whisper()


def keep_warm(every: float = 300) -> bool:
    """Scheduler job: if Ollama dropped the chat model (restart, keep_alive ran out), load it again.
    Returns True if it re-warmed."""
    global _last_check
    if time.monotonic() - _last_check < every:
        return False
    _last_check = time.monotonic()
    loaded = loaded_models()
    if loaded is None or any(_same(m, settings.chat_model) for m in loaded):
        return False
    threading.Thread(target=warm_llm, daemon=True).start()
    return True
