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


# ------------------------------------------------------------------ barge-in ("AMO, stop")
_INTERRUPT = re.compile(r"\b(stop|amo|ammo|shut up|quiet|hold on|hang on|wait|never ?mind|cancel|enough|"
                        r"pause|okay okay|ok ok|got it)\b")


def barge_in(heard: str, being_said: str) -> tuple[bool, str]:
    """While AMO is talking: did the user interrupt? Returns (interrupted, what they asked instead).
    The mic also hears AMO itself, so anything that's mostly AMO's own words is ignored."""
    h = _normalize(heard)
    if not h:
        return False, ""
    said = set(_normalize(being_said).split())
    words = [w for w in h.split() if len(w) > 2]
    if words and sum(w in said for w in words) / len(words) >= 0.6:
        return False, ""  # echo of AMO's own voice
    woke, rest = match_wake(heard)
    if woke:
        return True, rest
    if _INTERRUPT.search(h):
        rest = _INTERRUPT.sub(" ", h, count=1).strip()
        return True, rest if len(rest.split()) >= 2 else ""
    return False, ""


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
    # Optional: speak-while-thinking. respond(history, command) → (reply, interruption or None).
    # interruption is what the user said when they cut AMO off ("" = just "stop").
    respond: Callable[[list[dict[str, str]], str], tuple[str, str | None]] | None = None
    brief: Callable[[], str] | None = None  # morning brief text (a device asks the brain for it)
    announcements: Callable[[], list[str]] | None = None  # things AMO wants to say on its own
    wake_stream: Callable[[float | None], Iterator[Any]] | None = None  # streaming wake engine (vosk/oww)
    poll_seconds: float = 3.0
    _interrupted: str | None = None
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
            if self.brief is not None:
                text = self.brief()
            else:
                from ..db import get_db
                from ..scheduler import morning_brief_text

                text = morning_brief_text(get_db())
            self.say(brief_for_speech(text))
            return True

        self.history.append({"role": "user", "content": command})
        self.log("   … thinking")
        if self.respond is not None:
            try:
                reply, self._interrupted = self.respond(self.history[-10:], command)
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
            if not self.handle(command) or self.asleep:
                return
            if self._interrupted is not None:  # cut off mid-answer
                command, self._interrupted = self._interrupted, None
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
        idle = self.wake_stream or self.segments  # what to listen with while waiting for "Hey AMO"
        if self.announcements is None:
            for seg in idle(None):
                self.step(seg)
            return
        while True:  # listen in short windows so AMO can speak up between them
            for seg in idle(self.poll_seconds):
                self.step(seg)
            self.speak_announcements()


# ------------------------------------------------------------------ real audio
class MicSegmenter:
    """Splits microphone audio into utterances with a loudness gate calibrated to the room."""

    def __init__(self, sensitivity: float = 2.0, max_seconds: float = 12.0, silence: float = 0.8,
                 meter: bool = False):
        import numpy as np
        import sounddevice as sd

        self.np, self.sd = np, sd
        self.sensitivity = sensitivity
        self.meter = meter
        self._ticks = 0
        self._zero_blocks = 0
        self._warned_silent = False
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
        return max(self.noise_floor * self.sensitivity, 0.004)

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
        saved = self.max_blocks
        self.max_blocks = int(max_seconds * SAMPLE_RATE / BLOCK)
        try:
            for seg in self(timeout):
                return seg
            return None
        finally:
            self.max_blocks = saved

    def __call__(self, timeout: float | None) -> Iterator[Any]:
        np = self.np
        deadline = time.monotonic() + timeout if timeout else None
        pre: list = []  # keep a little audio from just before speech starts
        while True:
            if deadline and time.monotonic() > deadline:
                return
            block = self._read()
            level = self._rms(block)
            self._watch(level)
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
    from ..proactive import take_pending
    from . import acks
    from .loop import play_wav
    from .stt import VoiceUnavailable, transcribe_array
    from .tts import synthesize

    try:
        mic = MicSegmenter(sensitivity=settings.wake_sensitivity, meter=verbose)
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

    def respond(history: list[dict[str, str]], command: str) -> tuple[str, str | None]:
        """Speak while thinking: "Got it" right away, then each sentence as soon as it's written.
        Keeps an ear open while talking so you can cut AMO off ("AMO, stop")."""
        speaker = SentenceSpeaker(synth=synthesize)
        phrase = acks.pick(command)
        if phrase:
            try:
                speaker.say_audio(acks.audio(phrase), phrase)
            except Exception:  # noqa: BLE001 — acks are optional
                pass
        result: dict[str, Any] = {}

        def work() -> None:
            try:
                result["reply"] = agent.chat(history, channel="voice", on_text=speaker.feed)["content"]
            except Exception as e:  # noqa: BLE001
                result["error"] = e
                print(f"   ✗ {e}")
                speaker.feed("Sorry, I couldn't do that. Check the AMO window for details. ")
            finally:
                speaker.finish()

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        interrupt: str | None = None
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
                    hit, rest = barge_in(heard, " ".join(speaker.spoken))
                    if hit:
                        speaker.stop()
                        interrupt = rest
                        print(f"   ✋ heard “{heard}” — stopping")
                        break
        mic.flush()
        return result.get("reply", ""), interrupt

    threading.Thread(target=acks.prewarm, daemon=True).start()

    from .wake import StreamingWake, WakeEvent, make_detector

    print("Loading speech models …")
    detector = make_detector()
    if detector is None:
        transcribe_array(mic.np.zeros(SAMPLE_RATE // 2, dtype="float32"), model=settings.wake_model)
    print(f"Wake word engine: {settings.wake_engine}")

    def wake_text(seg) -> str:
        if isinstance(seg, WakeEvent):  # the engine already heard "Hey AMO"
            return "Hey AMO " + (transcribe_array(seg.audio, prompt="AMO") if seg.audio is not None else "")
        return transcribe_array(seg, model=settings.wake_model, prompt=WAKE_PROMPT)

    def command_text(seg) -> str:
        if isinstance(seg, WakeEvent):
            return "Hey AMO " + (transcribe_array(seg.audio, prompt="AMO") if seg.audio is not None else "")
        return transcribe_array(seg, prompt="AMO")

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
        verbose=verbose,
    )
    if verbose:
        print(f"room noise {mic.noise_floor:.4f}, wake threshold {mic.threshold:.4f}")
        print("The meter shows your mic live: talk and the bar should cross the | line.")
    try:
        listener.run()
    except KeyboardInterrupt:
        print()
