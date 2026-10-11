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
                 agent_chat=None) -> Iterator[tuple[str, str, bytes]]:
    from ..agent import Agent
    from ..llm import LLMError
    from . import acks
    from .speaker import split_ready
    from .tts import synthesize

    pieces: queue.Queue = queue.Queue()
    chat = agent_chat or Agent().chat

    def think() -> None:
        try:
            reply = chat(history, channel=channel, on_text=pieces.put)["content"]
            pieces.put(("done", reply))
        except LLMError as e:
            pieces.put(f"Sorry, I couldn't do that: {e}")
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
                yield "sentence", buf.strip(), synthesize(buf.strip())
            yield "done", item[1], b""
            return
        ready, buf = split_ready(buf + item)
        for sentence in ready:
            said_any = True
            yield "sentence", sentence, synthesize(sentence)
