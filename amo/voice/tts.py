"""Text-to-speech with Piper (https://github.com/rhasspy/piper) — fast, local, runs on a Pi.

Download a voice (an .onnx file plus its .onnx.json) into ./models/piper/, e.g.
en_US-lessac-medium from https://huggingface.co/rhasspy/piper-voices — or run
`amo setup-voice`.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any

from ..config import settings
from .stt import VoiceUnavailable

_voice: Any = None
_lock = threading.Lock()


def clean_for_speech(text: str) -> str:
    """Strip markdown so Piper doesn't read out asterisks and pound signs."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_#>|~]", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


def _load_voice():
    global _voice
    with _lock:
        if _voice is None:
            if not Path(settings.piper_voice).is_file():
                raise VoiceUnavailable(
                    f"Piper voice not found at {settings.piper_voice}. Run `amo setup-voice`."
                )
            try:
                from piper import PiperVoice
            except ImportError:
                _voice = "cli"
                return _voice
            _voice = PiperVoice.load(settings.piper_voice)
        return _voice


def _macos_say(text: str) -> bytes:
    """Fallback on a Mac: the built-in `say` voice, so voice works before Piper is set up."""
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        subprocess.run(["say", "-o", f.name, "--data-format=LEI16@22050", "-f", "-"],
                       input=text.encode(), check=True)
        return Path(f.name).read_bytes()


def synthesize(text: str) -> bytes:
    """Return WAV bytes for `text`."""
    text = clean_for_speech(text)
    try:
        voice = _load_voice()
    except VoiceUnavailable:
        if sys.platform == "darwin" and shutil.which("say"):
            return _macos_say(text)
        raise
    if voice == "cli":
        exe = shutil.which("piper")
        if not exe:
            raise VoiceUnavailable('Piper not installed: pip install -e ".[voice]"')
        out = subprocess.run(
            [exe, "--model", settings.piper_voice, "--output_file", "-"],
            input=text.encode(), capture_output=True, check=True,
        )
        return out.stdout
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        if hasattr(voice, "synthesize_wav"):  # piper-tts >= 1.3
            voice.synthesize_wav(text, wav)
        else:  # piper-tts 1.2
            voice.synthesize(text, wav)
    return buf.getvalue()
