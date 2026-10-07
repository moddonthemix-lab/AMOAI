from datetime import date

import pytest

from modd.db import Store


@pytest.fixture
def db():
    return Store()


def test_memory_recall_by_keyword(db):
    db.remember("Jay prefers late-night sessions after 10pm", area="studio")
    db.remember("Cravvr launch target is November", area="cravvr")
    hits = db.recall("when does jay like sessions")
    assert hits and "Jay" in hits[0]["content"]
    assert db.recall("cravvr launch", area="studio") == []
    assert len(db.recall("")) == 2  # no query -> most recent


def test_forget_removes_from_search(db):
    mid = db.remember("secret thing")
    assert db.forget(mid)
    assert db.recall("secret") == []


def test_recall_tolerates_fts_syntax(db):
    db.remember("AAPL breakout setup")
    assert db.recall('AAPL" OR (* NEAR') != []


def test_clients_and_sessions(db):
    db.add_client("Jay", phone="555")
    db.add_client("jay", email="j@x.com", notes="vocals")  # upsert, case-insensitive
    c = db.get_client("JAY")
    assert c["phone"] == "555" and c["email"] == "j@x.com" and "vocals" in c["notes"]

    sid = db.book_session("Jay", "2026-10-07T18:00", hours=3, rate=50)
    db.book_session("New Artist", "2026-10-08T12:00")  # auto-creates client
    assert len(db.list_clients()) == 2

    db.update_session(sid, status="done", paid=100)
    unpaid = db.unpaid_sessions()
    assert [s["id"] for s in unpaid] == [sid] and unpaid[0]["amount_due"] == 150
    db.update_session(sid, paid=150)
    assert db.unpaid_sessions() == []


def test_reselling_profit(db):
    iid = db.add_item("Jordan 4 Bred", cost=210, list_price=320)
    assert db.get_item(iid)["status"] == "listed"
    item = db.sell_item(iid, 300, fees=30, shipping=15, platform="stockx")
    assert item["status"] == "sold" and item["profit"] == 45
    assert db.items("sold")[0]["profit"] == 45


def test_trading_pnl_and_stats(db):
    db.log_trade("aapl", 10, 100, exit=110, fees=2, followed_rules=True)      # +98
    db.log_trade("TSLA", 5, 200, side="short", exit=210, followed_rules=False)  # -50
    open_id = db.log_trade("NVDA", 1, 500)
    assert db.get_trade(open_id)["pnl"] is None
    assert [t["id"] for t in db.trades(open_only=True)] == [open_id]

    s = db.trade_stats()
    assert s["n"] == 2 and s["wins"] == 1 and s["win_rate"] == 0.5
    assert s["total"] == 48 and s["rule_breaks"] == 1 and s["pnl_rule_breaks"] == -50

    assert db.close_trade(open_id, 520)["pnl"] == 20
    with pytest.raises(ValueError):
        db.log_trade("X", 1, 1, side="sideways")


def test_goals(db):
    gid = db.add_goal("Mix 2 songs", area="studio")
    assert db.goals()[0]["done"] == 0
    db.complete_goal(gid)
    assert db.goals()[0]["done"] == 1
    db.add_goal("tomorrow thing", day="2099-01-01")
    assert len(db.goals()) == 1


def test_revenue_and_dashboard(db):
    today = date.today().isoformat()
    sid = db.book_session("Jay", f"{today}T18:00", hours=2, rate=60)
    db.update_session(sid, "done", 120)
    iid = db.add_item("Hoodie", 20)
    db.sell_item(iid, 70, fees=5, shipping=5)
    db.log_trade("SPY", 1, 400, exit=410)

    r = db.revenue_for("month")
    assert r["studio"] == 120 and r["reselling"] == 40 and r["trading"] == 10 and r["total"] == 170
    d = db.dashboard()
    assert d["revenue_today"]["total"] == 170
    assert len(d["sessions_today"]) == 1 and d["inventory_count"] == 0
