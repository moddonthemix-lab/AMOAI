"""Hands-free mode: say "Hey AMO" and talk.

    amo listen            # run in a Terminal window (macOS asks for mic access once)
    amo listen -v         # also print everything it hears (for tuning)

How it works, cheaply enough for a CPU-only Mac or a Raspberry Pi:
1. A loudness gate (calibrated to the room) skips silence entirely.
2. Each burst of speech is checked by a *tiny* Whisper model for the wake phrase at the
   start ("Hey AMO …", "AMO, …"). Mid-sentence mentions don't count.
3. The command is transcribed with the regular Whisper model, answered, and spoken.
4. For a few seconds after answering, AMO listens for a follow-up without the wake phrase.

Built-in commands (instant, no AI model): "good morning" (morning brief), "what time is it",
"go to sleep" / "wake up", "stop" / "never mind".
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from ..config import settings

SAMPLE_RATE = 16000
BLOCK = 1600  # 100 ms

# What Whisper tends to hear when someone says "AMO". The first list is safe on its own
# ("AMO, …"); the second are real words too ("emo", "IMO"), so they need a greeting first.
NAME_VARIANTS = ["a m o", "amo", "ammo", "amoh", "amo's", "amoe", "aymo", "aimo", "eamo", "hamo"]
GREETING_ONLY_VARIANTS = ["a mo", "ah mo", "emo", "imo", "omo", "amu", "amon", "almo", "amore"]
GREETINGS = r"(?:hey|hi|hay|ay|a|ok|okay|yo|hello|oh)"

# Hint so Whisper spells the name the way we expect.
WAKE_PROMPT = "Hey AMO. AMO, what's on today?"


def _alternation(names: list[str]) -> str:
    names = sorted(set(names), key=len, reverse=True)
    return "(?:" + "|".join(re.escape(n).replace(r"\ ", r"\s*") for n in names) + ")"


def _name_pattern() -> str:
    extra = [w.strip().lower() for w in settings.wake_words.split(",") if w.strip()]
    strong = _alternation(NAME_VARIANTS + extra)
    weak = _alternation(NAME_VARIANTS + extra + GREETING_ONLY_VARIANTS)
    return rf"(?:{GREETINGS}\s+{weak}|{strong})"


def _normalize(text: str) -> str:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def match_wake(text: str) -> tuple[bool, str]:
    """Does the utterance start with the wake phrase? Returns (matched, rest of the sentence)."""
    norm = _normalize(text)
    m = re.match(rf"^{_name_pattern()}\b\s*(.*)$", norm)
    if not m:
        return False, ""
    return True, m.group(1).strip()


# ------------------------------------------------------------------ built-in commands
STOP_RE = re.compile(r"^(?:stop|cancel|never ?mind|nothing|forget it|that's all|no thanks?)\b")
SLEEP_RE = re.compile(r"\b(?:go to sleep|stop listening|sleep mode|mute yourself)\b")
WAKE_UP_RE = re.compile(r"\b(?:wake up|start listening|i'm back)\b")
MORNING_RE = re.compile(r"\b(?:good morning|morning brief|brief me|daily brief)\b")
TIME_RE = re.compile(r"\bwhat(?:'s| is) the time\b|\bwhat time is it\b")


def quick_command(text: str) -> str | None:
    """Return a key for commands handled without the AI model."""
    t = _normalize(text)
    if not t or STOP_RE.match(t):
        return "stop"
    if SLEEP_RE.search(t):
        return "sleep"
    if WAKE_UP_RE.search(t):
        return "wake"
    if MORNING_RE.search(t):
        return "morning"
    if TIME_RE.search(t):
        return "time"
    return None


def brief_for_speech(text: str) -> str:
    """The morning brief is line-based; make it flow as sentences."""
    lines = [ln.strip().rstrip(".") for ln in text.splitlines() if ln.strip()]
    if lines:
        lines[0] = f"Good morning. It's {lines[0]}"
    return ". ".join(lines) + "."


# ------------------------------------------------------------------ the listener
@dataclass
class Listener:
    """The wake-word loop. Audio I/O and models are injected so it can be tested."""

    segments: Callable[[float | None], Iterator[Any]]  # timeout → yields speech segments
    transcribe_wake: Callable[[Any], str]               # tiny model
    transcribe_command: Callable[[Any], str]            # regular model
    ask: Callable[[list[dict[str, str]]], str]          # history → reply text
    say: Callable[[str], None]
    chime: Callable[[], None] = lambda: None
    log: Callable[[str], None] = print
    verbose: bool = False
    follow_up_seconds: float = 7.0
    asleep: bool = False
    history: list[dict[str, str]] = field(default_factory=list)

    def next_utterance(self, timeout: float) -> Any | None:
        for seg in self.segments(timeout):
            return seg
        return None

    def handle(self, command: str) -> bool:
        """Act on one command. Returns True if the conversation should continue."""
        kind = quick_command(command)
        if kind == "stop":
            self.chime()
            return False
        if kind == "sleep":
            self.asleep = True
            self.say("Going quiet. Say hey AMO, wake up, when you need me.")
            return False
        if kind == "wake":
            self.asleep = False
            self.say("I'm here.")
            return True
        if kind == "time":
            from ..db import local_now

            now = local_now()
            hour = now.hour % 12 or 12
            self.say(f"It's {hour}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}.")
            return True
        if kind == "morning":
            from ..db import get_db
            from ..scheduler import morning_brief_text

            self.say(brief_for_speech(morning_brief_text(get_db())))
            return True

        self.history.append({"role": "user", "content": command})
        self.log("   … thinking")
        reply = self.ask(self.history[-10:])
        self.history.append({"role": "assistant", "content": reply})
        self.log(f"AMO: {reply}")
        self.say(reply)
        return True

    def converse(self, command: str) -> None:
        """Handle a command, then keep listening briefly for follow-ups (no wake phrase needed)."""
        while command:
            self.log(f"you: {command}")
            if not self.handle(command) or self.asleep:
                return
            self.log(f"   (follow up within {self.follow_up_seconds:g}s — no need to say Hey AMO)")
            seg = self.next_utterance(self.follow_up_seconds)
            if seg is None:
                self.log('Listening. Say "Hey AMO" …')
                return
            command = self.transcribe_command(seg)
            # Saying the wake phrase again in a follow-up is fine too.
            woke, rest = match_wake(command)
            if woke:
                command = rest

    def step(self, seg: Any) -> None:
        """Process one burst of speech heard while idle."""
        heard = self.transcribe_wake(seg)
        woke, rest = match_wake(heard)
        if not woke:
            if heard:
                self.log(f"   · heard “{heard}” — not for me")
            return
        self.log(f"✓ woke: “{heard}”")
        if self.asleep:
            if quick_command(rest) == "wake":
                self.asleep = False
                self.say("I'm here.")
            return
        if len(rest.split()) >= 2:
            # Wake phrase and request in one breath — re-read it with the better model.
            full = self.transcribe_command(seg)
            woke2, rest2 = match_wake(full)
            command = rest2 if woke2 and rest2 else rest
        else:
            self.chime()
            self.log("🎙  Yes? Say your request…")
            seg2 = self.next_utterance(8.0)
            if seg2 is None:
                self.log('   (didn\'t hear a request) Listening. Say "Hey AMO" …')
                return
            command = self.transcribe_command(seg2)
        self.converse(command)

    def run(self) -> None:
        self.log('Listening. Say "Hey AMO" … (Ctrl+C to quit)')
        for seg in self.segments(None):
            self.step(seg)


# ------------------------------------------------------------------ real audio
class MicSegmenter:
    """Splits microphone audio into utterances with a loudness gate calibrated to the room."""

    def __init__(self, sensitivity: float = 3.0, max_seconds: float = 12.0, silence: float = 0.8):
        import numpy as np
        import sounddevice as sd

        self.np, self.sd = np, sd
        self.sensitivity = sensitivity
        self.max_blocks = int(max_seconds * SAMPLE_RATE / BLOCK)
        self.silence_blocks = int(silence * SAMPLE_RATE / BLOCK)
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK)
        self.stream.start()
        self.noise_floor = 0.003
        self.calibrate()

    def _rms(self, block) -> float:
        return float(self.np.sqrt(self.np.mean(block**2)))

    def _read(self):
        block, _ = self.stream.read(BLOCK)
        return block[:, 0].copy()

    def calibrate(self, seconds: float = 1.5) -> None:
        levels = sorted(self._rms(self._read()) for _ in range(int(seconds * SAMPLE_RATE / BLOCK)))
        self.noise_floor = max(levels[len(levels) // 2], 0.001)

    @property
    def threshold(self) -> float:
        return max(self.noise_floor * self.sensitivity, 0.006)

    def flush(self) -> None:
        """Drop audio captured while AMO was talking (so it doesn't hear itself)."""
        avail = self.stream.read_available
        if avail:
            self.stream.read(avail)

    def __call__(self, timeout: float | None) -> Iterator[Any]:
        np = self.np
        deadline = time.monotonic() + timeout if timeout else None
        pre: list = []  # keep a little audio from just before speech starts
        while True:
            if deadline and time.monotonic() > deadline:
                return
            block = self._read()
            level = self._rms(block)
            if level < self.threshold:
                pre = (pre + [block])[-3:]
                # Track slow changes in room noise (fans, music in the next room).
                self.noise_floor = 0.995 * self.noise_floor + 0.005 * max(level, 0.001)
                continue
            chunks, quiet = pre + [block], 0
            while len(chunks) < self.max_blocks:
                b = self._read()
                chunks.append(b)
                quiet = quiet + 1 if self._rms(b) < self.threshold else 0
                if quiet >= self.silence_blocks:
                    break
            if len(chunks) - quiet - len(pre) < 3:  # under ~0.3s of sound: a click or bump
                continue
            yield np.concatenate(chunks)
            deadline = time.monotonic() + timeout if timeout else None


def chime_wav() -> bytes:
    """A short two-note 'I'm listening' sound."""
    import io
    import wave

    import numpy as np

    rate = 22050
    notes = []
    for freq in (660, 990):
        t = np.arange(int(rate * 0.09)) / rate
        env = np.minimum(1, np.minimum(t * 80, (t[-1] - t) * 80))
        notes.append(np.sin(2 * np.pi * freq * t) * env * 0.35)
    x = np.concatenate([notes[0], np.zeros(int(rate * 0.03)), notes[1]])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes((x * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def run(verbose: bool = False) -> None:
    from ..agent import Agent
    from .loop import play_wav
    from .stt import VoiceUnavailable, transcribe_array
    from .tts import synthesize

    try:
        mic = MicSegmenter(sensitivity=settings.wake_sensitivity)
    except ImportError as e:
        raise VoiceUnavailable('pip install -e ".[voice]" for microphone support') from e

    agent = Agent()
    chime = chime_wav()

    def say(text: str) -> None:
        if text:
            play_wav(synthesize(text))
        mic.flush()

    def ding() -> None:
        play_wav(chime)
        mic.flush()

    print("Loading speech models …")
    transcribe_array(mic.np.zeros(SAMPLE_RATE // 2, dtype="float32"), model=settings.wake_model)
    listener = Listener(
        segments=mic,
        transcribe_wake=lambda seg: transcribe_array(seg, model=settings.wake_model, prompt=WAKE_PROMPT),
        transcribe_command=lambda seg: transcribe_array(seg, prompt="AMO"),
        ask=lambda history: agent.chat(history, channel="voice")["content"],
        say=say,
        chime=ding,
        verbose=verbose,
    )
    if verbose:
        print(f"room noise {mic.noise_floor:.4f}, wake threshold {mic.threshold:.4f}")
    try:
        listener.run()
    except KeyboardInterrupt:
        print()
