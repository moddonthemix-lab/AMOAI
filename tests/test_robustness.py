"""Small things that used to break: odd input, two processes at once, time zones."""

import time
from datetime import datetime

from amo import pocket, proactive
from amo.config import settings
from amo.db import parse_when


def test_db_execute_returns_rows_changed_not_an_old_id(db):
    db.insert("notifications", {"title": "a", "body": "", "level": "info", "created_at": "x"})
    db.insert("notifications", {"title": "b", "body": "", "level": "info", "created_at": "x"})
    assert db.execute("UPDATE notifications SET read = 1 WHERE title = 'a'") == 1
    assert db.execute("UPDATE notifications SET read = 1 WHERE title = 'zzz'") == 0


def test_time_zone_offsets_become_local_time(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "America/Chicago")
    assert parse_when("2030-07-01T19:00:00Z") == "2030-07-01T14:00:00"
    assert parse_when("2030-07-01 19:00") == "2030-07-01T19:00:00"


def test_quiet_hours_with_minutes(monkeypatch):
    monkeypatch.setattr(settings, "quiet_hours", "22:00-08:00")
    assert proactive.quiet_now(datetime(2030, 1, 1, 23, 0))
    assert not proactive.quiet_now(datetime(2030, 1, 1, 12, 0))
    monkeypatch.setattr(settings, "quiet_hours", "late")
    assert not proactive.quiet_now(datetime(2030, 1, 1, 23, 0))


def test_an_announcement_is_said_once(db, monkeypatch):
    monkeypatch.setattr(settings, "quiet_hours", "")  # same result whatever time the tests run
    proactive.announce("Hello", kind="checkin", key="k1", db=db)
    assert proactive.take_pending(db) == ["Hello"]
    assert proactive.take_pending(db) == []


def test_notify_with_curly_quotes_never_crashes(db, monkeypatch):
    from amo import notify

    sent = {}
    monkeypatch.setattr(settings, "ntfy_url", "http://ntfy.invalid")
    monkeypatch.setattr(notify.httpx, "post", lambda url, **kw: sent.update(kw["headers"]))
    notify.notify("Jay’s session — paid", "body", db=db)
    assert sent["Title"].isascii() and sent["Title"].startswith("=?utf-8?")


def test_phone_numbers_and_numbers_are_spoken_right():
    from amo.voice.speakable import to_speech

    assert to_speech("Call Jay at 555-0101.") == "Call Jay at 5 5 5, 0 1 0 1."
    assert to_speech("Session #4 is booked") == "Session number 4 is booked"
    assert to_speech("2-3 hours") == "2 to 3 hours"


def test_calendar_notes_are_escaped(db):
    from amo.crm import StudioCRM
    from amo.integrations import calendar_ics

    crm = StudioCRM(db)
    crm.add_client("Ana")
    s = crm.book_session("Ana", "2030-01-01 19:00", hours=2, rate=50)
    db.execute("UPDATE studio_sessions SET notes = ? WHERE id = ?", ("bring drives, mics; hi\nline 2", s["id"]))
    desc = next(line for line in calendar_ics(db).split("\r\n") if line.startswith("DESCRIPTION:"))
    assert "drives\\, mics\; hi\\nline 2" in desc


def test_pocket_pet_knows_when_the_clocks_change(monkeypatch):
    monkeypatch.setattr(settings, "timezone", "America/Chicago")
    july = time.mktime((2030, 7, 1, 12, 0, 0, 0, 0, -1))
    assert pocket._next_clock_change(july) == {}  # nothing in the next 60 days
    oct20 = datetime(2030, 10, 20, 12).timestamp()
    ch = pocket._next_clock_change(oct20)
    assert ch["tz_offset_after"] == -6 * 3600
    assert abs(ch["tz_change"] - datetime.fromisoformat("2030-11-03T07:00:00+00:00").timestamp()) <= 60
