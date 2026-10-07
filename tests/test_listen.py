import pytest

from amo.voice.listen import Listener, brief_for_speech, match_wake, quick_command


@pytest.mark.parametrize("heard, rest", [
    ("Hey AMO, what's on my schedule today?", "what's on my schedule today"),
    ("Hey, Ammo. Book Jay for Friday.", "book jay for friday"),
    ("AMO what time is it", "what time is it"),
    ("Okay Amo.", ""),
    ("Hey a mo, good morning", "good morning"),
    ("Hey emo, book a session", "book a session"),
    ("Hi Amon.", ""),
    ("Hey A.M.O., brief me", "brief me"),
])
def test_wake_phrase_matches(heard, rest):
    assert match_wake(heard) == (True, rest)


@pytest.mark.parametrize("heard", [
    "I need more ammo for the game", "the demo sounds good", "hey what's up", "", "Amazing track",
    "IMO that beat is fire", "Emo music is back", "a moment please",
])
def test_no_false_wake(heard):
    assert match_wake(heard)[0] is False


def test_quick_commands():
    assert quick_command("never mind") == "stop"
    assert quick_command("go to sleep") == "sleep"
    assert quick_command("wake up") == "wake"
    assert quick_command("good morning") == "morning"
    assert quick_command("what time is it?") == "time"
    assert quick_command("book jay friday") is None
    assert brief_for_speech("Wednesday 2026-10-07\nStudio: no sessions booked today") == \
        "Good morning. It's Wednesday 2026-10-07. Studio: no sessions booked today."


def make_listener(script, transcripts, asked):
    """script: list of utterances the mic 'hears' (None = silence/timeout)."""
    said = []
    queue = list(script)

    def segments(timeout):
        while queue:
            seg = queue.pop(0)
            if seg is None:
                return
            yield seg

    lst = Listener(
        segments=segments,
        transcribe_wake=lambda s: transcripts[s],
        transcribe_command=lambda s: transcripts[s],
        ask=lambda h: asked.append(h[-1]["content"]) or f"answer to {h[-1]['content']}",
        say=said.append,
        log=lambda *_: None,
    )
    return lst, said


def test_wake_and_command_in_one_breath_then_follow_up():
    asked = []
    t = {"a": "Hey AMO, what's on today?", "b": "and tomorrow?", "c": "random chatter"}
    lst, said = make_listener(["a", "b", None, "c"], t, asked)
    lst.run()
    assert asked == ["what's on today", "and tomorrow?"]  # follow-up needed no wake phrase
    assert said == ["answer to what's on today", "answer to and tomorrow?"]  # chatter ignored


def test_wake_alone_then_chime_then_command(db):
    asked, chimes = [], []
    t = {"a": "Hey AMO.", "b": "what time is it"}
    lst, said = make_listener(["a", "b", None], t, asked)
    lst.chime = lambda: chimes.append(1)
    lst.run()
    assert chimes == [1] and asked == [] and said[0].startswith("It's ")


def test_sleep_and_wake():
    asked = []
    t = {"a": "Hey AMO go to sleep", "b": "Hey AMO what's up", "c": "Hey AMO wake up", "d": "Hey AMO hi there"}
    lst, said = make_listener(["a", "b", "c", "d", None], t, asked)
    lst.run()
    assert asked == ["hi there"]  # "what's up" was ignored while asleep
    assert said[0].startswith("Going quiet") and said[1] == "I'm here."


def test_mic_segmenter_splits_speech_from_silence(monkeypatch):
    import sys
    import types

    import numpy as np

    from amo.voice import listen

    rng = np.random.default_rng(0)
    quiet = lambda n: [rng.normal(0, 0.002, listen.BLOCK).astype("float32") for _ in range(n)]  # noqa: E731
    loud = lambda n: [rng.normal(0, 0.1, listen.BLOCK).astype("float32") for _ in range(n)]  # noqa: E731
    # calibration (15 blocks) · silence · 1.5s speech · silence · 0.1s tap · silence
    blocks = quiet(15) + quiet(10) + loud(15) + quiet(12) + loud(1) + quiet(12)

    class FakeStream:
        read_available = 0

        def __init__(self, **kw):
            pass

        def start(self):
            pass

        def read(self, n):
            if not blocks:
                raise StopIteration
            return blocks.pop(0).reshape(-1, 1), False

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(InputStream=FakeStream))
    mic = listen.MicSegmenter(sensitivity=3)
    segs = []
    try:
        for seg in mic(None):
            segs.append(seg)
    except (StopIteration, RuntimeError):
        pass
    assert len(segs) == 1  # the tap is ignored
    assert 1.5 <= len(segs[0]) / listen.SAMPLE_RATE <= 2.6


def test_ack_phrases_fit_the_request(monkeypatch):
    from amo.config import settings
    from amo.voice import acks

    assert acks.kind_of("what's on my schedule today") == "think"
    assert acks.kind_of("how much did I make this month?") == "think"
    assert acks.kind_of("book Jay Friday at 7") == "action"
    assert acks.kind_of("I sold the Jordans for 320") == "action"
    assert acks.pick("book jay friday") in acks.phrases("action")
    picks = [acks.pick("what's up today?") for _ in range(20)]
    assert all(a != b for a, b in zip(picks, picks[1:]))  # never the same twice in a row
    monkeypatch.setattr(settings, "acks", False)
    assert acks.pick("book jay") is None


def test_ack_plays_while_model_works():
    import threading
    import time

    events = []
    started = threading.Event()

    def slow_ask(history):
        started.set()
        time.sleep(0.2)
        events.append("answer ready")
        return "Booked."

    def ack(request):
        assert started.wait(1), "model should already be working when the ack plays"
        events.append(f"ack for {request}")

    lst = Listener(segments=lambda t: iter(()), transcribe_wake=str, transcribe_command=str,
                   ask=slow_ask, say=lambda t: events.append(f"say {t}"), ack=ack, log=lambda *_: None)
    lst.handle("book jay friday")
    assert events == ["ack for book jay friday", "answer ready", "say Booked."]


def test_listener_survives_model_errors():
    said = []

    def boom(history):
        raise RuntimeError("Ollama isn't running")

    lst = Listener(segments=lambda t: iter(()), transcribe_wake=str, transcribe_command=str,
                   ask=boom, say=said.append, log=lambda *_: None)
    assert lst.handle("book jay") is False
    assert said and said[0].startswith("Sorry")


def test_blocked_mic_warns(monkeypatch, capsys):
    import sys
    import types

    import numpy as np

    from amo.voice import listen

    blocks = [np.zeros(listen.BLOCK, dtype="float32") for _ in range(70)]

    class FakeStream:
        read_available = 0

        def __init__(self, **kw):
            pass

        def start(self):
            pass

        def read(self, n):
            if not blocks:
                raise StopIteration
            return blocks.pop(0).reshape(-1, 1), False

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(InputStream=FakeStream))
    mic = listen.MicSegmenter()
    try:
        for _ in mic(None):
            pass
    except (StopIteration, RuntimeError):
        pass
    assert "blocking it for Terminal" in capsys.readouterr().out


@pytest.mark.parametrize("written, spoken", [
    ("Booked Lil Jay (Jay Carter) for Fri Jan 4 at 7 PM — 3h recording at $50/h ($150).",
     "Booked Lil Jay, Jay Carter, for Friday January 4th at 7 PM, 3 hours recording at 50 dollars an hour, 150 dollars."),
    ("AMZN: Monthly 2D-2U reversal (R:R 1.55)", "A M Z N: Monthly two down, two up reversal, risk to reward 1.55"),
    ("NQ=F futures is at 31,264.25.", "N Q futures is at 31,264.25."),
    ("Prior bar 758.79–772.65 on 2030-01-04 19:00", "Prior bar 758.79 to 772.65 on January 4th at 7 PM"),
    ("Logged $120.50 from Ana.", "Logged 120 dollars and 50 cents from Ana."),
    ("**Done** ✅ — streak 🔥", "Done, streak"),
])
def test_speakable(written, spoken):
    from amo.voice.speakable import to_speech

    assert to_speech(written) == spoken


def test_polish_adds_lead_in_and_levels_volume():
    import io
    import wave

    import numpy as np

    from amo.voice.tts import polish

    rate = 22050
    quiet = (np.sin(2 * np.pi * 200 * np.arange(rate) / rate) * 800).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate), w.writeframes(quiet.tobytes())
    with wave.open(io.BytesIO(polish(buf.getvalue()))) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    assert np.all(x[: int(0.15 * rate)] == 0)           # silent lead-in so the first word isn't clipped
    assert np.sqrt(np.mean(x ** 2)) > 800 / 32768 * 2   # quiet input brought up to speaking level


def test_long_waits_get_progress_updates(monkeypatch):
    import threading
    import time

    from amo.config import settings
    from amo.voice import acks

    monkeypatch.setattr(settings, "acks", True)
    said = []
    worker = threading.Thread(target=lambda: time.sleep(0.35))
    worker.start()
    acks.wait_with_updates(worker, said.append, first_after=0.1)
    assert said == ["Still working on it.", "Almost there."]
    quick = threading.Thread(target=lambda: None)
    quick.start()
    said.clear()
    acks.wait_with_updates(quick, said.append, first_after=0.1)
    assert said == []
