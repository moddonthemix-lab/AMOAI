import threading
import time

from amo.voice.speaker import SentenceSpeaker, split_ready


def test_split_ready_keeps_decimals_and_waits_for_full_sentences():
    assert split_ready("AMZN is at 256.29. Monthly is a 2 up") == (["AMZN is at 256.29."], "Monthly is a 2 up")
    assert split_ready("Ok. Here's the thing. ") == (["Ok. Here's the thing."], "")
    assert split_ready("no end yet") == ([], "no end yet")


def make(play_time=0.0):
    played = []

    def play(wav, stop):
        played.append(wav.decode())
        end = time.time() + play_time
        while time.time() < end:
            if stop.is_set():
                return False
            time.sleep(0.005)
        return True

    return SentenceSpeaker(synth=lambda text: text.encode(), play=play), played


def test_speaks_first_sentence_before_the_reply_is_finished():
    sp, played = make()
    sp.feed("You have two sessions today. ")
    assert sp.started.wait(1)              # already speaking…
    assert played == ["You have two sessions today."]
    sp.feed("Jay at seven and Ana at nine")  # …while the rest is still being written
    sp.finish()
    assert sp.wait(2)
    assert played == ["You have two sessions today.", "Jay at seven and Ana at nine"]


def test_prerendered_ack_plays_first_and_stop_interrupts():
    sp, played = make(play_time=0.2)
    sp.say_audio(b"Got it.", "Got it.")
    sp.feed("Sentence one is here. Sentence two is here. Sentence three is here. ")
    time.sleep(0.25)
    sp.stop()
    assert sp.wait(2)
    assert played[0] == "Got it." and len(played) < 4
    assert sp.spoken[0] == "Got it."


def test_agent_streams_answers_but_not_unverified_actions(db, llm):
    from conftest import tool_call

    from amo.agent import Agent

    pieces = []
    llm.script({"role": "assistant", "content": "You have two sessions today. Jay at seven."})
    out = Agent(db).chat([{"role": "user", "content": "what's on today?"}], on_text=pieces.append)
    assert len(pieces) > 3 and "".join(pieces) == out["content"]       # streamed word by word

    pieces.clear()
    llm.script({"role": "assistant", "content": "Sure, I've booked that."},
               tool_call("add_goal", title="Drink water"))
    out = Agent(db).chat([{"role": "user", "content": "please track drinking water daily, add it"}],
                         on_text=pieces.append)
    assert pieces == [out["content"]] == ["New daily goal: Drink water."]  # verified, then said once


def test_converse_streams_ack_then_sentences(db, llm, monkeypatch):
    import base64
    import json

    from fastapi.testclient import TestClient

    from amo.api.main import app
    from amo.config import settings
    from amo.voice import tts

    monkeypatch.setattr(tts, "synthesize", lambda text, voice=None: f"WAV:{text}".encode())
    monkeypatch.setattr(settings, "acks", True)
    llm.script({"role": "assistant", "content": "You have two sessions today. Jay is at seven. Ana is at nine."})
    app.state.run_scheduler = False
    with TestClient(app) as c:
        lines = [json.loads(x) for x in c.post("/api/converse", json={"text": "what's on today?"}).text.splitlines()]
    kinds = [x["type"] for x in lines]
    assert kinds[0] == "ack" and kinds[-1] == "done" and kinds.count("sentence") == 3
    assert base64.b64decode(lines[1]["wav"]) == b"WAV:You have two sessions today."
    assert lines[-1]["reply"] == "You have two sessions today. Jay is at seven. Ana is at nine."


def test_openwebui_gets_real_streaming(db, llm):
    from fastapi.testclient import TestClient

    from amo.api.main import app

    llm.script({"role": "assistant", "content": "one two three four five six seven"})
    app.state.run_scheduler = False
    with TestClient(app) as c:
        body = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "count?"}],
                                                     "stream": True}).text
    assert body.count('"content"') >= 7 and body.strip().endswith("data: [DONE]")
