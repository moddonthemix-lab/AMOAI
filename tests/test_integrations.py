import sqlite3
from datetime import datetime

import pytest

from amo import integrations
from amo.agent import Agent
from amo.crm import StudioCRM


def test_calendar_feed(db):
    from amo.cravvr import Cravvr

    crm = StudioCRM(db)
    crm.add_client("Jay Carter", artist_name="Lil Jay", phone="555-0101")
    crm.book_session("Lil Jay", "2030-01-04 19:00", hours=3, rate=50)
    Cravvr(db).add_task("Finalize menu", due_date="2030-01-06")
    ics = integrations.calendar_ics(db)
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "SUMMARY:Studio: Lil Jay (recording)" in ics
    assert "DTSTART:20300104T190000" in ics and "DTEND:20300104T220000" in ics
    assert "recording · 3h · $150\\nPhone: 555-0101" in ics
    assert "SUMMARY:Cravvr: Finalize menu" in ics and "DTSTART;VALUE=DATE:20300106" in ics


def test_calendar_endpoint_needs_key_off_this_mac(db, llm, monkeypatch):
    from fastapi.testclient import TestClient

    from amo.api.main import app
    from amo.config import settings

    monkeypatch.setattr(settings, "api_key", "k")
    app.state.run_scheduler = False
    with TestClient(app) as c:
        assert c.get("/calendar.ics").status_code == 401
        r = c.get("/calendar.ics?key=k")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")


def test_texting_needs_your_ok(db, llm, monkeypatch):
    sent = []
    monkeypatch.setattr(integrations, "_imessage", lambda phone, msg: sent.append((phone, msg)))
    StudioCRM(db).add_client("Jay Carter", artist_name="Lil Jay", phone="555-0101")
    a = Agent(db)
    out = a.chat([{"role": "user", "content": "text Jay that the session is moved to 8"}])
    assert out["content"] == ("Here's the text to Lil Jay (555-0101): “The session is moved to 8” "
                              "Say “send it” to send, or “cancel”.")
    assert sent == [] and llm.calls == []          # drafted, not sent
    out = a.chat([{"role": "user", "content": "send it"}])
    assert out["content"] == "Sent to Lil Jay." and sent == [("555-0101", "The session is moved to 8")]
    # "send it" with nothing waiting doesn't send anything
    llm.script({"role": "assistant", "content": "Send what?"})
    assert a.chat([{"role": "user", "content": "send it"}])["content"] == "Send what?"
    a.chat([{"role": "user", "content": "message Jay saying see you tonight"}])
    assert a.chat([{"role": "user", "content": "cancel"}])["content"] == "Okay, I won't send it."
    assert len(sent) == 1


def test_text_without_phone_number(db, llm):
    StudioCRM(db).add_client("Ana Lopez")
    out = Agent(db).chat([{"role": "user", "content": "text Ana that we're on for Friday"}])
    assert "don't have a phone number for Ana Lopez" in out["content"]


def test_backup_and_restore(tmp_path, monkeypatch):
    from amo.config import settings
    from amo.db import Database

    live = tmp_path / "amo.db"
    db = Database(str(live))
    db.insert("goals", {"title": "Make one beat", "created_at": "x"})
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "bk"))
    path = integrations.backup(db)
    assert path.exists()
    assert sqlite3.connect(path).execute("SELECT title FROM goals").fetchone() == ("Make one beat",)
    db.execute("DELETE FROM goals")
    db.conn.close()
    integrations.restore(str(path), str(live))
    assert sqlite3.connect(live).execute("SELECT title FROM goals").fetchone() == ("Make one beat",)
    assert (tmp_path / "amo.before-restore.db").exists()
    # nightly: once per day, after 3am
    db2 = Database(str(live))
    assert not integrations.nightly_backup(db2, datetime(2030, 3, 5, 2, 0))
    assert integrations.nightly_backup(db2, datetime(2030, 3, 5, 3, 5))
    assert not integrations.nightly_backup(db2, datetime(2030, 3, 5, 9, 0))
