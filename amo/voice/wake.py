"""Lightweight wake-word engines for AMO's body (AMO_WAKE_ENGINE):

  whisper       default on the Mac: a tiny Whisper model checks each burst of speech
  vosk          grammar-limited recognizer that only listens for "Hey AMO" — light enough for a
                Raspberry Pi, no training. Setup: amo setup-wake vosk
  openwakeword  a tiny model trained on "Hey AMO" — the lightest and most accurate; runs on very
                small hardware. Setup: amo setup-wake openwakeword (training steps in docs/WAKE_WORD.md)

The streaming engines hear "Hey AMO" as it's said, then capture whatever you say right after
("Hey AMO, what's on today?") — only that part is sent for full recognition.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol

from ..config import settings
from .stt import VoiceUnavailable

VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
_NAMES = {"amo", "ammo", "emo"}


@dataclass
class WakeEvent:
    """"Hey AMO" was heard; `audio` is what you said right after (or None)."""
    audio: Any = None


class Detector(Protocol):
    def process(self, block) -> bool: ...
    def reset(self) -> None: ...


def vosk_heard_wake(text: str) -> bool:
    """What Vosk heard starts with 'hey amo' / 'hey ammo' / 'hey a mo'. "Hey" is required:
    on its own, 'ammo' is too easy to hear in normal speech ("need more" → "ammo")."""
    words = text.split()  # [unk] kept, so a "hey ammo" later in a sentence doesn't count
    return len(words) >= 2 and words[0] == "hey" and (words[1] in _NAMES or words[1:3] == ["a", "mo"])


class VoskDetector:
    GRAMMAR = ["hey amo", "hey ammo", "hey a mo", "hey emo", "hey", "[unk]"]

    def __init__(self, model_dir: str):
        try:
            import vosk
        except ImportError as e:
            raise VoiceUnavailable("Vosk isn't installed: amo setup-wake vosk") from e
        if not Path(model_dir).is_dir():
            raise VoiceUnavailable(f"Vosk model not found at {model_dir}: amo setup-wake vosk")
        vosk.SetLogLevel(-1)
        self._vosk = vosk
        self.model = vosk.Model(model_dir)
        self.reset()

    def reset(self) -> None:
        self.rec = self._vosk.KaldiRecognizer(self.model, 16000, json.dumps(self.GRAMMAR))

    def process(self, block) -> bool:
        import numpy as np

        data = (np.clip(block, -1, 1) * 32767).astype(np.int16).tobytes()
        if self.rec.AcceptWaveform(data):
            text = json.loads(self.rec.Result()).get("text", "")
        else:
            text = json.loads(self.rec.PartialResult()).get("partial", "")
        if vosk_heard_wake(text):
            self.reset()
            return True
        return False


class OpenWakeWordDetector:
    FRAME = 1280  # 80 ms at 16 kHz

    def __init__(self, model_path: str, threshold: float = 0.5):
        try:
            from openwakeword.model import Model
        except ImportError as e:
            raise VoiceUnavailable("openWakeWord isn't installed: amo setup-wake openwakeword") from e
        if not Path(model_path).is_file():
            raise VoiceUnavailable(f"No wake-word model at {model_path} — see docs/WAKE_WORD.md to train one")
        import numpy as np

        framework = "onnx" if model_path.endswith(".onnx") else "tflite"
        self.model = Model(wakeword_models=[model_path], inference_framework=framework)
        self.threshold = threshold
        self.buf = np.zeros(0, dtype=np.int16)
        self._np = np

    def reset(self) -> None:
        self.model.reset()
        self.buf = self._np.zeros(0, dtype=self._np.int16)

    def process(self, block) -> bool:
        np = self._np
        self.buf = np.concatenate([self.buf, (np.clip(block, -1, 1) * 32767).astype(np.int16)])
        while len(self.buf) >= self.FRAME:
            frame, self.buf = self.buf[: self.FRAME], self.buf[self.FRAME:]
            scores = self.model.predict(frame)
            if scores and max(scores.values()) >= self.threshold:
                self.reset()
                return True
        return False


def make_detector(engine: str | None = None) -> Detector | None:
    engine = (engine or settings.wake_engine).lower()
    if engine == "vosk":
        return VoskDetector(settings.vosk_model)
    if engine in ("openwakeword", "oww"):
        return OpenWakeWordDetector(settings.oww_model, settings.oww_threshold)
    return None  # whisper: handled by the regular segment-and-transcribe path


class StreamingWake:
    """Feeds every 100 ms of mic audio to a detector; yields a WakeEvent when it fires, with the
    speech that follows (if you kept talking)."""

    def __init__(self, mic, detector: Detector, follow_window: float = 0.7):
        self.mic, self.detector = mic, detector
        self.follow_blocks = int(follow_window * 10)

    def __call__(self, timeout: float | None) -> Iterator[WakeEvent]:
        import time

        deadline = time.monotonic() + timeout if timeout else None
        quiet, armed = 0, True
        while not deadline or time.monotonic() < deadline:
            block = self.mic._read()
            level = self.mic._rms(block)
            self.mic._watch(level)
            # A pause means a new sentence: start the detector fresh, so "Hey AMO" said after
            # other talk still counts as the start of what you said.
            quiet = quiet + 1 if level < self.mic.threshold else 0
            if quiet == 5:
                self.detector.reset()
                armed = True
            if self.detector.process(block) and armed:
                yield WakeEvent(self._follow_on())
                self.detector.reset()
                armed = False  # one wake per sentence — wait for a pause before the next
                if deadline:
                    deadline = time.monotonic() + timeout

    def _follow_on(self):
        """If speech continues right after "Hey AMO", capture it until you pause."""
        np = self.mic.np
        for _ in range(self.follow_blocks):
            block = self.mic._read()
            if self.mic._rms(block) >= self.mic.threshold:
                chunks, quiet = [block], 0
                while len(chunks) < self.mic.max_blocks:
                    b = self.mic._read()
                    chunks.append(b)
                    quiet = quiet + 1 if self.mic._rms(b) < self.mic.threshold else 0
                    if quiet >= self.mic.silence_blocks:
                        break
                return np.concatenate(chunks)
        return None


def setup(engine: str) -> None:
    """amo setup-wake vosk|openwakeword"""
    import subprocess
    import sys
    import urllib.request
    import zipfile

    if engine == "vosk":
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "vosk"], check=False)
        target = Path(settings.vosk_model)
        if not target.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
            zpath = target.parent / "vosk.zip"
            print("downloading the Vosk English model (~40 MB) …")
            urllib.request.urlretrieve(VOSK_URL, zpath)
            with zipfile.ZipFile(zpath) as z:
                z.extractall(target.parent)
            zpath.unlink()
        print(f"Vosk ready at {target}. Set AMO_WAKE_ENGINE=vosk in .env (amo will restart listening).")
    elif engine in ("openwakeword", "oww"):
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "openwakeword"], check=False)
        print("openWakeWord installed. Train your “Hey AMO” model with the steps in docs/WAKE_WORD.md,\n"
              f"save it as {settings.oww_model}, then set AMO_WAKE_ENGINE=openwakeword in .env.")
    else:
        raise SystemExit("choose: vosk or openwakeword")
