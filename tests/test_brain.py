"""Two-tier brain: each kind of request goes to the model that's best at it."""

import json
from types import SimpleNamespace

from amo import brain, cli, evals
from amo.agent import Agent
from amo.config import settings


def test_kinds():
    assert brain.kind("hey") == "chat"
    assert brain.kind("what's the capital of France") == "chat"
    assert brain.kind("what's my schedule tomorrow") == "lookup"
    assert brain.kind("who's Marcus") == "lookup"
    assert brain.kind("book Ty tomorrow at 2pm") == "action"
    assert brain.kind("Lil Jay paid 150 cash app") == "action"
    assert brain.kind("yes", previous="book Jay Friday") == "action"  # a "yes" continues the request


def test_each_kind_goes_to_its_model(db, llm, monkeypatch):
    monkeypatch.setattr(settings, "model_chat", "fast")
    monkeypatch.setattr(settings, "model_action", "careful")
    llm.script({"role": "assistant", "content": "Hey!"}, {"role": "assistant", "content": "Okay."})
    Agent(db).chat([{"role": "user", "content": "hey"}])
    assert llm.calls[-1]["model"] == "fast"
    Agent(db).chat([{"role": "user", "content": "move Jay to Saturday at 6pm"}])
    assert llm.calls[-1]["model"] == "careful"
    assert db.scalar("SELECT model FROM timings ORDER BY id DESC LIMIT 1") == "careful"
    assert brain.model_for("lookup") == settings.chat_model  # not set → the main model


def test_amo_brain_auto_sets_the_split(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setenv("AMO_ENV_FILE", str(env))
    monkeypatch.setattr(evals, "RESULTS", tmp_path)
    monkeypatch.setattr(cli, "_restart_server", lambda: None)
    monkeypatch.setattr(cli, "_installed_models", lambda: None)
    for k in ("chat", "lookup", "action"):
        monkeypatch.setattr(settings, f"model_{k}", "")

    def res(kind, ok, ms):
        return {"kind": kind, "ok": ok, "first_ms": ms, "total_ms": ms, "path": "model", "tags": ["ai"],
                "why": [] if ok else ["x"]}

    for name, ms, action_ok in (("big", 20000, True), ("small", 6000, False)):
        rs = [res("chat", True, ms)] * 5 + [res("lookup", True, ms)] * 5 + [res("action", action_ok, ms)] * 5
        (tmp_path / f"results-{name}.json").write_text(json.dumps({"model": name, "results": rs}))
    cli.cmd_brain(SimpleNamespace(off=False, auto=True, chat=None, lookup=None, action=None))
    text = env.read_text()
    assert "AMO_MODEL_CHAT=small" in text and "AMO_MODEL_LOOKUP=small" in text and "AMO_MODEL_ACTION=big" in text
    cli.cmd_brain(SimpleNamespace(off=True, auto=False, chat=None, lookup=None, action=None))
    assert "AMO_MODEL_ACTION=\n" in env.read_text() and not brain.two_tier()


def test_too_few_cases_keep_the_current_model(monkeypatch):
    monkeypatch.setattr(settings, "model_action", "careful")

    def res(kind, ok, ms):
        return {"kind": kind, "ok": ok, "first_ms": ms, "total_ms": ms, "path": "model", "tags": ["ai"], "why": []}

    a = {"model": "careful", "results": [res("action", True, 18000)] * 2}
    b = {"model": "fast", "results": [res("action", True, 9000)] * 2}
    rec = brain.recommend([a, b])
    assert rec["split"]["action"] == "careful" and rec["kept"] == ["action"]


def test_market_questions_go_to_the_tool_model():
    for t in ("what are the strat levels for NQ", "how's NQ looking", "break down NQ for me", "PLTR?", "bitcoin"):
        assert brain.kind(t) != "chat", t
    for t in ("hey", "tell me a joke", "I'm tired today", "what's the capital of France"):
        assert brain.kind(t) == "chat", t


def test_strat_phrasings_answer_without_the_ai():
    from amo import fastpath

    for t, sym in (("what are the strat levels for NQ", "NQ"), ("how's NQ looking", "NQ"), ("break down NQ for me", "NQ"),
                   ("what are the triggers on BTC", "BTC"), ("PLTR levels", "PLTR"), ("NQ strat levels", "NQ")):
        assert fastpath.match(t) == ("strat_analysis", {"symbol": sym}), t
    assert fastpath.match("break down my week") is None
