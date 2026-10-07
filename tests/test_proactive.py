from datetime import datetime, timedelta

import pytest

from amo import proactive, strat
from amo.config import settings
from amo.market import Bar


def B(h, l, o, c, t=0):  # noqa: E741
    return Bar(t, o, h, l, c)


@pytest.fixture()
def triggered_market(monkeypatch):
    """Every timeframe above its open (FTFC up); daily: 2U, inside bar, then the live bar
    breaks the inside bar's high → a 2-1-2U continuation has TRIGGERED."""
    def bars_for(tf):
        base = [B(100 + i, 90 + i, 95 + i, 99 + i, i) for i in range(30)]
        if tf == "D":
            base[-3] = B(140, 125, 126, 139, 27)
            base[-2] = B(138, 128, 129, 137, 28)
            base[-1] = B(139, 129, 129.5, 138.5, 29)
        return base

    monkeypatch.setattr(strat, "lookup", lambda s: s.upper())
    monkeypatch.setattr(strat, "candles", lambda sym, tf: (bars_for(tf), {"shortName": sym}))
    monkeypatch.setattr(proactive, "market_open", lambda now: True)
    monkeypatch.setattr(settings, "quiet_hours", "")


def test_watchlist_trigger_alert_fires_once(db, triggered_market):
    db.insert("watchlist", {"symbol": "AMZN", "created_at": "x"})
    now = datetime(2030, 3, 5, 11, 0)
    assert proactive.strat_watch(db, now) >= 1
    said = proactive.take_pending(db, now)
    assert any(s.startswith("Heads up: AMZN just triggered the daily 2U-1-2U continuation above 138") for s in said)
    # 15 minutes later, same trigger → silence (never repeats)
    assert proactive.strat_watch(db, now + timedelta(minutes=16)) == 0
    assert proactive.take_pending(db, now + timedelta(minutes=16)) == []


def test_quiet_hours_hold_announcements(db, monkeypatch):
    monkeypatch.setattr(settings, "quiet_hours", "22-8")
    proactive.announce("late alert", "alert", "k1", ttl_minutes=24 * 60, db=db, push=False, now=datetime(2030, 3, 5, 23, 0))
    assert proactive.take_pending(db, datetime(2030, 3, 5, 23, 30)) == []
    assert proactive.take_pending(db, datetime(2030, 3, 6, 8, 5)) == ["late alert"]
    assert proactive.quiet_now(datetime(2030, 3, 5, 7, 59)) and not proactive.quiet_now(datetime(2030, 3, 5, 12))


def test_expired_announcements_are_dropped(db, monkeypatch):
    monkeypatch.setattr(settings, "quiet_hours", "")
    proactive.announce("old news", key="old", ttl_minutes=10, db=db, push=False)
    from amo.db import local_now

    assert proactive.take_pending(db, local_now() + timedelta(hours=1)) == []


def test_studio_nudges_and_checkin(db, monkeypatch):
    from amo.crm import StudioCRM
    from amo.goals import Goals

    monkeypatch.setattr(settings, "quiet_hours", "")
    crm = StudioCRM(db)
    crm.add_client("Jay Carter", artist_name="Lil Jay")
    now = datetime(2030, 3, 5, 18, 50)
    crm.book_session("Lil Jay", "2030-03-05 19:00")
    done = crm.book_session("Lil Jay", "2030-03-04 19:00", rate=50, hours=2)
    crm.complete_session(done["id"], paid=40)
    Goals(db).add("Make one beat")
    monkeypatch.setattr(settings, "checkin_hour", 18)
    proactive.studio_nudges(db, now)
    proactive.daily_rhythm(db, now)
    said = " | ".join(proactive.take_pending(db, now))
    assert "Lil Jay's recording session starts at 19:00" in said
    assert "$60 is still owed" in said
    assert "Evening check-in: you've done 0 of 1 daily goals. Still open: Make one beat." in said
    proactive.studio_nudges(db, now)  # same day → no repeats
    assert proactive.take_pending(db, now) == []


def test_listener_speaks_announcements_between_conversations():
    from amo.voice.listen import Listener

    said, queue = [], [["Heads up: AMZN triggered."], []]
    rounds = iter(range(2))

    def segments(timeout):
        if next(rounds, None) is None:
            raise KeyboardInterrupt
        return iter(())

    lst = Listener(segments=segments, transcribe_wake=str, transcribe_command=str, ask=lambda h: "",
                   say=said.append, log=lambda *_: None, announcements=lambda: queue.pop(0) if queue else [])
    with pytest.raises(KeyboardInterrupt):
        lst.run()
    assert said == ["Heads up: AMZN triggered."]
    lst.asleep = True
    queue.append(["should not be said"])
    lst.speak_announcements()
    assert said == ["Heads up: AMZN triggered."]


def test_watchlist_by_voice(db, llm, monkeypatch):
    from amo import market
    from amo.agent import Agent

    monkeypatch.setattr(market, "lookup", lambda s: {"amazon": "AMZN", "tesla": "TSLA"}.get(s.strip().lower(), s.strip().upper()))
    out = Agent(db).chat([{"role": "user", "content": "add Amazon and Tesla to my watchlist"}])
    assert out["content"].startswith("Watching AMZN, TSLA.") and llm.calls == []
    out = Agent(db).chat([{"role": "user", "content": "what's on my watchlist?"}])
    assert out["content"] == "Your watchlist: AMZN, TSLA."
    out = Agent(db).chat([{"role": "user", "content": "remove Tesla from my watchlist"}])
    assert out["content"] == "Stopped watching TSLA."
