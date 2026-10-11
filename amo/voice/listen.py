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
import threading
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


# ------------------------------------------------------------------ interrupting AMO
# Only deliberate phrases at the START of what you say cut AMO off — never a word mid-sentence.
_STOP = re.compile(r"^(?:(?:hey )?(?:amo|ammo)\b[ ,]*)?(?:stop|wait|hold on|hang on|hold up|pause|cancel|"
                   r"never ?mind|shut up|be quiet|quiet|enough)\b[ ,.!]*")
_NO = re.compile(r"^(?:no+|nope|nah)\b[ ,.!?-]*", re.I)


def barge_in(heard: str, being_said: str) -> tuple[bool, str, bool]:
    """While AMO is talking: did the user interrupt?
    Returns (interrupted, what they want instead, is_correction).
    is_correction: AMO misheard or misunderstood ("no, I said Thursday", "you misheard me").
    The mic also hears AMO itself, so anything that's mostly AMO's own words is ignored."""
    from ..fastpath import correction

    h = _normalize(heard)
    if not h:
        return False, "", False
    said = set(_normalize(being_said).split())
    words = [w for w in h.split() if len(w) > 2]
    if all(w in said for w in h.split()) or (words and sum(w in said for w in words) / len(words) >= 0.6):
        return False, "", False  # echo of AMO's own voice
    from ..pronounce import PATTERNS

    if any(rx.match(heard.strip().rstrip(".!")) for rx in PATTERNS):
        return True, heard.strip(), False  # "No, it's pronounced kah-reem" — a lesson, not a mishearing
    meant = correction(heard.strip())
    if meant is not None:
        return True, meant, True
    if _NO.match(heard.strip()):  # "No, Thursday" / "no no no"
        rest = heard.strip()
        while (m := _NO.match(rest)):
            rest = rest[m.end():]
        return True, rest.strip(" ,.!?"), True
    m = _STOP.match(h)
    if m:
        rest = h[m.end():].strip()
        return True, rest if len(rest.split()) >= 2 else "", False
    woke, rest = match_wake(heard)
    if woke:
        return True, rest, False
    return False, "", False


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
    ack: Callable[[str], None] = lambda request: None  # "Got it" while the model works
    wait: Callable[[Any], None] = lambda worker: worker.join()  # + "Still working on it." on long waits
    # Optional: speak-while-thinking. respond(history, command) → (reply, interruption or None[, correction]).
    # interruption is what the user said when they cut AMO off ("" = just "stop"); correction=True
    # when they cut in because AMO misheard ("no, I said Thursday").
    respond: Callable[[list[dict[str, str]], str], tuple] | None = None
    brief: Callable[[], str] | None = None  # morning brief text (a device asks the brain for it)
    announcements: Callable[[], list[str]] | None = None  # things AMO wants to say on its own
    wake_stream: Callable[[float | None], Iterator[Any]] | None = None  # streaming wake engine (vosk/oww)
    on_state: Callable[[str], None] = lambda state: None  # idle | listening | thinking | asleep (the face)
    on_heard: Callable[[str], None] = lambda text: None  # what AMO understood you said (the face shows it)
    poll_seconds: float = 3.0
    _interrupted: str | None = None
    _correction: bool = False
    log: Callable[[str], None] = print
    verbose: bool = False
    follow_up_seconds: float = 7.0
    asleep: bool = False
    history: list[dict[str, str]] = field(default_factory=list)

    def next_utterance(self, timeout: float) -> Any | None:
        spec = getattr(self.segments, "speculator", None)
        if spec is not None:
            spec.on = True  # transcribe while you talk — only for requests, not idle chatter
        try:
            for seg in self.segments(timeout):
                return seg
            return None
        finally:
            if spec is not None:
                spec.on = False

    def handle(self, command: str) -> bool:
        """Act on one command. Returns True if the conversation should continue."""
        kind = quick_command(command)
        if kind == "stop":
            self.chime()
            return False
        if kind == "sleep":
            self.asleep = True
            self.on_state("asleep")
            self.say("Going quiet. Say hey AMO, wake up, when you need me.")
            return False
        if kind == "wake":
            self.asleep = False
            self.on_state("idle")
            self.say("I'm here.")
            return True
        if kind == "time":
            from ..db import local_now

            now = local_now()
            hour = now.hour % 12 or 12
            self.say(f"It's {hour}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}.")
            return True
        if kind == "morning":
            if self.brief is not None:
                text = self.brief()
            else:
                from ..briefing import today
                from ..db import get_db

                text = today(get_db())["text"]
            self.say(text)  # already written to be spoken (and usually already rendered)
            return True

        self.history.append({"role": "user", "content": command})
        self.log("   … thinking")
        self.on_state("thinking")
        if self.respond is not None:
            try:
                out = self.respond(self.history[-10:], command)
                reply, self._interrupted = out[0], out[1]
                self._correction = bool(out[2]) if len(out) > 2 else False
            except Exception as e:  # noqa: BLE001 — report instead of killing the listener
                self.log(f"   ✗ {e}")
                self.say("Sorry, I couldn't do that. Check the AMO window for details.")
                return False
            self.history.append({"role": "assistant", "content": reply})
            self.log(f"AMO: {reply}" + ("   ✋ (interrupted)" if self._interrupted is not None else ""))
            return True
        # Ask the model in the background and say "Got it" / "Let me think" meanwhile.
        result: dict[str, Any] = {}

        def work() -> None:
            try:
                result["reply"] = self.ask(self.history[-10:])
            except Exception as e:  # noqa: BLE001 — report instead of killing the listener
                result["error"] = e

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        self.ack(command)
        self.wait(worker)
        if "error" in result:
            self.log(f"   ✗ {result['error']}")
            self.say("Sorry, I couldn't do that. Check the AMO window for details.")
            return False
        reply = result["reply"]
        self.history.append({"role": "assistant", "content": reply})
        self.log(f"AMO: {reply}")
        self.say(reply)
        return True

    def converse(self, command: str) -> None:
        """Handle a command, then keep listening briefly for follow-ups (no wake phrase needed)."""
        while command:
            self.log(f"you: {command}")
            self.on_heard(command)
            if not self.handle(command) or self.asleep:
                if not self.asleep:
                    self.on_state("idle")
                return
            self.on_state("listening")
            if self._interrupted is not None:  # cut off mid-answer
                command, self._interrupted = self._interrupted, None
                corrected, self._correction = self._correction, False
                if self.history and self.history[-1]["role"] == "assistant":
                    self.history[-1]["content"] += " [cut off — the user interrupted]"
                if corrected:
                    # Misheard: the brain undoes anything it just saved from the misheard request,
                    # then does what you meant (or asks "what did you say?").
                    command = f"I meant {command}" if command else "you misheard me"
                    continue
                if command:
                    continue  # "AMO, stop — what's my schedule?" → answer the new question
                self.chime()
                self.log("🎙  Yes?")
                seg = self.next_utterance(8.0)
                if seg is None:
                    return
                command = self.transcribe_command(seg)
                continue
            self.log(f"   (follow up within {self.follow_up_seconds:g}s — no need to say Hey AMO)")
            seg = self.next_utterance(self.follow_up_seconds)
            if seg is None:
                self.log('Listening. Say "Hey AMO" …')
                self.on_state("idle")
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
                self.on_state("idle")
                self.say("I'm here.")
            return
        if len(rest.split()) >= 2:
            # Wake phrase and request in one breath — re-read it with the better model.
            full = self.transcribe_command(seg)
            woke2, rest2 = match_wake(full)
            command = rest2 if woke2 and rest2 else rest
        else:
            self.chime()
            self.on_state("listening")
            self.log("🎙  Yes? Say your request…")
            seg2 = self.next_utterance(8.0)
            if seg2 is None:
                self.log('   (didn\'t hear a request) Listening. Say "Hey AMO" …')
                self.on_state("idle")
                return
            command = self.transcribe_command(seg2)
        self.converse(command)

    def speak_announcements(self) -> None:
        """Say anything AMO queued up on its own (alerts, reminders, brief) — not while asleep."""
        if self.asleep or self.announcements is None:
            return
        try:
            items = self.announcements()
        except Exception as e:  # noqa: BLE001 — never let this kill the listener
            self.log(f"   (couldn't check announcements: {e})")
            return
        for text in items:
            self.chime()
            self.log(f"📣 AMO: {text}")
            self.say(text)

    def run(self) -> None:
        self.log('Listening. Say "Hey AMO" … (Ctrl+C to quit)')
        self.on_state("idle")
        idle = self.wake_stream or self.segments  # what to listen with while waiting for "Hey AMO"
        if self.announcements is None:
            for seg in idle(None):
                self.safe_step(seg)
            return
        while True:  # listen in short windows so AMO can speak up between them
            for seg in idle(self.poll_seconds):
                self.safe_step(seg)
            self.speak_announcements()

    def safe_step(self, seg: Any) -> None:
        """One request; a lost connection to the brain (Wi-Fi drop, Mac asleep) never ends listening."""
        try:
            self.step(seg)
        except Exception as e:  # noqa: BLE001
            self.log(f"   (that didn't work: {e}) Still listening.")
            self.on_state("idle")
            try:
                self.say("Sorry, I couldn't reach my brain just now." if "connect" in str(e).lower()
                         or "timed out" in str(e).lower() else "Sorry, something went wrong.")
            except Exception:  # noqa: BLE001 — the speaker may be what failed
                pass


# ------------------------------------------------------------------ real audio
class Endpointer:
    """Decides when you start and stop talking, from 50 ms loudness frames.

    - Room noise = the QUIETEST moment of the last 3 seconds (gaps between words keep it honest), so
      it follows a fan, music or the mic's auto-volume without mistaking your voice for noise.
    - Speech starts after 0.15 s above noise × sensitivity; it ends after `end_silence` seconds with
      no speech-level frame at all (not "quiet enough" frames — after you stop, a mic with auto-gain
      pumps the noise up, which used to keep AMO waiting until the 12-second limit).
    - Under 0.3 s of actual speech (a click, a cough) is ignored, so Whisper never gets fed noise.
    """

    FRAME = SAMPLE_RATE // 20  # 50 ms

    def __init__(self, sensitivity: float = 2.2, end_silence: float = 0.9, max_seconds: float = 15.0,
                 min_level: float = 0.004):
        from collections import deque

        self.sensitivity, self.min_level = sensitivity, min_level
        self.end_frames = max(1, int(end_silence * 20))
        self.max_frames = int(max_seconds * 20)
        self.history: Any = deque(maxlen=60)  # 3 s of frame levels
        self.start_floor = 0.0
        self.reset()

    def reset(self) -> None:
        self.active, self.frames, self.run, self.speech, self.since_speech = False, 0, 0, 0, 0
        self.peak = 0.0

    @property
    def floor(self) -> float:
        return max(min(self.history), 0.0005) if self.history else 0.003

    @property
    def threshold(self) -> float:
        floor = self.floor
        if self.active:  # during a long, gapless sentence the 3 s minimum creeps up — don't let it run away
            floor = min(floor, self.start_floor * 4)
        return max(self.min_level, floor * self.sensitivity)

    def feed(self, level: float) -> str:
        """One frame's loudness → "idle" | "start" | "speech" | "end" | "noise" (too short to be speech)."""
        self.history.append(level)
        loud = level >= self.threshold
        if self.active:
            # Also relative to how loud YOU are: a voice peaks well above any noise the mic's
            # auto-gain pumps up after you stop, so that noise can't keep the utterance going.
            self.peak = max(level, self.peak * 0.995)  # follows your voice (~10 s memory)
            loud = loud and level >= self.peak * 0.22
        if not self.active:
            self.run = self.run + 1 if loud else 0
            if self.run >= 3:
                self.active, self.frames, self.speech, self.since_speech = True, self.run, self.run, 0
                self.start_floor = self.floor
                self.peak = level
                return "start"
            return "idle"
        self.frames += 1
        if loud:
            self.speech += 1
            self.since_speech = 0
        else:
            self.since_speech += 1
        if self.since_speech >= self.end_frames or self.frames >= self.max_frames:
            enough = self.speech >= 6
            self.reset()
            return "end" if enough else "noise"
        return "speech"


class Speculator:
    """Transcribe while you talk: at a pause mid-utterance (≥ 0.35 s quiet after ≥ 1.2 s of talking)
    the audio so far is sent to Whisper in the background. If the pause turns out to be the end of
    what you said, the text is already done (or nearly) when the end-of-speech wait finishes — no
    waiting for Whisper afterwards. If you keep talking, that guess is dropped."""

    PAUSE_FRAMES = 7    # 350 ms of quiet
    MIN_FRAMES = 24     # after at least 1.2 s of utterance

    def __init__(self, transcribe: Callable[[Any], Any] | None = None):
        from concurrent.futures import ThreadPoolExecutor

        self.transcribe = transcribe
        self.on = False  # only while AMO is waiting for a request (not while idle, listening for "Hey AMO")
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="amo-stt") if transcribe else None
        self.reset()

    def reset(self) -> None:
        self.future: Any = None
        self.covered = 0     # frames the current guess covers
        self.valid = False

    def frame(self, state: str, since_speech: int, n_frames: int, audio: Callable[[], Any]) -> None:
        """Call after every frame of an active utterance."""
        if not self.pool or not self.on:
            return
        if since_speech == 0 and self.covered and n_frames > self.covered:
            self.valid = False  # you kept talking after the guess
        if (state == "speech" and since_speech == self.PAUSE_FRAMES and n_frames >= self.MIN_FRAMES
                and not (self.valid and self.covered == n_frames)):
            self.future = self.pool.submit(self.transcribe, audio())
            self.covered, self.valid = n_frames, True

    def take(self) -> Any:
        """At the end of the utterance: the background transcription if it still applies, else None."""
        fut = self.future if self.valid else None
        self.reset()
        return fut


class MicSegmenter:
    """Splits microphone audio into utterances (see Endpointer for how it decides)."""

    def __init__(self, sensitivity: float = 2.0, max_seconds: float = 15.0, silence: float | None = None,
                 meter: bool = False):
        import numpy as np
        import sounddevice as sd

        self.np, self.sd = np, sd
        self.meter = meter
        self._ticks = 0
        self._zero_blocks = 0
        self._warned_silent = False
        self.end = Endpointer(sensitivity=max(sensitivity, 1.5),
                              end_silence=settings.end_silence if silence is None else silence,
                              max_seconds=max_seconds)
        self.speculator = Speculator()  # run() gives it a transcriber (transcribe while you talk)
        self.guess: Any = None          # the finished utterance's background transcription, if any
        self.stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK)
        self.stream.start()
        self.calibrate()

    def _rms(self, block) -> float:
        return float(self.np.sqrt(self.np.mean(block**2)))

    def _read(self):
        block, _ = self.stream.read(BLOCK)
        return block[:, 0].copy()

    def calibrate(self, seconds: float = 1.5) -> None:
        for _ in range(int(seconds * SAMPLE_RATE / BLOCK)):
            for frame in self._frames(self._read()):
                self.end.history.append(self._rms(frame))

    def _frames(self, block):
        f = Endpointer.FRAME
        return [block[i:i + f] for i in range(0, len(block) - f + 1, f)]

    @property
    def noise_floor(self) -> float:
        return self.end.floor

    @property
    def threshold(self) -> float:
        return self.end.threshold

    def flush(self) -> None:
        """Drop audio captured while AMO was talking (so it doesn't hear itself)."""
        avail = self.stream.read_available
        if avail:
            self.stream.read(avail)

    def _watch(self, level: float) -> None:
        """Spot a muted/blocked mic, and draw a live level meter in verbose mode."""
        self._zero_blocks = self._zero_blocks + 1 if level == 0.0 else 0
        if self._zero_blocks >= 40 and not self._warned_silent:
            self._warned_silent = True
            print("\n⚠  The microphone is sending pure silence — macOS is blocking it for Terminal.\n"
                  "   Fix: System Settings → Privacy & Security → Microphone → turn on Terminal,\n"
                  "   then quit Terminal completely (⌘Q) and start AMO Listen again.")
        if self.meter:
            self._ticks += 1
            if self._ticks % 3 == 0:
                bars = min(20, int(level / max(self.threshold, 1e-6) * 10))
                mark = "█" * bars + "·" * (20 - bars)
                hit = "◀ HEARING YOU" if level >= self.threshold else ""
                print(f"\r   mic {mark[:10]}|{mark[10:]} {level:.4f} (wakes above {self.threshold:.4f}) {hit:<14}",
                      end="", flush=True)

    def listen_once(self, timeout: float, max_seconds: float = 3.0) -> Any | None:
        """One short utterance (used to catch interruptions while AMO talks), or None."""
        saved = self.end.max_frames
        self.end.max_frames = int(max_seconds * 20)
        try:
            for seg in self(timeout):
                return seg
            return None
        finally:
            self.end.max_frames = saved

    def __call__(self, timeout: float | None) -> Iterator[Any]:
        np = self.np
        deadline = time.monotonic() + timeout if timeout else None
        pre: list = []  # 300 ms from just before speech starts, so the first word isn't clipped
        chunks: list = []
        self.end.reset()
        while True:
            if deadline and not self.end.active and time.monotonic() > deadline:
                return
            block = self._read()
            self._watch(self._rms(block))
            for frame in self._frames(block):
                state = self.end.feed(self._rms(frame))
                if state == "idle":
                    pre = (pre + [frame])[-6:]
                elif state == "start":
                    chunks = pre + [frame]
                    pre = []
                    self.speculator.reset()
                elif state == "speech":
                    chunks.append(frame)
                    self.speculator.frame(state, self.end.since_speech, len(chunks),
                                          lambda c=list(chunks): np.concatenate(c))
                elif state in ("end", "noise"):
                    guess = self.speculator.take()
                    if state == "end":
                        self.guess = guess
                        yield np.concatenate(chunks)
                        self.guess = None
                        deadline = time.monotonic() + timeout if timeout else None
                    chunks = []


_vocab: dict[str, Any] = {"at": 0.0, "text": "Hey AMO."}


def vocab() -> str:
    """Whisper's hint: your clients' names and tickers, so it spells them right (refreshed every 10 min)."""
    if time.monotonic() - _vocab["at"] > 600:
        try:
            from ..profile import whisper_prompt

            _vocab["text"] = whisper_prompt()
        except Exception:  # noqa: BLE001
            pass
        _vocab["at"] = time.monotonic()
    return _vocab["text"]


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
    from ..proactive import take_pending
    from . import acks
    from .loop import play_wav
    from .stt import VoiceUnavailable, transcribe_array
    from .tts import synthesize

    try:
        mic = MicSegmenter(sensitivity=settings.wake_sensitivity, meter=verbose)
        mic.speculator = Speculator(lambda audio: transcribe_array(audio, prompt=vocab()))
    except ImportError as e:
        raise VoiceUnavailable('pip install -e ".[voice]" for microphone support') from e

    agent = Agent()
    chime = chime_wav()
    from ..selfrepair import install as record_crashes

    record_crashes()

    from .. import face

    def say(text: str) -> None:
        if text:
            wav = synthesize(text)
            face.publish_say(text, wav)
            play_wav(wav)
        mic.flush()

    def ding() -> None:
        play_wav(chime)
        mic.flush()

    def ack(request: str) -> None:
        phrase = acks.pick(request)
        if phrase:
            try:
                play_wav(acks.audio(phrase))
            except Exception:  # noqa: BLE001 — acks are optional
                pass

    def wait(worker) -> None:
        def speak_line(line: str) -> None:
            try:
                play_wav(acks.audio(line))
            except Exception:  # noqa: BLE001
                pass
        acks.wait_with_updates(worker, speak_line)

    from .speaker import SentenceSpeaker

    stt_last: dict[str, Any] = {}  # how long the last request took to transcribe (speed report)

    def respond(history: list[dict[str, str]], command: str) -> tuple[str, str | None, bool]:
        """Speak while thinking: "Got it" right away, then each sentence as soon as it's written.
        Keeps an ear open while talking so you can cut AMO off ("AMO, stop", "no, I said Thursday")."""
        from ..perf import Turn

        # timed from when you stopped talking (speech-to-text included) to AMO's first real sentence
        heard = stt_last if stt_last.get("text") == command else {}
        turn = Turn("voice", command, started=heard.get("started"))
        if heard:
            turn.add("stt", heard["ms"])
        phrase = acks.pick(command)

        def played(text: str, wav: bytes) -> None:
            if text != phrase:
                turn.mark("first_audio")
            face.publish_say(text, wav)

        def timed_synth(text: str) -> bytes:
            if "first_audio" in turn.marks or turn.sums.get("tts"):
                return synthesize(text)
            with turn.timer("tts"):
                return synthesize(text)

        speaker = SentenceSpeaker(synth=timed_synth, on_play=played)
        if phrase:
            try:
                speaker.say_audio(acks.audio(phrase), phrase)
            except Exception:  # noqa: BLE001 — acks are optional
                pass
        result: dict[str, Any] = {}

        def work() -> None:
            try:
                result["reply"] = agent.chat(history, channel="voice", on_text=speaker.feed, turn=turn)["content"]
            except Exception as e:  # noqa: BLE001
                result["error"] = e
                print(f"   ✗ {e}")
                speaker.feed("Sorry, I couldn't do that. Check the AMO window for details. ")
            finally:
                speaker.finish()

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        interrupt: str | None = None
        corrected = False
        still_n, next_still = 0, time.monotonic() + settings.ack_still_after
        flushed = False
        while not speaker.wait(0.05):
            now = time.monotonic()
            if worker.is_alive() and speaker.idle and now >= next_still:  # long wait, nothing to say yet
                line = acks.still(still_n)
                if line:
                    try:
                        speaker.say_audio(acks.audio(line), line)
                    except Exception:  # noqa: BLE001
                        pass
                    still_n += 1
                next_still = now + settings.ack_still_after * 2
            if settings.barge_in and speaker.started.is_set():
                if not flushed:  # drop what the mic picked up while AMO was thinking
                    mic.flush()
                    flushed = True
                seg = mic.listen_once(timeout=0.4, max_seconds=3.0)
                if seg is not None:
                    heard = transcribe_array(seg, model=settings.wake_model, prompt=WAKE_PROMPT)
                    hit, rest, corrected = barge_in(heard, " ".join(speaker.spoken))
                    if hit:
                        speaker.stop()
                        print(f"   ✋ heard “{heard}” — stopping")
                        if rest:  # re-read what you said with the better model
                            better = transcribe_array(seg, prompt=vocab())
                            hit2, rest2, corr2 = barge_in(better, "")
                            rest, corrected = (rest2, corr2) if hit2 else (better, corrected)
                        interrupt = rest
                        break
        if corrected:  # let the misheard request finish saving, so it can be undone cleanly
            worker.join(timeout=90)
        mic.flush()
        turn.info["interrupted"] = interrupt is not None
        turn.save(model=settings.chat_model)
        return result.get("reply", ""), interrupt, corrected

    threading.Thread(target=acks.prewarm, daemon=True).start()
    # Load the command model now too (the wake model loads below), so the first request isn't slow.
    threading.Thread(target=lambda: transcribe_array(mic.np.zeros(SAMPLE_RATE // 2, dtype="float32"),
                                                     prompt=vocab()), daemon=True).start()

    from .wake import StreamingWake, WakeEvent, make_detector

    print("Loading speech models …")
    detector = make_detector()
    if detector is None:
        transcribe_array(mic.np.zeros(SAMPLE_RATE // 2, dtype="float32"), model=settings.wake_model)
    print(f"Wake word engine: {settings.wake_engine}")

    def wake_text(seg) -> str:
        if isinstance(seg, WakeEvent):  # the engine already heard "Hey AMO"
            return "Hey AMO " + (transcribe_array(seg.audio, prompt=vocab()) if seg.audio is not None else "")
        return transcribe_array(seg, model=settings.wake_model, prompt=WAKE_PROMPT)

    def command_text(seg) -> str:
        started = time.monotonic()
        if isinstance(seg, WakeEvent):
            text = "Hey AMO " + (transcribe_array(seg.audio, prompt=vocab()) if seg.audio is not None else "")
        elif mic.guess is not None:  # transcribed while you were still talking
            try:
                text = mic.guess.result(timeout=30)
            except Exception:  # noqa: BLE001
                text = transcribe_array(seg, prompt=vocab())
            mic.guess = None
        else:
            text = transcribe_array(seg, prompt=vocab())
        woke, rest = match_wake(text)
        stt_last.update(text=rest if woke and rest else text, started=started,
                        ms=(time.monotonic() - started) * 1000)
        return text

    listener = Listener(
        segments=mic,
        wake_stream=StreamingWake(mic, detector) if detector else None,
        transcribe_wake=wake_text,
        transcribe_command=command_text,
        ask=lambda history: agent.chat(history, channel="voice")["content"],
        say=say,
        chime=ding,
        ack=ack,
        wait=wait,
        respond=respond if settings.stream_speech else None,
        announcements=take_pending,
        on_state=face.publish_state,
        on_heard=face.publish_heard,
        verbose=verbose,
    )
    if verbose:
        print(f"room noise {mic.noise_floor:.4f}, wake threshold {mic.threshold:.4f}")
        print("The meter shows your mic live: talk and the bar should cross the | line.")
    try:
        listener.run()
    except KeyboardInterrupt:
        print()
