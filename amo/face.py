"""AMO's face: what the /face page shows.

Whatever plays AMO's voice (the Mac listener, a body device) reports:
- state changes: idle | listening | thinking | asleep
- each sentence it's about to say, with when it starts, the time of every word (so captions can
  appear one word at a time, in sync) and a loudness curve (so the mouth moves with the voice).
"""

from __future__ import annotations

import io
import json
import re
import time
import wave
from typing import Any

from .db import Database, get_db, now_iso

ENV_STEP_MS = 50  # loudness curve resolution


def _now_ms() -> int:
    return int(time.time() * 1000)


def _publish(kind: str, data: dict[str, Any], db: Database | None = None) -> None:
    db = db or get_db()
    db.insert("face_events", {"kind": kind, "data": json.dumps(data), "created_at": now_iso()})
    newest = db.scalar("SELECT MAX(id) FROM face_events") or 0
    if newest % 100 == 0:  # keep the table small
        db.execute("DELETE FROM face_events WHERE id <= ?", (newest - 200,))


def publish_state(state: str, db: Database | None = None) -> None:
    """idle | listening | thinking | asleep"""
    try:
        _publish("state", {"state": state, "at": _now_ms()}, db)
    except Exception:  # noqa: BLE001 — the face must never break voice
        pass


def say_event(text: str, wav: bytes, start_ms: int | None = None) -> dict[str, Any]:
    """Word timings and a loudness curve for one spoken sentence."""
    import numpy as np

    from .voice.speakable import to_speech

    with wave.open(io.BytesIO(wav)) as w:
        rate = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    step = max(1, int(rate * ENV_STEP_MS / 1000))
    n = max(1, len(x) // step)
    env = np.sqrt(np.mean(x[: n * step].reshape(n, step) ** 2, axis=1)) if len(x) >= step else np.zeros(1)
    top = float(np.percentile(env, 95)) or 1.0
    env = np.clip(env / top, 0, 1)
    loud = np.flatnonzero(env > 0.15)
    first, last = (int(loud[0]), int(loud[-1]) + 1) if len(loud) else (0, n)
    speech_start, speech_end = first * ENV_STEP_MS, last * ENV_STEP_MS

    spoken = to_speech(text)
    words = spoken.split()
    # Longer words take longer; punctuation adds a short pause after the word.
    weights = [len(re.sub(r"\W", "", w)) + 1.5 + (4 if re.search(r"[.!?]$", w) else 2 if re.search(r"[,;:]$", w) else 0)
               for w in words] or [1]
    total = sum(weights)
    span = max(speech_end - speech_start, 1)
    times, acc = [], 0.0
    for wt in weights:
        times.append(int(speech_start + span * acc / total))
        acc += wt
    return {
        "text": spoken,
        "words": [{"w": w, "t": t} for w, t in zip(words, times)],
        "env": [round(float(v), 2) for v in env],
        "step": ENV_STEP_MS,
        "dur": int(len(x) / rate * 1000),
        "start": start_ms if start_ms is not None else _now_ms(),
    }


def publish_say(text: str, wav: bytes, start_ms: int | None = None, db: Database | None = None) -> None:
    try:
        _publish("say", say_event(text, wav, start_ms), db)
    except Exception:  # noqa: BLE001
        pass


def events_after(after_id: int, db: Database | None = None, limit: int = 50) -> list[dict[str, Any]]:
    db = db or get_db()
    rows = db.all("SELECT id, kind, data FROM face_events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit))
    return [{"id": r["id"], "kind": r["kind"], **json.loads(r["data"])} for r in rows]


def latest_id(db: Database | None = None) -> int:
    return int((db or get_db()).scalar("SELECT COALESCE(MAX(id), 0) FROM face_events"))
