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
