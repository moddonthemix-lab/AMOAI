from datetime import datetime, time as dtime

import pytest

from amo import coach


def test_rules_are_understood():
    k = lambda t: (coach.parse_rule(t).kind, coach.parse_rule(t).value)  # noqa: E731
    assert k("No trades in the first 5 minutes") == ("first_minutes", 5)
    assert k("No trades after 11am") == ("after", dtime(11, 0))
    assert k("don't trade after 3") == ("after", dtime(15, 0))
    assert k("Never trade before 9:45 am") == ("before", dtime(9, 45))
    assert k("Max 3 trades a day") == ("max_per_day", 3)
    assert k("Always use a stop loss") == ("stop", None)
    assert k("At least 2 to 1 on every trade") == ("min_rr", 2.0)
    assert k("Only trade with full timeframe continuity") == ("continuity", None)
    assert k("Journal every trade before bed") == ("reminder", None)


BULL = {"symbol": "NQ=F", "bias": "bullish", "why": "full timeframe continuity is UP"}


def test_check_against_continuity_and_rules(db):
    for r in ("No trades in the first 5 minutes", "Always use a stop", "At least 2 to 1",
              "Only trade with continuity", "Journal every trade"):
        db.insert("trading_rules", {"rule": r, "created_at": "x"})
    c = coach.check_trade("NQ", "short", 18000, None, None, when=datetime(2030, 3, 5, 9, 32), db=db, strat_read=BULL)
    text = coach.summary(c, "NQ", "short", planned=True)
    assert c.verdict == "against" and c.with_continuity is False
    assert "against continuity — NQ is bullish" in text
    assert "breaks your rule “No trades in the first 5 minutes” (it's 9:32)" in text
    assert "breaks your rule “Always use a stop”" in text
    assert text.startswith("I'd pass on that short in NQ.") and "Remember: your rule: Journal every trade" in text

    good = coach.check_trade("NQ", "long", 18000, 17980, 18060, when=datetime(2030, 3, 5, 10, 15), db=db, strat_read=BULL)
    assert good.verdict == "ok" and good.rr == 3.0 and good.with_continuity is True
    assert coach.summary(good, "NQ", "long", planned=True).startswith("The long in NQ checks out.")


def test_logging_a_trade_gets_coached_and_reviewed(db, llm, monkeypatch):
    from amo.agent import Agent
    from amo.trading import TradingJournal

    monkeypatch.setattr(coach, "_ny_now", lambda: datetime(2030, 3, 5, 9, 33))
    from amo import strat
    monkeypatch.setattr(strat, "analyze", lambda s: dict(BULL))
    db.insert("trading_rules", {"rule": "No trades in the first 5 minutes", "created_at": "x"})
    from conftest import tool_call

    llm.script(tool_call("log_trade", symbol="NQ", side="short", entry=18000, stop=18020))
    out = Agent(db).chat([{"role": "user", "content": "short NQ at 18000 stop 18020"}])
    assert out["content"].startswith("Logged short NQ at 18000, stop 18020")
    assert "⚠ against continuity" in out["content"] and "No trades in the first 5 minutes" in out["content"]
    t = TradingJournal(db).recent(1)[0]
    assert t["with_continuity"] == 0 and t["followed_rules"] == 0 and "against continuity" in t["coach_notes"]
    TradingJournal(db).close_trade(t["id"], 18030)

    rev = coach.review("year", db)["text"]
    assert "1 trades, net -$30" in rev and "Against: 1 trades, -$30" in rev
    assert "Breaking your rules cost -$30" in rev and rev.endswith("wait for the timeframes to agree.")


@pytest.mark.parametrize("said, tool", [
    ("Should I go long Amazon at 255 with a stop at 249 and a target of 268?", "check_trade"),
    ("how's my trading this week", "trading_review"),
])
def test_coach_phrases(said, tool):
    from amo.fastpath import match

    assert match(said)[0] == tool
