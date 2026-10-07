import pytest

from amo import market, strat
from amo.market import Bar


def B(h, l, o=None, c=None, t=0):  # noqa: E741
    o = (h + l) / 2 if o is None else o
    c = (h + l) / 2 if c is None else c
    return Bar(t, o, h, l, c)


def test_universal_truth_1_scenarios():
    prev = B(110, 100)
    assert strat.scenario(prev, B(108, 102)) == "1"      # inside
    assert strat.scenario(prev, B(112, 101)) == "2U"     # takes the high only
    assert strat.scenario(prev, B(109, 98)) == "2D"      # takes the low only
    assert strat.scenario(prev, B(111, 99)) == "3"       # takes both
    # "The break decides the number, never the color": a red bar that took the high is still 2U
    assert strat.scenario(prev, B(112, 101, o=111, c=101.5)) == "2U"
    assert strat.scenario(prev, B(110, 100)) == "1"      # equal high/low is not a break


def test_2_1_2_setup_trigger_target_stop():
    bars = [B(105, 95), B(110, 96), B(108, 99), B(108.5, 99.5)]  # x, 2U, 1, live
    setups = strat.find_setups("D", bars, strat.label(bars))
    longs = [s for s in setups if s.direction == "long"]
    shorts = [s for s in setups if s.direction == "short"]
    assert longs[0].name == "2U-1-2U continuation" and longs[0].trigger == 108 and longs[0].target == 110
    assert longs[0].stop == 99 and longs[0].status == "triggered"
    assert shorts[0].name == "2U-1-2D reversal" and shorts[0].trigger == 99 and shorts[0].status == "waiting"


def test_3_1_2_and_2_2_reversal():
    bars = [B(105, 95), B(110, 90), B(107, 93), B(106, 94)]   # 3 then inside
    names = {s.name for s in strat.find_setups("W", bars, strat.label(bars))}
    assert {"3-1-2U", "3-1-2D"} <= names
    bars = [B(105, 95), B(104, 92), B(103, 90), B(103.5, 91)]  # 2D, 2D, live
    long = next(s for s in strat.find_setups("D", bars, strat.label(bars)) if s.direction == "long")
    assert long.name == "2D-2D-2U reversal" and long.trigger == 103 and long.target == 104


def test_hammer_detected():
    assert strat.candle_shape(B(100, 90, o=99, c=99.5)) == "hammer"
    assert strat.candle_shape(B(100, 90, o=91, c=90.5)) == "shooter"
    assert strat.candle_shape(B(100, 90, o=91, c=99)) is None


def test_weekly_monthly_built_from_daily():
    day = 86400
    # Mon 2026-10-05 … Fri 2026-10-09, then Mon 2026-10-12
    start = 1791158400  # 2026-10-05 00:00 UTC
    daily = [Bar(start + i * day, 10 + i, 12 + i, 9 + i, 11 + i) for i in range(5)] + \
            [Bar(start + 7 * day, 20, 22, 19, 21)]
    weeks = market.aggregate(daily, "W")
    assert len(weeks) == 2
    assert (weeks[0].o, weeks[0].h, weeks[0].l, weeks[0].c) == (10, 16, 9, 15)
    assert market.aggregate(daily, "M")[0].o == 10 and len(market.aggregate(daily, "M")) == 1


@pytest.fixture()
def fake_market(monkeypatch):
    """Rising market: every timeframe trading above its open → full continuity up,
    with a daily inside bar after a 2U (2-1-2 setup waiting)."""
    def bars_for(tf):
        base = [B(100 + i, 90 + i, o=95 + i, c=99 + i, t=i) for i in range(30)]
        if tf == "D":
            base[-3] = B(140, 125, o=126, c=139, t=27)    # 2U
            base[-2] = B(138, 128, o=129, c=137, t=28)    # 1 (inside)
            base[-1] = B(137.5, 129, o=129.5, c=136, t=29)  # live, still inside, above its open
        return base

    monkeypatch.setattr(strat, "lookup", lambda s: s.upper())
    monkeypatch.setattr(strat, "candles", lambda sym, tf: (bars_for(tf), {"shortName": sym}))


def test_full_report_and_take(fake_market):
    r = strat.report("TEST")
    assert r["continuity"]["ftfc"] == "up" and r["bias"] == "bullish"
    assert "FULL TIMEFRAME CONTINUITY UP" in r["thesis"]
    assert r["take"]["best"]["name"] == "2U-1-2U continuation"
    assert r["take"]["best"]["trigger"] == 138 and r["take"]["grade"] in ("A", "B")
    assert "Not financial advice" in r["thesis"]
    assert r["summary"].startswith("TEST is at")


def test_compare_picks_best(fake_market, monkeypatch):
    c = strat.compare(["AAA", "BBB"])
    assert c["symbols"] == ["AAA", "BBB"] and "My pick" in c["text"]


def test_questions_route_to_strat_without_the_model(db, llm, fake_market):
    from amo.agent import Agent

    out = Agent(db).chat([{"role": "user", "content": "what's your input on AMZN or TSLA setups?"}])
    assert llm.calls == [] and "My pick" in out["content"]
    out = Agent(db).chat([{"role": "user", "content": "how does NVDA look"}], channel="voice")
    assert llm.calls == [] and out["content"].startswith("NVDA is at")


def test_web_search_parses_duckduckgo(monkeypatch):
    import httpx

    from amo import web

    page = '''<div class="result results_links results_links_deep web-result ">
      <h2 class="result__title"><a rel="nofollow" class="result__a" href="https://thestrat.ai/docs/ftfc/">Full Timeframe Continuity (FTFC) - <b>TheStrat</b></a></h2>
      <a class="result__snippet" href="https://thestrat.ai/docs/ftfc/">FTFC means the last <b>open</b> of every timeframe</a></div>
      <div class="result results_links results_links_deep web-result ">
      <h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fx&amp;rut=1">Example</a></h2></div>'''
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, text=page, request=httpx.Request("POST", "https://x")))
    web._cache.clear()
    res = web.search("ftfc")
    assert res[0] == {"title": "Full Timeframe Continuity (FTFC) - TheStrat", "url": "https://thestrat.ai/docs/ftfc/",
                      "snippet": "FTFC means the last open of every timeframe"}
    assert res[1]["url"] == "https://example.com/x"
