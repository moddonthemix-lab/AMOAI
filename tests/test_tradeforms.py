"""Amo ➜ Trader: the 19:00 trade_setup form and the 16:15 setup_scorecard (fake hub and brain, no network)."""

import json
from types import SimpleNamespace

import pytest

from amo import familyhub, tradeforms as tf
from amo.family.hub_client import HubClientError
from amo.market import Bar

CANDS = [  # what AMO's Strat engine computed
    {"id": 1, "symbol": "AAPL", "market_symbol": "AAPL", "timeframe": "daily", "pattern": "2D-1-2U reversal (inside bar)",
     "direction": "long", "trigger": 182.4, "stop": 179.8, "targets": [185.1], "two_r_target": False, "rr": 1.04,
     "with_continuity": True, "continuity": "M up, W up, D inside", "bias": "bullish", "confidence": "medium"},
    {"id": 2, "symbol": "NQ", "market_symbol": "NQ=F", "timeframe": "4h", "pattern": "2U-2D reversal",
     "direction": "short", "trigger": 21000.0, "stop": 21100.0, "targets": [20800.0], "two_r_target": False,
     "rr": 2.0, "with_continuity": True, "continuity": "M down, W down, D down", "bias": "bearish",
     "confidence": "high"},
    {"id": 3, "symbol": "BTC", "market_symbol": "BTC-USD", "timeframe": "daily", "pattern": "3-2U",
     "direction": "long", "trigger": 81300.0, "stop": 80100.0, "targets": [83700.0], "two_r_target": True,
     "rr": 2.0, "with_continuity": False, "continuity": "M down, W up, D up", "bias": "leaning bearish",
     "confidence": "low"},
]
SETUP_FORM = {"name": "trade_setup", "title": "Trade setups", "list": True, "allow_empty": True,
              "fields": [], "json_schema": {"type": "object"}}
SCORE_FORM = {"name": "setup_scorecard", "title": "Setup scorecard", "list": True, "allow_empty": True,
              "fields": [], "json_schema": {"type": "object"}}


class Hub:
    def __init__(self):
        self.brain, self.results, self.updates, self.refuse = None, [], [], None

    def form(self, name):
        return {"trade_setup": SETUP_FORM, "setup_scorecard": SCORE_FORM}[name]

    def post_result(self, task_id, summary="", artifacts=None, files=None, channel="tasks", form=None):
        if self.refuse:
            raise HubClientError(400, self.refuse)
        self.results.append({"task": task_id, "form": form, "brain": self.brain, "summary": summary})
        return {"id": 1}

    def update_task(self, task_id, status, note=""):
        self.updates.append((task_id, status, note))


class Brain:
    def __init__(self, items=None, note="", fail=False):
        self.items, self.note, self.fail, self.prompts = items, note, fail, []

    def fill_form(self, form, prompt, job="chat", local_only=False, system=None, history=None):
        self.prompts.append(prompt)
        if self.fail:
            return SimpleNamespace(form=None, refused=False, brain="local", reason="no valid form")
        return SimpleNamespace(form={"name": form["name"], "items": self.items or [], "note": self.note},
                               refused=False, brain="cloud")


class Agent:
    def chat(self, messages, channel="webui", local_only=False, **kw):
        return {"content": "AAPL has a daily inside bar; NQ 4-hour reversal; BTC 3-2U.", "brain": "local",
                "tool_calls": [{"name": "watchlist_scan", "result": {"symbols": ["AAPL", "NQ=F", "BTC-USD"]}}]}


@pytest.fixture()
def hub(db, monkeypatch):
    monkeypatch.setattr(tf, "candidates", lambda symbols, now=None: ([dict(c) for c in CANDS], []))
    h = Hub()

    def make(brain):
        return familyhub.Connector(hub=h, db=db, brain=brain, agent=Agent())

    return SimpleNamespace(hub=h, db=db, make=make)


TOP = {"id": 7, "title": "Top setups for tomorrow", "details": "", "requester": "boss", "status": "open",
       "channel": "trades", "expects_form": None}


def test_top_setups_go_out_as_a_trade_setup_form(hub):
    ai = [  # the AI picks, ranks and writes the words; one number is off, one setup is made up
        {"symbol": "NQ", "timeframe": "4h", "pattern": "2U-2D reversal", "direction": "short", "trigger": 21000,
         "stop": 21100, "targets": [20800], "confidence": "high", "why": "Clean 4-hour reversal with continuity."},
        {"symbol": "AAPL", "timeframe": "daily", "pattern": "2-1-2", "direction": "long", "trigger": 182.0,
         "stop": 179.8, "targets": [185.1], "confidence": "high", "why": "Inside bar at weekly support."},
        {"symbol": "TSLA", "timeframe": "daily", "pattern": "2-1-2", "direction": "long", "trigger": 250,
         "stop": 240, "targets": [260], "confidence": "high", "why": "Made up."},
    ]
    brain = Brain(ai, note="Inside bars ranked first.")
    hub.make(brain).do_for_boss(TOP, "Top setups for tomorrow", False)
    [res] = hub.hub.results
    form = res["form"]
    assert form["name"] == "trade_setup" and res["brain"] == "cloud"
    assert [i["symbol"] for i in form["items"]] == ["AAPL", "NQ"]  # inside bar first; TSLA dropped
    aapl = form["items"][0]
    assert (aapl["trigger"], aapl["stop"], aapl["targets"]) == (182.4, 179.8, [185.1])  # the engine's numbers
    assert aapl["why"] == "Inside bar at weekly support." and aapl["timeframe"] == "daily"
    assert set(aapl) == {"symbol", "timeframe", "pattern", "direction", "trigger", "stop", "targets",
                         "invalidation", "continuity", "confidence", "why"}
    assert form["note"].startswith("Inside bars ranked first.")
    assert "exact numbers" in brain.prompts[0] and "AAPL has a daily inside bar" in brain.prompts[0]
    plan = hub.db.one("SELECT * FROM trade_plans")
    assert json.loads(plan["symbols"])["NQ"] == "NQ=F"


def test_the_engine_fills_it_when_the_brain_cant(hub):
    hub.make(Brain(fail=True)).do_for_boss(TOP, "Top setups for tomorrow", False)
    form = hub.hub.results[0]["form"]
    assert [i["symbol"] for i in form["items"]][:1] == ["AAPL"] and len(form["items"]) == 3
    btc = next(i for i in form["items"] if i["symbol"] == "BTC")
    assert "2R" in btc["why"]  # no prior level: the target is said to be 2R
    assert "8:00 PM" in form["note"] or "7:00 PM" in form["note"]  # crypto's daily rollover


def test_nothing_good_is_an_empty_list_with_a_reason(hub):
    hub.make(Brain([], note="Chop on the daily, conflicting continuity.")).do_for_boss(TOP, "", False)
    form = hub.hub.results[0]["form"]
    assert form["items"] == [] and form["note"].startswith("Chop on the daily")


def test_a_refused_form_is_reported_not_sent_as_text(hub):
    hub.hub.refuse = "item 1: 'trigger' must be a number"
    c = hub.make(Brain(fail=True))
    c.do_for_boss(TOP, "", False)
    assert hub.hub.results == []
    assert hub.hub.updates[-1][1] == "blocked" and "'trigger' must be a number" in hub.hub.updates[-1][2]
    assert "refused" in c.state["last_error"]


def test_which_tasks_get_which_form():
    assert tf.wanted_form({"title": "Top setups for tomorrow"}) == "trade_setup"
    assert tf.wanted_form({"title": "Grade today's setups"}) == "setup_scorecard"
    assert tf.wanted_form({"title": "Anything", "expects_form": "trade_setup"}) == "trade_setup"
    assert tf.wanted_form({"title": "What's on my schedule"}) is None


def test_plain_tickers():
    assert [tf.plain(s) for s in ("NQ=F", "BTC-USD", "PEPE24478-USD", "AAPL", "^GSPC")] == \
        ["NQ", "BTC", "PEPE", "AAPL", "GSPC"]


# ------------------------------------------------------------------ grading
def bars(*hl, start=1000):
    return [Bar(start + i * 3600, (h + l) / 2, h, l, (h + l) / 2) for i, (h, l) in enumerate(hl)]


LONG = {"direction": "long", "trigger": 100.0, "stop": 98.0, "targets": [104.0]}


def test_outcomes_from_the_price_data():
    assert tf.outcome(LONG, bars((99.5, 98.5), (99.9, 99)), 0)[0] == "not triggered"
    out, r, _ = tf.outcome(LONG, bars((100.5, 99), (102, 100), (104.5, 101)), 0)
    assert (out, r) == ("hit target", 2.0)
    assert tf.outcome(LONG, bars((100.5, 99), (99.5, 97.5)), 0)[:2] == ("hit stop", -1.0)
    assert tf.outcome(LONG, bars((105, 97)), 0)[0] == "hit stop"  # both in one hour: the stop, to be safe
    out, r, _ = tf.outcome(LONG, bars((101, 99.5)), 0)
    assert out == "still open"
    assert tf.outcome(LONG, bars((105, 97), start=0), 5000)[0] == "not triggered"  # before the plan: ignored


def test_the_scorecard_grades_the_last_plan_and_keeps_a_hit_rate(hub, monkeypatch):
    plan = {"name": "trade_setup", "note": "", "items": [
        {"symbol": "AAPL", "timeframe": "daily", "pattern": "2D-1-2U reversal (inside bar)", "direction": "long",
         "trigger": 100.0, "stop": 98.0, "targets": [104.0]},
        {"symbol": "NQ", "timeframe": "4h", "pattern": "2U-2D reversal", "direction": "short",
         "trigger": 21000.0, "stop": 21100.0, "targets": [20800.0]}]}
    hub.db.insert("trade_plans", {"task_id": 7, "posted_at": "2026-10-28T19:00:00-04:00", "form": json.dumps(plan),
                                  "symbols": json.dumps({"AAPL": "AAPL", "NQ": "NQ=F"})})
    t0 = 1_793_300_000  # after the plan (Oct 28, 7 PM New York)
    data = {"AAPL": bars((100.5, 99), (104.2, 101), start=t0), "NQ=F": bars((21050, 21020), start=t0)}
    monkeypatch.setattr("amo.market.candles", lambda sym, tf_: (data[sym], {}))
    brain = Brain([{"symbol": "AAPL", "direction": "long", "outcome": "hit stop", "r_multiple": -1,
                    "comment": "Clean breakout, target by the second hour."}],
                  note="Inside bars keep delivering; size them up.")
    hub.make(brain).do_for_boss({**TOP, "id": 8, "title": "Grade today's setups"}, "", False)
    form = hub.hub.results[0]["form"]
    assert form["name"] == "setup_scorecard"
    aapl, nq = form["items"]
    assert (aapl["outcome"], aapl["r_multiple"]) == ("hit target", 2.0)  # computed, not the AI's "hit stop"
    assert aapl["comment"] == "Clean breakout, target by the second hour."
    assert (nq["outcome"], nq["r_multiple"]) == ("not triggered", None)
    assert form["note"].startswith("1 of 1 finished setups worked. Running (1 day): 1 of 1 = 100%.")
    assert form["note"].endswith("Lesson: Inside bars keep delivering; size them up.")
    assert hub.db.one("SELECT graded_at FROM trade_plans")["graded_at"]
    assert tf.hit_rates(hub.db) == {"daily|inside bar": {"won": 1, "done": 1, "rate": 1.0}}
    # graded once: the next run has nothing new
    hub.make(Brain()).do_for_boss({**TOP, "id": 9, "title": "Grade today's setups"}, "", False)
    assert hub.hub.results[1]["form"]["items"] == [] and "No plan to grade" in hub.hub.results[1]["form"]["note"]


def test_what_works_is_favored_after_inside_bars(db):
    for outcome in ("hit target", "hit target", "hit target"):
        db.insert("setup_grades", {"plan_id": 1, "symbol": "NQ", "timeframe": "4h", "pattern": "2U-2D reversal",
                                   "direction": "short", "outcome": outcome, "graded_at": "2026-10-20T16:15"})
    for outcome in ("hit stop", "hit stop", "hit stop"):
        db.insert("setup_grades", {"plan_id": 1, "symbol": "X", "timeframe": "daily", "pattern": "3-2U",
                                   "direction": "long", "outcome": outcome, "graded_at": "2026-10-20T16:15"})
    no_inside = [dict(c) for c in CANDS[1:]]
    no_inside[1]["with_continuity"] = True
    ranked = tf.rank(no_inside, tf.hit_rates(db))
    assert ranked[0]["symbol"] == "NQ"
    assert tf.rank([dict(c) for c in CANDS], tf.hit_rates(db))[0]["symbol"] == "AAPL"  # inside bars still first
