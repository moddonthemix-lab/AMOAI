"""AMO's body: a thin voice client for a Raspberry Pi (or any computer with a mic and speaker).

The device is just ears and a mouth. Thinking happens on the "brain" (your Mac running
`amo serve`), over Wi-Fi:

    amo device --brain http://Your-Mac.local:8765 --key YOUR_AMO_API_KEY

- listens locally (loudness gate), sends speech to the brain to recognise
- the brain streams back "Got it", then each sentence's audio as soon as it's ready
- you can interrupt AMO mid-answer ("AMO, stop")
"""

from __future__ import annotations

import base64
import io
import json
import threading
import wave
from typing import Any, Iterator

import httpx

from ..config import settings
from .listen import SAMPLE_RATE, WAKE_PROMPT, Listener, MicSegmenter, barge_in, chime_wav  # noqa: F401
from .speaker import SentenceSpeaker, play_interruptible


def to_wav(samples) -> bytes:
    import numpy as np

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


class Brain:
    """Talks to the AMO server on your Mac."""

    def __init__(self, url: str, key: str = "", timeout: float = 300):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        r = httpx.get(f"{self.url}/api/health", timeout=10)
        r.raise_for_status()
        return r.json()

    def hear(self, samples, model: str = "whisper-1") -> str:
        files = {"file": ("speech.wav", to_wav(samples), "audio/wav")}
        r = httpx.post(f"{self.url}/v1/audio/transcriptions", files=files, data={"model": model},
                       headers=self.headers, timeout=60)
        r.raise_for_status()
        return r.json().get("text", "")

    def speak(self, text: str) -> bytes:
        r = httpx.post(f"{self.url}/v1/audio/speech", json={"input": text}, headers=self.headers, timeout=60)
        r.raise_for_status()
        return r.content

    def brief(self) -> str:
        r = httpx.get(f"{self.url}/api/brief", headers=self.headers, timeout=60)
        r.raise_for_status()
        return r.json()["text"]

    def announcements(self) -> list[str]:
        r = httpx.get(f"{self.url}/api/announcements/next", headers=self.headers, timeout=15)
        r.raise_for_status()
        return r.json().get("say", [])

    def face(self, kind: str, data: dict[str, Any]) -> None:
        """Tell the brain what AMO's face should show (best effort — never blocks speech)."""
        try:
            httpx.post(f"{self.url}/api/face/event", json={"kind": kind, **data}, headers=self.headers, timeout=3)
        except httpx.HTTPError:
            pass

    def converse(self, text: str, history: list[dict[str, str]]) -> Iterator[dict[str, Any]]:
        body = {"text": text, "history": history[:-1] if history and history[-1]["content"] == text else history}
        with httpx.stream("POST", f"{self.url}/api/converse", json=body, headers=self.headers,
                          timeout=self.timeout) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if line.strip():
                    yield json.loads(line)


def face_say(brain: Brain):
    """Word timing is worked out on the device; the brain stamps the start time on arrival."""
    from ..face import say_event

    def on_play(text: str, wav: bytes) -> None:
        ev = say_event(text, wav, start_ms=0)
        ev.pop("start", None)
        threading.Thread(target=brain.face, args=("say", ev), daemon=True).start()

    return on_play


def make_respond(brain: Brain, mic: MicSegmenter, verbose: bool = False):
    def respond(history: list[dict[str, str]], command: str) -> tuple[str, str | None]:
        speaker = SentenceSpeaker(synth=brain.speak, play=play_interruptible, on_play=face_say(brain))
        result: dict[str, Any] = {"reply": ""}

        def stream() -> None:
            try:
                for ev in brain.converse(command, history):
                    if ev["type"] in ("ack", "sentence"):
                        speaker.say_audio(base64.b64decode(ev["wav"]), ev["text"])
                    elif ev["type"] == "done":
                        result["reply"] = ev.get("reply", "")
            except httpx.HTTPError as e:
                print(f"   ✗ brain unreachable: {e}")
                result["reply"] = ""
            finally:
                speaker.finish()

        threading.Thread(target=stream, daemon=True).start()
        interrupt: str | None = None
        flushed = False
        while not speaker.wait(0.05):
            if settings.barge_in and speaker.started.is_set():
                if not flushed:
                    mic.flush()
                    flushed = True
                seg = mic.listen_once(timeout=0.4, max_seconds=3.0)
                if seg is not None:
                    heard = brain.hear(seg, settings.wake_model)
                    hit, rest = barge_in(heard, " ".join(speaker.spoken))
                    if hit:
                        speaker.stop()
                        interrupt = rest
                        print(f"   ✋ heard “{heard}” — stopping")
                        break
        mic.flush()
        return result["reply"], interrupt

    return respond


def run(brain_url: str, key: str = "", verbose: bool = False) -> None:
    from .loop import play_wav

    brain = Brain(brain_url, key)
    try:
        info = brain.health()
        print(f"Connected to AMO's brain at {brain_url} (Ollama {'✓' if info.get('ollama') else '✗'})")
    except httpx.HTTPError as e:
        print(f"Can't reach the brain at {brain_url}: {e}\n"
              "On the Mac run:  amo lan on   — then use the address and key it prints.")
        return

    mic = MicSegmenter(sensitivity=settings.wake_sensitivity, meter=verbose)
    chime = chime_wav()

    show = face_say(brain)

    def say(text: str) -> None:
        if text:
            wav = brain.speak(text)
            show(text, wav)
            play_wav(wav)
        mic.flush()

    def ding() -> None:
        play_wav(chime)
        mic.flush()

    # With a local wake engine (vosk/openwakeword) only speech AFTER "Hey AMO" leaves the device.
    from .wake import StreamingWake, WakeEvent, make_detector

    detector = make_detector()
    print(f"Wake word engine: {settings.wake_engine}" + ("" if detector else " (checked by the brain)"))

    def wake_text(seg) -> str:
        if isinstance(seg, WakeEvent):
            return "Hey AMO " + (brain.hear(seg.audio) if seg.audio is not None else "")
        return brain.hear(seg, settings.wake_model)

    def command_text(seg) -> str:
        if isinstance(seg, WakeEvent):
            return "Hey AMO " + (brain.hear(seg.audio) if seg.audio is not None else "")
        return brain.hear(seg)

    listener = Listener(
        segments=mic,
        wake_stream=StreamingWake(mic, detector) if detector else None,
        transcribe_wake=wake_text,
        transcribe_command=command_text,
        ask=lambda history: "",
        say=say,
        chime=ding,
        respond=make_respond(brain, mic, verbose),
        brief=brain.brief,
        announcements=brain.announcements,
        on_state=lambda state: threading.Thread(target=brain.face, args=("state", {"state": state}), daemon=True).start(),
        verbose=verbose,
    )
    try:
        listener.run()
    except KeyboardInterrupt:
        print()
