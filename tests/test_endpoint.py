"""When does AMO decide you've started / finished talking? (Simulated 50 ms loudness frames.)"""

import random

from amo.voice.listen import Endpointer


def run(ep, levels):
    """Feed levels; return [(state, frame_index)] for start/end/noise events."""
    return [(st, i) for i, lv in enumerate(levels) if (st := ep.feed(lv)) in ("start", "end", "noise")]


def speech(seconds, loud=0.06, gap_every=6):
    """Talking: loud frames with short dips between words."""
    return [loud * 0.3 if i % gap_every == gap_every - 1 else loud * random.uniform(0.7, 1.2)
            for i in range(int(seconds * 20))]


def quiet(seconds, level=0.003):
    return [level * random.uniform(0.8, 1.2) for _ in range(int(seconds * 20))]


def test_normal_sentence_ends_after_the_pause():
    random.seed(1)
    ep = Endpointer(end_silence=0.9)
    events = run(ep, quiet(2) + speech(2.5) + quiet(3))
    assert [e for e, _ in events] == ["start", "end"]
    end_at = events[1][1] / 20
    assert 2 + 2.5 + 0.85 <= end_at <= 2 + 2.5 + 1.0  # ~0.9 s after you stop


def test_short_pause_mid_sentence_doesnt_cut_you_off():
    random.seed(2)
    ep = Endpointer(end_silence=0.9)
    events = run(ep, quiet(2) + speech(1.5) + quiet(0.6) + speech(1.5) + quiet(2))
    assert [e for e, _ in events] == ["start", "end"]


def test_mic_auto_gain_noise_doesnt_keep_it_waiting():
    """After you stop, an auto-gain mic raises the hiss 4× (to a quarter of your voice) — it must still
    end within ~2 s (the old fixed gate waited for the 12 s limit)."""
    random.seed(3)
    ep = Endpointer(end_silence=0.9)
    events = run(ep, quiet(2) + speech(2) + quiet(6, level=0.012))
    assert [e for e, _ in events] == ["start", "end"]
    assert events[1][1] / 20 <= 2 + 2 + 2.0


def test_noisy_room_and_clicks():
    random.seed(4)
    ep = Endpointer()
    # a fan: steady noise is learned, not mistaken for speech
    assert run(ep, quiet(4, level=0.02)) == []
    # a click/bump: too short to be speech → never sent to Whisper
    ep2 = Endpointer()
    events = run(ep2, quiet(2) + [0.08] * 4 + quiet(2))
    assert "end" not in [e for e, _ in events]
    # talking over the fan still works
    events = run(ep, speech(2, loud=0.15) + quiet(2, level=0.02))
    assert [e for e, _ in events] == ["start", "end"]


def test_long_talk_is_capped():
    random.seed(5)
    ep = Endpointer(max_seconds=5)
    events = run(ep, quiet(1) + speech(8))
    assert ("end", 1 * 20 + 5 * 20 - 1) in events or [e for e, _ in events][:2] == ["start", "end"]


def test_transcribe_while_talking():
    """A pause that turns out to be the end → the text was already being made; talking on → dropped."""
    import time as _t

    from amo.voice.listen import Speculator

    calls = []

    def slow_transcribe(audio):
        calls.append(len(audio))
        _t.sleep(0.05)
        return f"text of {len(audio)} frames"

    def utterance(spec, frames):
        """frames: list of 'S' (speech) / '.' (quiet) after the start; returns the guess future or None."""
        ep = Endpointer(end_silence=0.9)
        for _ in range(10):
            ep.feed(0.003)
        chunks = []
        for f in "SSS" + frames + "." * 30:
            st = ep.feed(0.06 if f == "S" else 0.003)
            if st == "start":
                chunks = [1, 1, 1]
                spec.reset()
            elif st == "speech":
                chunks.append(1)
                spec.frame(st, ep.since_speech, len(chunks), lambda c=list(chunks): c)
            elif st in ("end", "noise"):
                return spec.take()
        return None

    spec = Speculator(slow_transcribe)
    spec.on = True
    # 2 s of talking, then quiet: the guess started 350 ms into the pause and is the answer
    fut = utterance(spec, "S" * 40)
    assert fut is not None and fut.result().startswith("text of") and len(calls) == 1
    # talk, pause, talk again: the first guess is dropped (the final one covers everything)
    calls.clear()
    fut = utterance(spec, "S" * 30 + "." * 8 + "S" * 20)
    text = fut.result()
    spec.pool.shutdown(wait=True)
    assert len(calls) == 2 and text == f"text of {calls[-1]} frames" and calls[-1] > calls[0]
    spec = Speculator(slow_transcribe)
    spec.on = True
    # too short to bother
    calls.clear()
    assert utterance(spec, "S" * 5) is None and calls == []
    # idle listening ("Hey AMO" detection): never runs
    spec.on = False
    assert utterance(spec, "S" * 40) is None
