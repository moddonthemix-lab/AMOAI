"""Voice in/out, all local.

- Speech-to-text: faster-whisper (runs on CPU on the Pi, GPU on the desktop).
- Text-to-speech: Piper if installed (natural, fast on a Pi), else pyttsx3, else print.
- Microphone: sounddevice, with simple energy-based end-of-speech detection.

Install the extras with ``pip install -e .[voice]``. Phase 2 adds a wake word
in front of ``listen()``; everything after it stays the same.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile

SAMPLE_RATE = 16_000
CHUNK_SECONDS = 0.1


class VoiceUnavailable(RuntimeError):
    pass


def _require(module: str):
    try:
        return __import__(module)
    except ImportError as e:
        raise VoiceUnavailable(
            f"Voice needs '{module}'. Install voice extras: pip install -e '.[voice]'"
        ) from e


class Voice:
    def __init__(self, whisper_model: str = "base.en", piper_voice: str = ""):
        self.whisper_model_name = whisper_model
        self.piper_voice = piper_voice
        self._whisper = None
        self._tts = None

    # ------------------------------------------------------------- listening
    def record(self, max_seconds: float = 20, silence_seconds: float = 1.2,
               threshold: float = 0.015):
        """Record from the default mic until the speaker pauses. Returns float32 mono audio."""
        np = _require("numpy")
        sd = _require("sounddevice")
        chunk = int(SAMPLE_RATE * CHUNK_SECONDS)
        frames, heard_speech, quiet = [], False, 0.0
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32") as stream:
            for _ in range(int(max_seconds / CHUNK_SECONDS)):
                data, _ = stream.read(chunk)
                frames.append(data[:, 0].copy())
                level = float(np.sqrt(np.mean(data ** 2)))
                if level > threshold:
                    heard_speech, quiet = True, 0.0
                elif heard_speech:
                    quiet += CHUNK_SECONDS
                    if quiet >= silence_seconds:
                        break
        return np.concatenate(frames) if frames else np.zeros(0, dtype="float32")

    def transcribe(self, audio) -> str:
        if self._whisper is None:
            fw = _require("faster_whisper")
            self._whisper = fw.WhisperModel(self.whisper_model_name, device="auto", compute_type="int8")
        segments, _ = self._whisper.transcribe(audio, language="en", vad_filter=True)
        return " ".join(s.text.strip() for s in segments).strip()

    def listen(self) -> str:
        return self.transcribe(self.record())

    # -------------------------------------------------------------- speaking
    def say(self, text: str) -> None:
        if not text:
            return
        if self.piper_voice and shutil.which("piper"):
            self._say_piper(text)
            return
        try:
            if self._tts is None:
                self._tts = _require("pyttsx3").init()
            self._tts.say(text)
            self._tts.runAndWait()
        except VoiceUnavailable:
            print(f"(no TTS engine) {text}", file=sys.stderr)

    def _say_piper(self, text: str) -> None:
        with tempfile.NamedTemporaryFile(suffix=".wav") as wav:
            subprocess.run(["piper", "--model", self.piper_voice, "--output_file", wav.name],
                           input=text.encode(), check=True, capture_output=True)
            player = next((p for p in ("aplay", "afplay", "paplay") if shutil.which(p)), None)
            if player:
                subprocess.run([player, wav.name], check=False, capture_output=True)
            else:
                sf = _require("soundfile")
                sd = _require("sounddevice")
                audio, sr = sf.read(wav.name)
                sd.play(audio, sr)
                sd.wait()
