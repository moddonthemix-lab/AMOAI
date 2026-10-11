"""The morning brief: templates only, prepared ahead, severe weather first."""

import json
from datetime import datetime, timedelta

from amo import briefing
from amo.crm import StudioCRM
from amo.goals import Goals

MORNING = datetime(2026, 10, 12, 7, 30)  # a Monday
SUNNY = {"now": 58, "now_code": 0, "code": 2, "high": 74, "low": 55, "rain": 60, "uv": 8, "sunset": "18:58",
         "at": MORNING.isoformat()}
WEEKLY = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:1
DTSTART;TZID=America/New_York:20260105T100000
DTEND;TZID=America/New_York:20260105T110000
RRULE:FREQ=WEEKLY;BYDAY=MO
SUMMARY:Gym with Ty
END:VEVENT
BEGIN:VEVENT
UID:2
DTSTART;VALUE=DATE:20261012
SUMMARY:Mom's birthday
END:VEVENT
END:VCALENDAR
"""


def test_brief_reads_like_a_person(db):
    StudioCRM(db).add_client("Jay Carter", artist_name="Lil Jay")
    StudioCRM(db).book_session("Lil Jay", "2026-10-12 19:00", hours=3, rate=50)
    Goals(db).add("Make one beat")
    b = briefing.build(db, MORNING, weather=SUNNY, events=[{"time": None, "end": None, "title": "Mom's birthday"},
                                                           {"time": "10:00", "end": "11:00", "title": "Gym with Ty"}],
                       warnings=[], fetch=False)
    text = b["text"]
    assert text.startswith("Good morning, Modd. It's Monday, October 12.")
    assert "Right now it's clear, 58 degrees. Today: partly cloudy, a high of 74 and a low of 55." in text
    assert "60% chance of rain — grab an umbrella." in text and "sunscreen" in text
    assert "On your calendar: Mom's birthday (all day) and Gym with Ty at 10 AM." in text
    assert "In the studio: Lil Jay at 7 PM (3 hours)." in text
    assert "Daily goals: Make one beat." in text


def test_severe_weather_comes_first(db):
    warn = [{"id": "x", "event": "Tornado Warning", "severity": "Extreme", "ends": None, "headline": ""}]
    b = briefing.build(db, MORNING, weather=SUNNY, warnings=warn, events=[], fetch=False)
    assert [p["key"] for p in b["parts"]][:3] == ["hello", "alerts", "weather"]
    assert "Heads up: a Tornado Warning." in b["text"]


def test_recurring_calendar_events(db, monkeypatch):
    from amo import calendar_feed
    from amo.config import settings

    monkeypatch.setattr(settings, "calendar_urls", "webcal://example.com/secret.ics")
    monkeypatch.setattr(calendar_feed, "_fetch", lambda url, timeout=10: WEEKLY.encode())
    monkeypatch.setattr(calendar_feed, "CACHE_DIR", str(__import__("tempfile").mkdtemp()))
    events = briefing.calendar_events(MORNING.date())
    assert [e["title"] for e in events] == ["Mom's birthday", "Gym with Ty"]  # the weekly one, this Monday
    assert briefing.calendar_events((MORNING + timedelta(days=1)).date()) == []


def test_prepared_ahead_and_refreshed(db):
    assert briefing.due_to_prepare(db, MORNING.replace(hour=7, minute=20), 8)  # from 45 min before 8
    assert not briefing.due_to_prepare(db, MORNING.replace(hour=6, minute=0), 8)
    b = briefing.prepare(db, MORNING, render=False)
    assert not briefing.due_to_prepare(db, MORNING + timedelta(minutes=5), 8)
    assert briefing.today(db, MORNING + timedelta(hours=1))["prepared_at"] == b["prepared_at"]  # cached
    later = briefing.today(db, MORNING + timedelta(hours=4))  # stale → rebuilt
    assert later["prepared_at"] != b["prepared_at"]


def test_severe_alerts_announced_once(db, monkeypatch):
    from amo.config import settings
    from amo.proactive import take_pending

    monkeypatch.setattr(settings, "location", "33.75,-84.39")
    monkeypatch.setattr(settings, "quiet_hours", "22-8")
    monkeypatch.setattr(briefing, "alerts", lambda loc: [
        {"id": "a1", "event": "Tornado Warning", "severity": "Extreme", "ends": None, "headline": ""},
        {"id": "a2", "event": "Wind Advisory", "severity": "Moderate", "ends": None, "headline": ""}])
    night = datetime(2026, 10, 12, 2, 0)
    assert briefing.watch_alerts(db, night) == 1
    assert briefing.watch_alerts(db, night + timedelta(minutes=20)) == 0  # never repeats
    assert take_pending(db, night) == ["Weather alert: a Tornado Warning."]  # even in quiet hours


def test_long_speech_is_cached(tmp_path, monkeypatch):
    from amo.config import settings
    from amo.voice import tts

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "amo.db"))
    monkeypatch.setattr(tts, "piper_installed", lambda: True)
    calls = []
    monkeypatch.setattr(tts, "_synthesize", lambda text, style: calls.append(text) or b"RIFFfake")
    long = "Good morning, Modd. " * 12
    assert tts.synthesize(long) == b"RIFFfake" and tts.synthesize(long) == b"RIFFfake"
    assert len(calls) == 1  # second time straight from disk
    tts.synthesize("Got it.")
    tts.synthesize("Got it.")
    assert len(calls) == 3  # short phrases aren't cached on disk
