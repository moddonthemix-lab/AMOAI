import sys
import types

import numpy as np

from amo.voice import listen, wake


def test_vosk_wake_rule():
    assert wake.vosk_heard_wake("hey amo")
    assert wake.vosk_heard_wake("hey ammo [unk]")
    assert not wake.vosk_heard_wake("amo [unk]")       # "Hey" required for Vosk
    assert not wake.vosk_heard_wake("ammo")
    assert wake.vosk_heard_wake("hey a mo")
    assert not wake.vosk_heard_wake("[unk] hey")
    assert not wake.vosk_heard_wake("hey [unk]")
    assert not wake.vosk_heard_wake("[unk] [unk] [unk] ammo")  # mid-sentence "ammo"


def fake_mic(monkeypatch, blocks):
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
    return listen.MicSegmenter(sensitivity=2)


def test_streaming_wake_captures_what_follows(monkeypatch):
    rng = np.random.default_rng(1)
    quiet = lambda n: [rng.normal(0, 0.002, 1600).astype("float32") for _ in range(n)]  # noqa: E731
    loud = lambda n: [rng.normal(0, 0.1, 1600).astype("float32") for _ in range(n)]  # noqa: E731
    marker = np.full(1600, 0.5, dtype="float32")  # stands for the moment "Hey AMO" is recognised
    blocks = quiet(15) + quiet(5) + [marker] + loud(12) + quiet(10) + quiet(5)
    mic = fake_mic(monkeypatch, blocks)

    class FakeDetector:
        def process(self, block):
            return bool(np.max(block) == 0.5)

        def reset(self):
            pass

    events = []
    try:
        for ev in wake.StreamingWake(mic, FakeDetector())(None):
            events.append(ev)
    except (StopIteration, RuntimeError):  # the fake mic ran out of audio
        pass
    assert len(events) == 1 and events[0].audio is not None
    assert 1.1 <= len(events[0].audio) / 16000 <= 2.5  # the request said right after "Hey AMO"


def test_listener_uses_wake_stream_for_idle_listening():
    from amo.voice.wake import WakeEvent

    asked = []
    lst = listen.Listener(
        segments=lambda timeout: iter(()),
        wake_stream=lambda timeout: iter([WakeEvent("audio")]),
        transcribe_wake=lambda seg: "Hey AMO what's on today" if isinstance(seg, WakeEvent) else "",
        transcribe_command=lambda seg: "Hey AMO what's on today",
        ask=lambda h: asked.append(h[-1]["content"]) or "Two sessions.", say=lambda t: None,
        log=lambda *_: None)
    lst.next_utterance = lambda timeout: None
    lst.run()
    assert asked == ["what's on today"]
