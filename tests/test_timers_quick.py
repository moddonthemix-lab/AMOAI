"""Instant answers (no AI model) and timers / reminders."""

from datetime import datetime, timedelta

import pytest

from amo import timers
from amo.agent import Agent
from amo.proactive import take_pending
from amo.quick_answers import answer, holidays

NOW = datetime(2026, 10, 11, 14, 5)


# ------------------------------------------------------------------ instant answers
def test_quick_answers():
    assert answer("what time is it", NOW) == "It's 2:05 PM."
    assert answer("hey amo what's today's date?", NOW) == "It's Sunday, October 11th."
    assert answer("what's 15 percent of 240", NOW) == "15 percent of 240 is 36."
    assert answer("what is twelve times seven", NOW) == "12 times 7 is 84."
    assert answer("how many days until christmas", NOW) == "Christmas is in 75 days!"
    assert answer("how many days until thanksgiving", NOW) == "Thanksgiving is in 46 days!"
    assert answer("convert 5 miles to km", NOW) == "5 miles is about 8.05 kilometers."
    assert answer("what's 72 degrees fahrenheit in celsius", NOW) == "72 degrees Fahrenheit is about 22 degrees Celsius."
    assert "divide by zero" in answer("what's 10 divided by 0", NOW)


def test_quick_answers_never_hijack():
    for text in ["what is love", "what's my schedule today", "add a daily goal: make one beat", "what's tesla doing",
                 "how many sessions did I book this week", "what's 2 plus my rate", "book jay for 7"]:
        assert answer(text, NOW) is None, text


def test_us_holidays():
    h = {name: d for d, name in holidays(2026).items()}
    assert h["Thanksgiving"].isoformat() == "2026-11-26"
    assert h["Memorial Day"].isoformat() == "2026-05-25"
    assert h["Martin Luther King Jr. Day"].isoformat() == "2026-01-19"
    assert h["Easter Sunday"].isoformat() == "2026-04-05"


def test_agent_answers_instantly(db, llm):
    out = Agent(db).chat([{"role": "user", "content": "what's 12 times 12"}])
    assert out["content"] == "12 times 12 is 144." and llm.calls == []


# ------------------------------------------------------------------ timers & reminders
def test_parsing():
    assert timers.match("set a pasta timer for 10 minutes", NOW) == (
        "set_timer", {"minutes": 10.0, "label": "pasta", "message": ""})
    assert timers.match("remind me in 30 seconds to stir the soup", NOW) == (
        "set_timer", {"minutes": 0.5, "label": "", "message": "stir the soup"})
    assert timers.match("remind me tomorrow at 9 to call Jay", NOW) == (
        "set_reminder", {"when": "2026-10-12T09:00", "message": "call Jay", "alarm": False})
    assert timers.match("remind me at 5 to send Jay the mix", NOW)[1]["when"] == "2026-10-11T17:00"
    assert timers.match("set an alarm for 7am", NOW)[1] == {"when": "2026-10-12T07:00", "message": "", "alarm": True}
    assert timers.match("cancel the pasta timer", NOW) == ("cancel_timer", {"target": "pasta", "all": False})
    assert timers.match("how long left on the pasta timer", NOW) == ("timer_left", {"target": "pasta"})
    assert timers.match("what timers do I have", NOW) == ("list_timers", {})
    for text in ["remind me to call jay", "how many minutes until dinner", "what's the mood"]:
        assert timers.match(text, NOW) is None, text


def test_timer_lifecycle(db, llm):
    a = Agent(db)
    out = a.chat([{"role": "user", "content": "set a pasta timer for 10 minutes"}])
    assert out["content"].endswith("Pasta timer set for 10 minutes.") and llm.calls == []
    out = a.chat([{"role": "user", "content": "remind me in 30 seconds to stir the soup"}])
    assert "I'll remind you to stir the soup" in out["content"]
    listed = a.chat([{"role": "user", "content": "what timers do I have?"}])["content"]
    assert "the pasta timer" in listed and "stir the soup" in listed
    left = a.chat([{"role": "user", "content": "how long left on the pasta timer"}])["content"]
    assert left.endswith("left on the pasta timer.")

    soon = datetime.fromisoformat(timers.active(db)[0]["due_at"]) + timedelta(seconds=1)
    said = timers.fire_due(db, soon)
    assert said == ["Time's up — stir the soup."]
    assert take_pending(db, soon) == ["Time's up — stir the soup."]
    assert a.chat([{"role": "user", "content": "cancel the pasta timer"}])["content"] == "Cancelled the pasta timer."
    assert timers.active(db) == []


def test_timer_rings_in_quiet_hours_and_missed_ones(db, monkeypatch):
    from amo.config import settings
    from amo.proactive import announce

    monkeypatch.setattr(settings, "quiet_hours", "22-8")
    night = datetime(2026, 10, 12, 6, 59)
    timers.add_reminder(night + timedelta(minutes=1), alarm=True, db=db, now=night)
    announce("Strat alert: TSLA triggered", "alert", db=db, now=night)
    timers.fire_due(db, night + timedelta(minutes=1, seconds=1))
    assert take_pending(db, night + timedelta(minutes=1, seconds=2)) == ["It's 7 AM. That's your alarm."]

    timers.add_reminder(NOW + timedelta(hours=1), "call Jay", db=db, now=NOW)
    assert timers.fire_due(db, NOW + timedelta(hours=3)) == []  # AMO was off: reported, not rung
    assert db.scalar("SELECT status FROM timers WHERE message = 'call Jay'") == "missed"
    assert db.scalar("SELECT title FROM notifications ORDER BY id DESC LIMIT 1") == "Missed while AMO was off"


def test_undo_a_misheard_timer(db, llm):
    a = Agent(db)
    a.chat([{"role": "user", "content": "set a timer for 50 minutes"}])
    out = a.chat([{"role": "user", "content": "set a timer for 50 minutes"},
                  {"role": "assistant", "content": "Timer set for 50 minutes."},
                  {"role": "user", "content": "no, I said set a timer for 15 minutes"}])
    assert out["content"].startswith("Undid that: cancelled the timer.")
    assert [t["due_at"] for t in timers.active(db)] and len(timers.active(db)) == 1
    assert "15 minutes" in out["content"]


@pytest.mark.parametrize("said, minutes", [
    ("set a timer for half an hour", 30), ("remind me in half an hour to call Jay", 30),
    ("timer for 2 and a half hours", 150), ("set a timer for one and a half hours", 90),
    ("timer for 1 hour 30 minutes", 90), ("remind me in 1 hour and 15 minutes to eat", 75),
    ("timer for a minute and a half", 1.5), ("set a 5m timer", 5), ("remind me 10 minutes from now to stir", 10),
])
def test_durations_add_up(said, minutes):
    tool, args = timers.match(said, datetime(2030, 3, 5, 14, 0))
    assert tool == "set_timer" and args["minutes"] == minutes


def test_a_duration_inside_a_reminder_is_not_a_timer():
    now = datetime(2030, 3, 5, 14, 0)  # a Tuesday
    tool, args = timers.match("remind me tomorrow at 9 about the 3 hour mix session", now)
    assert tool == "set_reminder" and args["when"] == "2030-03-06T09:00" and "3 hour mix session" in args["message"]
    tool, args = timers.match("remind me on Friday to book a 2 hour session with Jay", now)
    assert tool == "set_reminder" and args["when"].startswith("2030-03-08")
    assert timers.match("remind me to pay Jay for 2 sessions", now) is None  # "for 2" isn't 2 o'clock
    assert timers.match("set an alarm for 7", now)[0] == "set_reminder"
    # more than a reminder → the AI handles the whole request
    assert timers.match("Book Jay for Friday at 7 and remind me to text him", now) is None
    assert timers.match("when is the alarm going off", now) == ("timer_left", {"target": ""})
    assert timers.match("when does Jay's reminder go off", now) == ("timer_left", {"target": "jay"})
