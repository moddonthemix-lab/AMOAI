import io
import wave

import numpy as np

from amo import face


def tone_wav(seconds=2.0, rate=22050, lead=0.2):
    t = np.arange(int(rate * seconds)) / rate
    x = (np.sin(2 * np.pi * 180 * t) * 9000).astype(np.int16)
    x[: int(lead * rate)] = 0  # silent lead-in, like AMO's polished audio
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate), w.writeframes(x.tobytes())
    return buf.getvalue()


def test_word_timings_follow_the_audio():
    ev = face.say_event("Booked Lil Jay for Fri Jan 4 at 7 PM. Try to contain your excitement.", tone_wav(), start_ms=1000)
    words = [w["w"] for w in ev["words"]]
    assert words[:5] == ["Booked", "Lil", "Jay", "for", "Friday"]       # captions show the spoken form
    times = [w["t"] for w in ev["words"]]
    assert times == sorted(times) and times[0] >= 150                  # in order, after the lead-in
    assert times[-1] < ev["dur"] and ev["start"] == 1000
    assert max(ev["env"]) == 1 and ev["env"][0] == 0                  # loudness curve for the mouth


def test_face_events_api_and_device_post(db, llm):
    from fastapi.testclient import TestClient

    from amo.api.main import app

    app.state.run_scheduler = False
    with TestClient(app) as c:
        start = c.get("/api/face/events").json()["last"]
        face.publish_state("thinking", db)
        face.publish_say("Got it.", tone_wav(0.6), db=db)
        r = c.post("/api/face/event", json={"kind": "say", "text": "Hi.", "words": [{"w": "Hi.", "t": 0}],
                                            "env": [1], "step": 50, "dur": 300, "delay_ms": 100})
        assert r.json() == {"ok": True}
        evs = c.get(f"/api/face/events?after={start}").json()["events"]
        assert [e["kind"] for e in evs] == ["state", "say", "say"]
        assert evs[0]["state"] == "thinking" and evs[1]["text"] == "Got it."
        assert evs[2]["start"] > 0  # stamped by the brain when the device's sentence arrived
        assert c.get("/face").status_code == 200


def test_listener_reports_states():
    from amo.voice.listen import Listener

    states = []
    lst = Listener(segments=lambda t: iter(()), transcribe_wake=str, transcribe_command=str,
                   ask=lambda h: "Two sessions.", say=lambda t: None, log=lambda *_: None,
                   on_state=states.append)
    lst.next_utterance = lambda timeout: None
    lst.converse("what's on today")
    assert states == ["thinking", "listening", "idle"]
