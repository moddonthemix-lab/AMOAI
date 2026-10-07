from datetime import timedelta

import pytest

from amo.crm import StudioCRM
from amo.db import today
from amo.finance import Finance, dashboard
from amo.goals import Goals
from amo.reselling import Reselling
from amo.trading import TradingJournal


def test_crm_booking_payment_balance(db):
    crm = StudioCRM(db)
    jay = crm.add_client("Jay Carter", artist_name="Lil Jay", phone="555-0101")
    crm.add_client("Jayla Smith")
    tomorrow = (today() + timedelta(days=1)).isoformat()
    s = crm.book_session("Lil Jay", f"{tomorrow} 19:00", hours=3, rate=50)
    assert s["client_name"] == "Jay Carter" and s["total"] == 150
    assert len(crm.upcoming(7)) == 1

    with pytest.raises(ValueError, match="ambiguous"):
        crm.book_session("Jay", f"{tomorrow} 12:00")

    crm.complete_session(s["id"], paid=100, method="cashapp")
    c = crm.get_client(jay["id"])
    assert c["total_paid"] == 100 and c["balance_due"] == 50
    assert crm.unpaid_sessions()[0]["paid"] == 100


def test_reselling_profit(db):
    r = Reselling(db)
    r.add_item("Jordan 4 Bred sz10", cost=210, platform="stockx")
    r.add_item("PS5 Slim", cost=350)
    sold = r.mark_sold("Jordan 4", 320, fees=30, shipping=15)
    assert sold["profit"] == 65 and sold["status"] == "sold"
    s = r.summary()
    assert s["sold"] == 1 and s["profit"] == 65
    assert s["inventory"]["items"] == 1 and s["inventory"]["capital_tied_up"] == 350


def test_trading_stats_and_rules(db):
    tj = TradingJournal(db)
    a = tj.open_trade("spy", "long", 500, quantity=10, stop=498, setup="ORB")
    tj.close_trade(a["id"], 504, followed_rules=True)
    b = tj.open_trade("NQ", "short", 18000, quantity=1, stop=18020)
    b = tj.close_trade(b["id"], 18030, followed_rules=False, emotion="revenge")
    assert b["pnl"] == -30 and b["r_multiple"] == -1.5
    st = tj.stats()
    assert st["trades"] == 2 and st["net_pnl"] == 10 and st["win_rate"] == 0.5
    assert st["rule_adherence"] == 0.5 and st["pnl_when_rules_broken"] == -30
    assert st["by_setup"]["ORB"]["pnl"] == 40
    tj.add_rule("Max 3 trades per day")
    assert [r["rule"] for r in tj.rules()] == ["Max 3 trades per day"]


def test_goals_streak(db):
    g = Goals(db)
    goal = g.add("Make 1 beat", area="studio")
    for d in range(3):
        g.check_in(goal["id"], day=(today() - timedelta(days=d + 1)).isoformat())
    assert g.get(goal["id"])["streak"] == 3  # today not done yet doesn't break it
    g.check_in("beat")
    got = g.get(goal["id"])
    assert got["streak"] == 4 and got["done_today"]


def test_revenue_and_dashboard(db):
    crm = StudioCRM(db)
    crm.add_client("Ana")
    crm.record_payment(200, client="Ana")
    crm.record_payment(75, business="cravvr")
    Reselling(db).add_item("Hoodie", cost=20)
    Reselling(db).mark_sold("Hoodie", 60)
    rev = Finance(db).revenue("month")
    assert rev["by_business"]["studio"] == 200
    assert rev["by_business"]["cravvr"] == 75
    assert rev["by_business"]["reselling"] == 40
    assert rev["total"] == 315
    d = dashboard(db)
    assert d["revenue"]["today"] == 315 and len(d["revenue"]["spark"]) == 14
