"""Everyday requests about your own data, done without the AI (built from what it got wrong)."""

import pytest

from amo import evals, shortcuts, tools
from amo.agent import Agent
from amo.db import set_db


@pytest.fixture()
def seeded():
    db = evals._seeded()
    set_db(db)
    return db


@pytest.mark.parametrize("said, tool", [
    ("I finished my beat today", "check_in_goal"),
    ("done with the gym", "check_in_goal"),
    ("can you put calling my mom on my daily goals", "add_goal"),
    ("rename my gym goal to gym 5 days a week", "update_record"),
    ("Lil Jay paid 150 cash app", "record_payment"),
    ("log that Ty paid me 100 in cash", "record_payment"),
    ("book Lil Jay Friday at 7pm for 3 hours at 50 an hour", "book_session"),
    ("move Lil Jay's session to Saturday at 6pm", "update_session"),
    ("cancel Lil Jay's session", "update_session"),
    ("bought Jordan 1 Chicago size 10 for 180", "add_resale_item"),
    ("sold the Jordan 4s on StockX for 320 with 30 in fees", "mark_item_sold"),
    ("listed the Jordan 4s on eBay for 299", "list_resale_item"),
    ("long TSLA at 250 stop 245 target 265", "log_trade"),
    ("closed my NQ trade at 18040", "close_trade"),
    ("add a rule: no trades in the first 5 minutes", "add_trading_rule"),
    ("add a cravvr task: call the food supplier, high priority", "add_cravvr_task"),
    ("what are my goals", "list_goals"),
    ("who owes me money", "unpaid_sessions"),
    ("what's my schedule tomorrow", "studio_schedule"),
    ("what's in my inventory", "resale_inventory"),
    ("what are my open positions", "open_positions"),
    ("what are my trading rules", "trading_rules"),
    ("what's open for cravvr", "cravvr_tasks"),
    ("how much did I make this month", "revenue"),
])
def test_recognised(seeded, said, tool):
    assert shortcuts.match(said, seeded)[0] == tool


@pytest.mark.parametrize("said", [
    "I bought lunch for 15", "I bought a new mic for 200", "long day at work", "I hit my head",
    "Mom paid 50 for dinner", "book a flight to Miami tomorrow at 5", "cancel my gym membership",
    "what am I holding", "what are my options", "closed the door at 5", "sold out",
])
def test_everyday_sentences_left_alone(seeded, said):
    assert shortcuts.match(said, seeded) is None


def test_done_and_said_without_the_ai(seeded, llm):
    a = Agent(seeded)
    assert a.chat([{"role": "user", "content": "done with the gym"}])["content"].startswith("Checked in")
    out = a.chat([{"role": "user", "content": "what are my goals"}])["content"]
    assert out.startswith("Your goals:") and "Gym" in out and "done today" in out
    assert "Lil Jay" in a.chat([{"role": "user", "content": "what's my schedule tomorrow"}])["content"]
    a.chat([{"role": "user", "content": "sold the Jordan 4s on StockX for 320 with 30 in fees"}])
    assert seeded.scalar("SELECT status FROM resale_items WHERE id = 1") == "sold"
    assert llm.calls == []


def test_junk_arguments_from_the_ai_are_dropped(seeded):
    import json

    r = json.loads(tools.call("record_payment", {"amount": "150 dollars", "client": "Jay Carter", "paid_at": "null"}))
    assert r.get("amount") == 150 and "error" not in r
    r = json.loads(tools.call("record_payment", {"amount": "null"}))
    assert "missing required" in r["error"]  # not a crash
