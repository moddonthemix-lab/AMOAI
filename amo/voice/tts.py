"""Text-to-speech: voice presets on top of Piper (local neural voices) or the built-in
macOS voices, plus optional pitch and "robot" effects.

Presets (AMO_VOICE):
  computer        dry, posh British computer — lower pitch, metallic edge
  jarvis          calm British butler with a light digital polish
  british-female  British female voice
  default         neutral American voice

AMO_VOICE can also be a Piper voice name ("en_GB-alan-medium") or a macOS voice
("say:Daniel"). AMO_VOICE_RATE / AMO_VOICE_PITCH / AMO_VOICE_ROBOT override the preset.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import wave
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..config import settings
from .stt import VoiceUnavailable

PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


@dataclass(frozen=True)
class VoiceStyle:
    piper: str            # Piper voice name
    say: tuple[str, ...]  # macOS voices to try, in order
    rate: float = 1.0     # speaking speed (1 = normal)
    pitch: float = 0.0    # semitones
    robot: float = 0.0    # 0..1 metallic/ring-mod amount
    personality: str = "default"


PRESETS: dict[str, VoiceStyle] = {
    "computer": VoiceStyle("en_GB-alan-medium", ("Daniel", "Oliver", "Arthur"),
                           rate=0.96, pitch=-1.5, robot=0.32, personality="computer"),
    "jarvis": VoiceStyle("en_GB-alan-medium", ("Daniel", "Oliver", "Arthur"),
                         rate=1.0, pitch=-0.5, robot=0.12, personality="jarvis"),
    "british-female": VoiceStyle("en_GB-jenny_dioco-medium", ("Kate", "Serena", "Stephanie", "Martha", "Daniel"),
                                 personality="jarvis"),
    "default": VoiceStyle("en_US-lessac-medium", ("Samantha", "Alex")),
}

_piper_cache: dict[str, Any] = {}
_lock = threading.Lock()


def clean_for_speech(text: str) -> str:
    """Strip markdown and emoji so the voice doesn't read out symbols."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_#>|~]", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.M)
    text = re.sub(r"[\U0001F000-\U0001FFFF☀-➿]", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- style resolution
def resolve_style(voice: str | None = None) -> VoiceStyle:
    name = (voice or "").strip() or settings.voice
    if name in PRESETS:
        style = PRESETS[name]
    elif name.startswith("say:"):
        style = VoiceStyle(PRESETS["default"].piper, (name[4:],))
    elif re.match(r"^[a-z]{2}_[A-Z]{2}-", name):  # Piper voice, optionally "name:speaker"
        style = VoiceStyle(name, PRESETS["default"].say)
    else:
        style = PRESETS.get(settings.voice, PRESETS["default"])
    overrides = {}
    if settings.voice_rate:
        overrides["rate"] = float(settings.voice_rate)
    if settings.voice_pitch:
        overrides["pitch"] = float(settings.voice_pitch)
    if settings.voice_robot:
        overrides["robot"] = float(settings.voice_robot)
    return replace(style, **overrides) if overrides else style


def piper_dir() -> Path:
    return Path(settings.piper_voice).parent


def split_speaker(voice_name: str) -> tuple[str, str | None]:
    """'en_GB-semaine-medium:obadiah' → ('en_GB-semaine-medium', 'obadiah')."""
    base, _, speaker = voice_name.partition(":")
    return base, speaker or None


def piper_path(voice_name: str) -> Path:
    return piper_dir() / f"{split_speaker(voice_name)[0]}.onnx"


def speaker_id(voice_name: str) -> int | None:
    base, speaker = split_speaker(voice_name)
    if speaker is None:
        return None
    if speaker.isdigit():
        return int(speaker)
    import json

    meta = json.loads(Path(str(piper_path(base)) + ".json").read_text())
    ids = meta.get("speaker_id_map") or {}
    if speaker not in ids:
        raise VoiceUnavailable(f"{base} has no speaker '{speaker}' (choose from: {', '.join(ids)})")
    return ids[speaker]


def piper_installed() -> bool:
    try:
        import piper  # noqa: F401
    except ImportError:
        return False
    return True


def download_piper_voice(voice_name: str) -> Path:
    """Fetch a Piper voice (e.g. en_GB-alan-medium) from the official voice repo."""
    voice_name = split_speaker(voice_name)[0]
    m = re.match(r"^(([a-z]{2})_[A-Z]{2})-(.+)-(x_low|low|medium|high)$", voice_name)
    if not m:
        raise ValueError(f"not a Piper voice name: {voice_name}")
    locale, lang, speaker, quality = m.groups()
    dest = piper_path(voice_name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", ".json"):
        target = Path(str(dest) + suffix)
        if not target.exists():
            url = f"{PIPER_BASE}/{lang}/{locale}/{speaker}/{quality}/{voice_name}.onnx{suffix}"
            urllib.request.urlretrieve(url, target)
    return dest


def mac_voices() -> list[str]:
    if sys.platform != "darwin" or not shutil.which("say"):
        return []
    out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
    return [line.split("  ")[0].strip() for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------- engines
def _piper_wav(text: str, style: VoiceStyle, speed: float) -> bytes:
    path = piper_path(style.piper)
    if not path.is_file():
        # Back-compat: the original single voice file from `amo setup-voice`.
        legacy = Path(settings.piper_voice)
        if style.piper == PRESETS["default"].piper and legacy.is_file():
            path = legacy
        else:
            raise VoiceUnavailable(f"Piper voice {style.piper} not downloaded (amo set-voice downloads it)")
    if not piper_installed():
        raise VoiceUnavailable("Piper isn't installed")
    from piper import PiperVoice

    with _lock:
        voice = _piper_cache.get(str(path))
        if voice is None:
            voice = _piper_cache[str(path)] = PiperVoice.load(str(path))
    length_scale = 1.0 / max(speed, 0.1)
    sid = speaker_id(style.piper)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        if hasattr(voice, "synthesize_wav"):  # piper-tts >= 1.3
            try:
                from piper import SynthesisConfig

                cfg = SynthesisConfig(length_scale=length_scale, speaker_id=sid)
                voice.synthesize_wav(text, wav, syn_config=cfg)
            except ImportError:
                voice.synthesize_wav(text, wav)
        else:  # piper-tts 1.2
            voice.synthesize(text, wav, length_scale=length_scale, speaker_id=sid)
    return buf.getvalue()


def _say_wav(text: str, style: VoiceStyle, speed: float) -> bytes:
    if sys.platform != "darwin" or not shutil.which("say"):
        raise VoiceUnavailable("no voice engine available (install Piper: pip install piper-tts)")
    installed = set(mac_voices())
    voice = next((v for v in style.say if v in installed), None)
    cmd = ["say", "-r", str(int(185 * speed))]
    if voice:
        cmd += ["-v", voice]
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        subprocess.run(cmd + ["-o", f.name, "--data-format=LEI16@22050", "-f", "-"],
                       input=text.encode(), check=True)
        return Path(f.name).read_bytes()


# ---------------------------------------------------------------- effects
def apply_effects(wav_bytes: bytes, pitch: float = 0.0, robot: float = 0.0) -> bytes:
    """Pitch shift (by resampling; the engine already slowed down to compensate) and a
    metallic 'computer' timbre (ring modulation + short comb delay)."""
    if not pitch and not robot:
        return wav_bytes
    try:
        import numpy as np
    except ImportError:
        return wav_bytes
    with wave.open(io.BytesIO(wav_bytes)) as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        frames = w.readframes(w.getnframes())
    if width != 2:
        return wav_bytes
    x = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)

    if pitch:
        factor = 2 ** (pitch / 12)
        n = max(1, int(len(x) / factor))
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)

    if robot:
        t = np.arange(len(x), dtype=np.float32) / rate
        ring = x * np.sin(2 * np.pi * 48.0 * t)
        delay = int(0.006 * rate)
        comb = np.concatenate([np.zeros(delay, dtype=np.float32), x[:-delay]]) if delay < len(x) else x
        x = (1 - robot) * x + robot * 0.8 * ring + robot * 0.45 * comb

    peak = float(np.max(np.abs(x))) or 1.0
    x = np.clip(x / peak * 0.92, -1, 1)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((x * 32767).astype(np.int16).tobytes())
    return out.getvalue()


def synthesize(text: str, voice: str | None = None) -> bytes:
    """Return WAV bytes for `text` in the configured (or given) voice."""
    text = clean_for_speech(text)
    if not text:
        text = "Done."
    style = resolve_style(voice)
    # Pitch is shifted by resampling, which also speeds speech up — so ask the engine to
    # speak proportionally slower and the result keeps the intended speed.
    speed = style.rate / (2 ** (style.pitch / 12))
    try:
        wav = _piper_wav(text, style, speed)
    except VoiceUnavailable:
        wav = _say_wav(text, style, speed)
    return apply_effects(wav, style.pitch, style.robot)
