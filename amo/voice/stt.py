"""Speech-to-text with faster-whisper (local Whisper, CPU or GPU)."""

from __future__ import annotations

import io
import threading
from typing import Any

from ..config import settings

_models: dict[str, Any] = {}
_lock = threading.Lock()


class VoiceUnavailable(RuntimeError):
    pass


def _get_model(name: str | None = None):
    name = name or settings.whisper_model
    with _lock:
        if name not in _models:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise VoiceUnavailable('faster-whisper not installed: pip install -e ".[voice]"') from e
            # int8 runs well on CPU; faster-whisper uses CUDA automatically with device="auto".
            _models[name] = WhisperModel(name, device="auto", compute_type="int8")
        return _models[name]


def transcribe(audio: bytes | str, language: str | None = "en", size: str | None = None) -> str:
    """Transcribe a WAV/MP3/WebM file path or raw file bytes (size: e.g. "tiny.en")."""
    model = _get_model(size)
    src = io.BytesIO(audio) if isinstance(audio, (bytes, bytearray)) else audio
    segments, _info = model.transcribe(src, language=language, vad_filter=True, beam_size=1)
    return " ".join(s.text.strip() for s in segments).strip()


def transcribe_array(samples, sample_rate: int = 16000, model: str | None = None,
                     prompt: str | None = None) -> str:
    """Transcribe a float32 mono numpy array (from the microphone)."""
    m = _get_model(model)
    if sample_rate != 16000:
        import numpy as np

        idx = np.linspace(0, len(samples) - 1, int(len(samples) * 16000 / sample_rate))
        samples = np.interp(idx, np.arange(len(samples)), samples).astype("float32")
    segments, _ = m.transcribe(samples, language="en", vad_filter=False, beam_size=1,
                               initial_prompt=prompt, condition_on_previous_text=False)
    return " ".join(s.text.strip() for s in segments).strip()
