import json

from conftest import tool_call

from amo import tools
from amo.agent import Agent
from amo.crm import StudioCRM
from amo.learning import learn_from_conversations, weekly_reflection
from amo.memory import Memory


def test_memory_search_and_dedupe(db, llm):
    mem = Memory(db)
    mem.add("My studio rate is $50/hour, $45 for regulars", "studio", 5)
    mem.add("Cravvr launch target is spring", "cravvr")
    dup = mem.add("my studio rate is $50/hour, $45 for regulars.", "studio")
    assert dup["duplicate"]
    hits = mem.search("what's my hourly studio rate?")
    assert hits and "rate" in hits[0]["content"]
    assert mem.core()[0]["importance"] == 5


def test_agent_runs_tools_and_injects_memory(db, llm):
    Memory(db).add("Never trade the first 5 minutes after the open", "trading", 5)
    StudioCRM(db).add_client("Jay Carter")
    llm.script(
        tool_call("book_session", client="Jay", starts_at="2030-01-04 19:00", hours="3", rate="$50"),
        {"role": "assistant", "content": "Booked Jay for Friday 7pm, 3 hours."},
    )
    out = Agent(db).chat([{"role": "user", "content": "book jay friday 7pm 3 hours at 50"}])
    assert out["content"].startswith("Booked")
    assert out["tool_calls"][0]["result"]["total"] == 150  # string args were coerced
    system = llm.calls[0]["messages"][0]["content"]
    assert "Never trade the first 5 minutes" in system
    assert db.scalar("SELECT COUNT(*) FROM conversations") == 2


def test_tool_errors_are_returned_not_raised(db, llm):
    out = json.loads(tools.call("book_session", {"client": "Nobody", "starts_at": "2030-01-01 10:00"}))
    assert "no client" in out["error"]
    out = json.loads(tools.call("book_session", {"client": "x"}))
    assert "missing" in out["error"]
    assert all(s["function"]["name"] in tools.REGISTRY for s in tools.schemas())


def test_learning_extracts_facts(db, llm):
    Agent(db).chat([{"role": "user", "content": "My engineer Marcus works Tuesdays"}], learn=False)
    llm.script({"role": "assistant", "content": json.dumps({"facts": [
        {"content": "Marcus is the studio engineer and works Tuesdays", "category": "person", "importance": 4},
    ]})})
    added = learn_from_conversations(db)
    assert len(added) == 1 and added[0]["category"] == "person"
    assert db.scalar("SELECT COUNT(*) FROM conversations WHERE learned = 0") == 0
    assert learn_from_conversations(db) == []  # nothing new to process


def test_weekly_reflection(db, llm):
    llm.script({"role": "assistant", "content": json.dumps({
        "summary": "- Studio was slow\n- Focus on follow-ups",
        "insights": ["Mondays are the slowest studio day"]})})
    r = weekly_reflection(db)
    assert "Studio was slow" in r["summary"]
    assert Memory(db).search("slowest studio day")
    # The reflection is fed back into future conversations.
    assert "Focus on follow-ups" in Agent(db).build_system_prompt("hi")
