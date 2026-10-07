"""Text-to-speech: voice presets on top of Piper (local neural voices) or the built-in
macOS voices, plus optional pitch and "robot" effects.

Presets (AMO_VOICE):
  amo             Obadiah, clean — AMO's voice (default)
  computer        Obadiah, slightly deeper with a metallic computer edge
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
    # Piper variation: lower = cleaner, steadier, less breathy (Piper defaults 0.667 / 0.8)
    noise: float | None = None
    noise_w: float | None = None


PRESETS: dict[str, VoiceStyle] = {
    # AMO's voice: Obadiah (Piper "semaine" voice), a deadpan British male, rendered clean —
    # no effects, reduced breathiness and wobble.
    "amo": VoiceStyle("en_GB-semaine-medium:obadiah", ("Daniel", "Oliver", "Arthur"),
                      personality="computer", noise=0.35, noise_w=0.5),
    "computer": VoiceStyle("en_GB-semaine-medium:obadiah", ("Daniel", "Oliver", "Arthur"),
                           pitch=-1.0, robot=0.32, personality="computer", noise=0.35, noise_w=0.5),
    "jarvis": VoiceStyle("en_GB-alan-medium", ("Daniel", "Oliver", "Arthur"),
                         rate=1.0, pitch=-0.5, robot=0.12, personality="jarvis"),
    "british-female": VoiceStyle("en_GB-jenny_dioco-medium", ("Kate", "Serena", "Stephanie", "Martha", "Daniel"),
                                 personality="jarvis"),
    "default": VoiceStyle("en_US-lessac-medium", ("Samantha", "Alex")),
}

_piper_cache: dict[str, Any] = {}
_lock = threading.Lock()


def clean_for_speech(text: str) -> str:
    """Make written text sound natural spoken (see speakable.py)."""
    from .speakable import to_speech

    return to_speech(text)


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


def active_personality() -> str:
    """AMO_PERSONALITY if set, otherwise the personality that goes with the voice preset."""
    return settings.personality or resolve_style().personality


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
        elif piper_installed():
            try:
                path = download_piper_voice(style.piper)  # first use: fetch the voice (~75 MB)
            except Exception as e:  # noqa: BLE001 — offline etc.: fall back to the Mac voice
                raise VoiceUnavailable(f"couldn't download Piper voice {style.piper}: {e}") from e
        else:
            raise VoiceUnavailable("Piper isn't installed")
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

                cfg = SynthesisConfig(length_scale=length_scale, speaker_id=sid,
                                      noise_scale=style.noise, noise_w_scale=style.noise_w)
                # Piper yields one chunk per sentence: add a natural pause between them.
                chunks = list(voice.synthesize(text, cfg))
                rate = chunks[0].sample_rate if chunks else voice.config.sample_rate
                gap = b"\x00\x00" * int(rate * settings.voice_sentence_pause)
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(gap.join(c.audio_int16_bytes for c in chunks))
            except ImportError:
                voice.synthesize_wav(text, wav)
        else:  # piper-tts 1.2
            voice.synthesize(text, wav, length_scale=length_scale, speaker_id=sid,
                             noise_scale=style.noise, noise_w=style.noise_w)
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
    return polish(apply_effects(wav, style.pitch, style.robot))


def polish(wav_bytes: bytes, lead: float = 0.18, tail: float = 0.12) -> bytes:
    """Make playback sound clean on any speaker:
    - a short silent lead-in, because Macs swallow the first moments of audio while the output
      wakes up (that's what clips the start of "Got it")
    - gentle fade in/out (no clicks), consistent loudness across phrases."""
    try:
        import numpy as np
    except ImportError:
        return wav_bytes
    with wave.open(io.BytesIO(wav_bytes)) as w:
        rate, width, ch = w.getframerate(), w.getsampwidth(), w.getnchannels()
        frames = w.readframes(w.getnframes())
    if width != 2 or not frames:
        return wav_bytes
    x = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    # trim silence the engine left at the ends, then add our own consistent padding
    loud = np.flatnonzero(np.abs(x) > 0.01)
    if len(loud):
        x = x[max(0, loud[0] - int(0.02 * rate)): loud[-1] + int(0.05 * rate)]
    fade_in, fade_out = int(0.008 * rate), int(0.03 * rate)
    if len(x) > fade_in + fade_out:
        x[:fade_in] *= np.linspace(0, 1, fade_in)
        x[-fade_out:] *= np.linspace(1, 0, fade_out)
    rms = float(np.sqrt(np.mean(x ** 2))) or 1.0
    x = x * min(settings.voice_loudness / rms, 0.95 / (float(np.max(np.abs(x))) or 1.0))
    x = np.concatenate([np.zeros(int(lead * rate), np.float32), x, np.zeros(int(tail * rate), np.float32)])
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
    return out.getvalue()
