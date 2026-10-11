"""Speak a reply as it's being written: yields ("ack" | "sentence", text, wav) as soon as each piece is
ready, then ("done", full_reply, b""). Shared by every voice device (Pi body, ESP32 pet)."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterator
from typing import Any

from ..config import settings


def spoken_reply(history: list[dict[str, Any]], text: str, channel: str = "voice",
                 agent_chat=None, turn: Any = None) -> Iterator[tuple[str, str, bytes]]:
    """turn: a perf.Turn to time (speech-to-text added by the caller); saved when the reply ends."""
    from ..agent import Agent
    from ..llm import LLMError
    from . import acks
    from .speaker import split_ready
    from .tts import synthesize

    from ..perf import Turn

    pieces: queue.Queue = queue.Queue()
    chat = agent_chat or Agent().chat
    turn = turn or Turn(channel, text)

    def speak(sentence: str) -> bytes:
        if "first_audio" in turn.marks:
            return synthesize(sentence)
        with turn.timer("tts"):
            wav = synthesize(sentence)
        turn.mark("first_audio")
        return wav

    def think() -> None:
        try:
            reply = chat(history, channel=channel, on_text=pieces.put, turn=turn)["content"]
            pieces.put(("done", reply))
        except LLMError as e:
            pieces.put(f"Sorry, I couldn't do that: {e}")
            pieces.put(("done", ""))
        except Exception:  # noqa: BLE001 — a bug must never leave the speaker waiting forever
            import logging

            logging.getLogger(__name__).exception("voice reply failed")
            turn.info["ok"] = False
            pieces.put("Sorry, something went wrong on my end.")
            pieces.put(("done", ""))

    threading.Thread(target=think, daemon=True).start()
    phrase = acks.pick(text)
    if phrase:
        try:
            yield "ack", phrase, acks.audio(phrase)
        except Exception:  # noqa: BLE001 — acks are optional
            pass
    buf, said_any, still_n = "", False, 0
    deadline = time.monotonic() + settings.ack_still_after
    while True:
        try:
            item = pieces.get(timeout=0.2)
        except queue.Empty:
            if not said_any and time.monotonic() > deadline and (line := acks.still(still_n)):
                yield "ack", line, acks.audio(line)
                still_n += 1
                deadline = time.monotonic() + settings.ack_still_after * 2
            continue
        if isinstance(item, tuple):
            if buf.strip():
                yield "sentence", buf.strip(), speak(buf.strip())
            turn.save()
            yield "done", item[1], b""
            return
        ready, buf = split_ready(buf + item)
        for sentence in ready:
            said_any = True
            yield "sentence", sentence, speak(sentence)
