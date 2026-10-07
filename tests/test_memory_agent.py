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


def test_missing_model_error_is_helpful(monkeypatch):
    import httpx
    import pytest

    from amo.llm import LLMError, Ollama

    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(404, json={"error": "model not found"},
                                                                     request=httpx.Request("POST", "http://x")))
    with pytest.raises(LLMError, match="ollama pull llama3.2:3b"):
        Ollama("http://x").chat([{"role": "user", "content": "hi"}], model="llama3.2:3b")


def test_tool_routing_keeps_prompts_small():
    from amo.tools import route

    assert route("hey AMO") == ["remember", "recall"]
    studio = route("Book Lil Jay tomorrow at 7pm for 3 hours at $50")
    assert "book_session" in studio and "log_trade" not in studio
    assert "mark_item_sold" in route("sold the jordans on stockx for 320")
    assert "log_trade" in route("long NQ at 18000 stop 17980")
    assert "revenue" in route("how much did I make this month?")


def test_agent_sends_only_routed_tools(db, llm):
    Agent(db).chat([{"role": "user", "content": "hey"}], learn=False)
    sent = [t["function"]["name"] for t in llm.calls[0]["tools"]]
    assert sent == ["remember", "recall"]


def test_runtime_params(monkeypatch):
    from amo.config import settings
    from amo.llm import runtime_params

    p = runtime_params("llama3.2:3b", {"temperature": 0})
    assert p["options"] == {"num_ctx": settings.num_ctx, "temperature": 0} and "think" not in p
    assert runtime_params("gemma4:e2b")["think"] is False
    assert runtime_params("qwen3:4b")["think"] is False


def test_set_env(tmp_path, monkeypatch):
    from amo.cli import _set_env

    env = tmp_path / ".env"
    env.write_text("AMO_CHAT_MODEL=llama3.2:3b\nAMO_API_KEY=\n")
    monkeypatch.setenv("AMO_ENV_FILE", str(env))
    _set_env("AMO_CHAT_MODEL", "gemma4:e2b")
    _set_env("AMO_THINK", "0")
    assert env.read_text() == "AMO_CHAT_MODEL=gemma4:e2b\nAMO_API_KEY=\nAMO_THINK=0\n"
