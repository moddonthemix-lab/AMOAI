"""Speech-to-text with faster-whisper (local Whisper, CPU or GPU)."""

from __future__ import annotations

import io
import threading
from typing import Any

from ..config import settings

_model: Any = None
_lock = threading.Lock()


class VoiceUnavailable(RuntimeError):
    pass


def _get_model():
    global _model
    with _lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise VoiceUnavailable('faster-whisper not installed: pip install -e ".[voice]"') from e
            # int8 runs well on CPU; faster-whisper uses CUDA automatically with device="auto".
            _model = WhisperModel(settings.whisper_model, device="auto", compute_type="int8")
        return _model


def transcribe(audio: bytes | str, language: str | None = "en") -> str:
    """Transcribe a WAV/MP3/WebM file path or raw file bytes."""
    model = _get_model()
    src = io.BytesIO(audio) if isinstance(audio, (bytes, bytearray)) else audio
    segments, _info = model.transcribe(src, language=language, vad_filter=True, beam_size=1)
    return " ".join(s.text.strip() for s in segments).strip()


def transcribe_array(samples, sample_rate: int = 16000) -> str:
    """Transcribe a float32 mono numpy array (from the microphone)."""
    model = _get_model()
    if sample_rate != 16000:
        import numpy as np

        idx = np.linspace(0, len(samples) - 1, int(len(samples) * 16000 / sample_rate))
        samples = np.interp(idx, np.arange(len(samples)), samples).astype("float32")
    segments, _ = model.transcribe(samples, language="en", vad_filter=True, beam_size=1)
    return " ".join(s.text.strip() for s in segments).strip()
