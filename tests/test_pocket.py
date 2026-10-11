"""The pocket pet: its offline copy of your day, voice clips, and what it brings back."""

import io
import json
import time
import wave
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from amo import pocket, timers
from amo.agent import Agent
from amo.api.main import app


def wav(seconds=0.5, rate=22050):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x10\x00" * int(seconds * rate))
    return buf.getvalue()


@pytest.fixture()
def client(db, llm, monkeypatch, tmp_path):
    monkeypatch.setattr(pocket, "DIR", tmp_path / "pocket")
    monkeypatch.setattr("amo.voice.tts.synthesize", lambda text, voice=None: wav())
    app.state.run_scheduler = False
    with TestClient(app) as c:
        yield c


def test_audio_is_made_small_speaker_friendly():
    pcm = pocket.to_pcm16k(wav(1.0, 22050))
    assert len(pcm) == 2 * 16000  # 1 s of 16 kHz 16-bit mono
    with wave.open(io.BytesIO(pocket.to_wav16k(pcm))) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1


def test_clip_words():
    assert pocket.clip_text("h7") == "seven" and pocket.clip_text("m00") == "o'clock"
    assert pocket.clip_text("m05") == "oh five" and pocket.clip_text("m42") == "forty two"
    assert pocket.clip_text("h13") is None and pocket.clip_text("nope") is None


def test_snapshot_has_what_the_pet_needs_offline(db, client):
    from amo.crm import StudioCRM
    from amo.goals import Goals

    StudioCRM(db).add_client("Jay Carter", artist_name="Lil Jay")
    today = datetime.now().strftime("%Y-%m-%d")
    StudioCRM(db).book_session("Lil Jay", f"{today} 23:59")
    Goals(db).add("Make one beat")
    t = timers.start_timer(10, "pasta", db=db)
    snap = client.get("/api/pocket/snapshot").json()
    assert snap["owner"] == "Modd" and abs(snap["now"] - time.time()) < 5
    assert snap["sessions"] == [{"time": "23:59", "who": "Lil Jay"}] or snap["sessions"] == []  # timezone edge
    assert snap["goals"][0]["title"] == "Make one beat" and snap["goals"][0]["done"] is False
    timer = snap["timers"][0]
    assert timer["label"] == "pasta" and timer["clip"] == f"t{t['id']}" and 590 <= timer["due"] - time.time() <= 610
    ids = {c["id"] for c in snap["clips"]}
    assert {"offline", "its", "am", "pm", "h12", "m59", f"t{t['id']}"} <= ids
    r = client.get(f"/api/pocket/clip/t{t['id']}")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(r.content)) as w:
        assert w.getframerate() == 16000
    assert client.get("/api/pocket/clip/h99").status_code == 404


def test_offline_uploads_are_handled_and_asked_about(db, client, monkeypatch):
    said = {"a.wav": "note: beat idea, 140 BPM", "b.wav": "book Lil Jay Friday at 7", "c.wav": "I feel good today"}
    files = {}

    def upload(eid, etype, body=b""):
        files[eid] = body
        ev = {"id": eid, "type": etype, "at": int(time.time()) - 600}
        return client.post("/api/pocket/sync", params={"event": json.dumps(ev)}, content=body)

    assert upload("a", "voice_note", b"A").json() == {"ok": True}
    assert upload("a", "voice_note", b"A").json()["duplicate"] is True  # re-upload is harmless
    upload("b", "voice_note", b"B")
    upload("c", "voice_note", b"C")
    upload("p", "photo", b"\xff\xd8jpeg")
    by_body = {b"A": said["a.wav"], b"B": said["b.wav"], b"C": said["c.wav"]}
    out = pocket.process(db, transcribe=lambda audio: by_body[audio], describe=lambda img, q: "A red mug on a desk.")
    assert out["notes"] == 2 and out["photos"] == 1 and out["asked"] == ["book Lil Jay Friday at 7"]
    assert out["summary"] == ("While I was offline I saved 2 notes and 1 photo. "
                              "You also asked me to book Lil Jay Friday at 7 — want me to do it?")
    texts = [r["text"] for r in db.all("SELECT text FROM notes ORDER BY id")]
    assert texts[0] == "Beat idea, 140 BPM" and texts[1].startswith("Voice note (") and "I feel good today" in texts[1]
    assert texts[2].startswith("Photo (") and "A red mug on a desk." in texts[2]
    assert pocket.clip_text(out["say"], db) == out["summary"]
    assert pocket.process(db, transcribe=lambda a: "x")["notes"] == 0  # nothing new


def test_yes_and_skip_for_offline_requests(db, llm):
    now = datetime.now().isoformat(timespec="seconds")
    pocket.add_pending("add a daily goal: stretch", now, db)
    pocket.add_pending("add a daily goal: read", now, db)
    a = Agent(db)
    out = a.chat([{"role": "user", "content": "yes"}])["content"]
    assert "New daily goal: Stretch." in out and out.endswith("You also asked me to add a daily goal: read — want me to do it?")
    assert a.chat([{"role": "user", "content": "skip it"}])["content"] == "Okay, skipped."
    assert db.scalar("SELECT COUNT(*) FROM goals") == 1 and pocket.pending(db) == []
    # old requests expire instead of being done days later
    pocket.add_pending("add a daily goal: nap", (datetime.now() - timedelta(days=3)).isoformat(), db)
    assert pocket.pending(db) == []


def test_talk_streams_pcm(db, client, llm, monkeypatch):
    monkeypatch.setattr("amo.voice.stt.transcribe", lambda audio, language="en", size=None, prompt=None: "what's 2 plus 2")
    r = client.post("/api/pocket/talk", content=wav())
    assert r.status_code == 200 and r.headers["x-amo-rate"] == "16000"
    assert len(r.content) > 1000 and len(r.content) % 2 == 0
