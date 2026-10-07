"""Speak while thinking: text arrives piece by piece, gets cut into sentences, and each
sentence is voiced as soon as it's complete — the next one is synthesized while the current
one plays. Can be stopped instantly (barge-in: "AMO, stop").
"""

from __future__ import annotations

import io
import queue
import re
import threading
import time
import wave
from typing import Callable

# A sentence ends at . ! ? or a newline followed by whitespace — "261.12" doesn't split.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|\n+")
_MIN_FIRST = 12  # don't voice a tiny fragment like "Ok." on its own unless that's all there is


def split_ready(buffer: str) -> tuple[list[str], str]:
    """Split off complete sentences; return (sentences, remainder still being written)."""
    parts = _SENTENCE_END.split(buffer)
    if len(parts) == 1:
        return [], buffer
    done, rest = parts[:-1], parts[-1]
    out, carry = [], ""
    for p in done:
        carry = f"{carry} {p}".strip() if carry else p.strip()
        if len(carry) >= _MIN_FIRST:
            out.append(carry)
            carry = ""
    if carry:
        rest = f"{carry} {rest}".strip()
    return out, rest


def play_interruptible(wav: bytes, stop: threading.Event) -> bool:
    """Play WAV bytes; returns False if stopped early."""
    import numpy as np
    import sounddevice as sd

    with wave.open(io.BytesIO(wav)) as w:
        rate = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype("float32") / 32768
    sd.play(audio, rate)
    end = time.monotonic() + len(audio) / rate + 0.3
    while time.monotonic() < end:
        if stop.is_set():
            sd.stop()
            return False
        time.sleep(0.03)
    sd.wait()
    return True


class SentenceSpeaker:
    def __init__(self, synth: Callable[[str], bytes], play: Callable[[bytes, threading.Event], bool] = play_interruptible):
        self.synth, self.play = synth, play
        self._buf = ""
        self._texts: queue.Queue = queue.Queue()   # sentences (str) or pre-made audio (bytes); None = end
        self._audio: queue.Queue = queue.Queue()
        self.stopped = threading.Event()
        self._playing = False
        self.spoken: list[str] = []                 # what has been / is being said (for echo filtering)
        self.started = threading.Event()            # first audio began playing
        self._done = threading.Event()
        threading.Thread(target=self._synth_loop, daemon=True).start()
        threading.Thread(target=self._play_loop, daemon=True).start()

    # ---- feeding
    def feed(self, piece: str) -> None:
        if self.stopped.is_set():
            return
        self._buf += piece
        ready, self._buf = split_ready(self._buf)
        for sentence in ready:
            self._texts.put(sentence)

    def say_audio(self, wav: bytes, label: str = "") -> None:
        """Queue pre-rendered audio (e.g. a cached "Got it.")."""
        if not self.stopped.is_set():
            if label:
                self.spoken.append(label)
            self._texts.put((wav, label))

    def finish(self) -> None:
        if self._buf.strip():
            self._texts.put(self._buf.strip())
        self._buf = ""
        self._texts.put(None)

    def stop(self) -> None:
        self.stopped.set()
        self._texts.put(None)

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    @property
    def idle(self) -> bool:
        """Nothing queued or playing right now."""
        return self._texts.empty() and self._audio.empty() and not self._playing

    _playing = False

    # ---- workers
    def _synth_loop(self) -> None:
        while True:
            item = self._texts.get()
            if item is None or self.stopped.is_set():
                self._audio.put(None)
                return
            if isinstance(item, tuple):
                self._audio.put(item)
                continue
            self.spoken.append(item)  # known before it's heard, so its echo is never mistaken for you
            try:
                self._audio.put((self.synth(item), ""))
            except Exception:  # noqa: BLE001 — skip a sentence rather than go silent
                continue

    def _play_loop(self) -> None:
        try:
            while True:
                item = self._audio.get()
                if item is None or self.stopped.is_set():
                    return
                wav, _label = item
                self._playing = True
                self.started.set()
                finished = self.play(wav, self.stopped)
                self._playing = False
                if not finished:
                    return
        finally:
            self._playing = False
            self._done.set()
