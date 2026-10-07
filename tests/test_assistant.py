import json

from modd import tools
from modd.assistant import Assistant
from modd.config import Config
from modd.db import Store


class FakeLLM:
    """Replays scripted assistant messages and records what it was sent."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append(json.loads(json.dumps(messages)))
        return self.script.pop(0)


def tool_call(name, **args):
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]}


def test_tool_loop_writes_to_db():
    db = Store()
    llm = FakeLLM([
        tool_call("log_trade", symbol="aapl", qty=10, entry=100, exit=105, followed_rules=False),
        {"role": "assistant", "content": "Logged. +$50, but you broke your rules."},
    ])
    a = Assistant(db, llm, Config())
    reply = a.ask("I bought 10 AAPL at 100 and sold at 105, ignored my stop")
    assert reply.startswith("Logged")
    assert db.trades()[0]["pnl"] == 50
    tool_msg = llm.calls[1][-1]
    assert tool_msg["role"] == "tool" and json.loads(tool_msg["content"])["symbol"] == "AAPL"
    # tool traffic stays out of rolling history
    assert [m["role"] for m in a.history] == ["user", "assistant"]


def test_system_prompt_includes_memories_and_rules():
    db = Store()
    db.remember("Jay pays in cash only", area="studio")
    db.add_rule("Max 2% risk per trade")
    llm = FakeLLM([{"role": "assistant", "content": "ok"}])
    Assistant(db, llm, Config()).ask("how does Jay pay?")
    system = llm.calls[0][0]["content"]
    assert "Jay pays in cash only" in system and "Max 2% risk" in system


def test_tool_errors_are_returned_not_raised():
    db = Store()
    assert "error" in json.loads(tools.call(db, "nope", {}))
    assert "error" in json.loads(tools.call(db, "log_trade", {"symbol": "X"}))  # missing args
    assert "error" in json.loads(tools.call(db, "remember", "{bad json"))
    assert json.loads(tools.call(db, "remember", '{"content": "hi"}'))["saved_id"] == 1


def test_every_tool_has_valid_schema():
    for schema in tools.schemas():
        fn = schema["function"]
        assert fn["name"] and fn["description"]
        assert set(fn["parameters"]["required"]) <= set(fn["parameters"]["properties"])
