"""Strat level alerts: tell me when a ticker breaks the previous candle's high or low (max 3)."""

import time

import pytest

from amo import levels, market, proactive
from amo.agent import Agent
from amo.market import Bar

PRICES: dict[str, list] = {}


@pytest.fixture()
def feed(monkeypatch):
    """Fake candles: yesterday 20–25 and today's live bar, which tests move around."""
    now = int(time.time())

    def candles(sym, tf="D"):
        cur = PRICES.get(sym, {"h": 24, "l": 21, "c": 23, "t": now})
        return [Bar(now - 2 * 86400, 21, 24, 19, 22), Bar(now - 86400, 22, 25, 20, 23),
                Bar(cur.get("t", now), 23, cur["h"], cur["l"], cur["c"])], {"exchangeTimezoneName": "UTC"}

    monkeypatch.setattr(market, "candles", candles)
    monkeypatch.setattr(levels, "_continuity", lambda sym, side: "")
    monkeypatch.setattr(levels, "is_trading", lambda sym, now=None: True)
    PRICES.clear()
    return now


def said(db):
    return [r["text"] for r in db.all("SELECT text FROM announcements ORDER BY id")]


def test_set_by_voice_and_alerted_once_per_side(db, llm, feed):
    a = Agent(db)
    out = a.chat([{"role": "user", "content": "put an alert on PLTR for the daily"}])["content"]
    assert out.startswith("Watching PLTR on the daily — yesterday's high 25.00, low 20.00") and "1 of 3" in out
    assert levels.check(db) == 0  # inside yesterday's range
    PRICES["PLTR"] = {"h": 25.6, "l": 21, "c": 25.5}
    assert levels.check(db) == 1
    assert said(db)[-1].startswith("PLTR just broke yesterday's high 25.00 — daily 2U. Price 25.50.")
    assert levels.check(db) == 0  # once per side per candle
    PRICES["PLTR"] = {"h": 25.6, "l": 19.5, "c": 19.8}
    assert levels.check(db) == 1 and "now an outside bar (3)" in said(db)[-1]
    assert llm.calls == []


def test_a_new_candle_rearms_it(db, feed):
    levels.add("NQ", "D", db)
    PRICES["NQ=F"] = {"h": 26, "l": 21, "c": 25.5}
    assert levels.check(db) == 1
    PRICES["NQ=F"] = {"h": 26, "l": 21, "c": 25.5, "t": feed + 60}  # next candle (same fake levels)
    assert levels.check(db) == 1


def test_already_broken_when_set_is_not_announced(db, feed):
    PRICES["TSLA"] = {"h": 26, "l": 21, "c": 25.8}
    r = levels.add("TSLA", "D", db)
    assert "already above the high (2U)" in r["text"]
    assert levels.check(db) == 0
    PRICES["TSLA"] = {"h": 26, "l": 19, "c": 19.5}
    assert levels.check(db) == 1 and "broke yesterday's low too" in said(db)[-1]


def test_max_three_list_and_remove(db, llm, feed):
    a = Agent(db)
    for t in ("put an alert on PLTR for the daily", "watch NQ for a break on the 4 hour",
              "alert me when BTC breaks the daily high or low"):
        a.chat([{"role": "user", "content": t}])
    assert [(x["symbol"], x["tf"]) for x in levels.active(db)] == [("PLTR", "D"), ("NQ=F", "4H"), ("BTC-USD", "D")]
    out = a.chat([{"role": "user", "content": "put an alert on AAPL for the daily"}])["content"]
    assert "max of 3 alerts" in out
    assert a.chat([{"role": "user", "content": "what alerts do I have?"}])["content"].startswith("Watching 3 of 3:")
    assert a.chat([{"role": "user", "content": "remove the PLTR alert"}])["content"] == "Removed the PLTR daily alert."
    assert a.chat([{"role": "user", "content": "clear my alerts"}])["content"].startswith("Removed the NQ 4-hour, BTC")
    assert levels.active(db) == [] and llm.calls == []


def test_not_checked_when_the_market_is_closed(db, feed, monkeypatch):
    monkeypatch.setattr(levels, "is_trading", lambda sym, now=None: False)
    levels.add("PLTR", "D", db)
    PRICES["PLTR"] = {"h": 26, "l": 21, "c": 25.5}
    assert levels.check(db) == 0


def test_dashboard_and_voice_share_the_list(db, feed):
    from fastapi.testclient import TestClient

    from amo.api.main import app

    with TestClient(app) as c:
        assert c.post("/api/alerts", json={"symbol": "pltr", "tf": "4H"}).json()["symbol"] == "PLTR"
        Agent(db).chat([{"role": "user", "content": "put an alert on BTC for the daily"}])
        got = c.get("/api/alerts").json()
        assert [a["name"] for a in got["alerts"]] == ["PLTR", "BTC"] and got["max"] == 3
        assert c.delete(f"/api/alerts/{got['alerts'][0]['id']}").status_code == 200
        assert [a["name"] for a in c.get("/api/alerts").json()["alerts"]] == ["BTC"]


def test_phrases():
    m = levels.match
    assert m("put an alert on PLTR for the daily") == ("add_level_alert", {"symbol": "PLTR", "timeframe": "D"})
    assert m("watch PLTR for a break for me") == ("add_level_alert", {"symbol": "PLTR", "timeframe": "D"})
    assert m("set an alert on ETH on the 4 hour") == ("add_level_alert", {"symbol": "ETH", "timeframe": "4H"})
    assert m("alert me when TSLA breaks the weekly high") == ("add_level_alert", {"symbol": "TSLA", "timeframe": "W"})
    assert m("turn off the alerts") is None and m("watch PLTR") is None  # plain "watch" stays the watchlist


def test_futures_hours():
    assert levels.is_trading("BTC-USD")
    assert isinstance(levels.is_trading("NQ=F"), bool) and proactive.market_open is not None


def test_when_closed_it_shows_the_next_triggers(db, feed):
    PRICES["PLTR"] = {"h": 24.5, "l": 21.5, "c": 24, "t": feed - 3 * 86400}  # last candle isn't today's
    r = levels.add("PLTR", "D", db)
    assert "next candle's triggers: above 24.50 or below 21.50" in r["text"] and "market's closed" in r["text"]
    assert (r["high"], r["low"]) == (24.5, 21.5)
    assert levels.check(db) == 0


@pytest.mark.parametrize("said, symbols, tf", [
    ("set an alert for PLTR daily", "PLTR", "D"), ("alert me on PLTR", "PLTR", "D"),
    ("set a daily alert on PLTR", "PLTR", "D"), ("set alerts on PLTR, NQ and BTC", "PLTR, NQ, BTC", "D"),
    ("put alerts on PLTR and TSLA for the daily", "PLTR, TSLA", "D"),
    ("let me know when PLTR breaks the daily high", "PLTR", "D"),
    ("notify me if NQ breaks yesterday's high or low", "NQ", "D"), ("watch PLTR and NQ for a break", "PLTR, NQ", "D"),
    ("put PLTR on alert", "PLTR", "D"), ("add PLTR to my alerts", "PLTR", "D"),
    ("set an alert on BTC on the 4 hour", "BTC", "4H"),
])
def test_any_wording(said, symbols, tf):
    assert levels.match(said) == ("add_level_alert", {"symbol": symbols, "timeframe": tf})


@pytest.mark.parametrize("said", ["turn off the alerts", "let me know when you're done", "tell me a joke",
                                  "alert me when my session starts", "let me know when Jay pays"])
def test_not_alerts(said):
    assert levels.match(said) is None


def test_several_at_once_shows_on_the_dashboard(db, llm, feed):
    out = Agent(db).chat([{"role": "user", "content": "set alerts on PLTR, NQ and BTC"}])["content"]
    assert [a["name"] for a in levels.views(db)] == ["PLTR", "NQ", "BTC"] and "3 of 3" in out and llm.calls == []


def test_amo_never_claims_an_alert_it_did_not_set(db, llm, feed):
    from conftest import tool_call

    # a phrasing the shortcut doesn't know: the model says "done" without the tool → made to do it
    llm.script({"role": "assistant", "content": "Alert is set — I'll let you know when it breaks."},
               tool_call("add_level_alert", symbol="AMD", timeframe="D"))
    Agent(db).chat([{"role": "user", "content": "keep an eye on AMD's daily range and alert me"}])
    assert [a["name"] for a in levels.views(db)] == ["AMD"]
