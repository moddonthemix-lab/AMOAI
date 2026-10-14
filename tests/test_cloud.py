"""Cloud AI through the Family Hub's shared brain — with a fake Claude and a fake hub, so it costs $0."""

import logging
from types import SimpleNamespace

import pytest

from amo import cloud, perf
from amo.agent import Agent

KEY = "sk-ant-test-0000-never-show-me"


class FakeHub:
    def __init__(self, allowed=True, reason=""):
        self.allowed, self.reason = allowed, reason
        self.checks, self.usage = [], []

    def budget_check(self, estimate):
        self.checks.append(estimate)
        return {"allowed": self.allowed, "reason": self.reason}

    def report_usage(self, model, input_tokens, output_tokens, cost_usd, note=None):
        self.usage.append({"model": model, "in": input_tokens, "out": output_tokens, "cost": cost_usd, "note": note})


class FakeClaude:
    def __init__(self, text="Compound interest is interest on your interest.", stop="end_turn", error=None):
        self.text, self.stop, self.error = text, stop, error
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return SimpleNamespace(
            model=kw["model"], stop_reason=self.stop,
            stop_details=SimpleNamespace(explanation="That's outside what I can help with.") if self.stop == "refusal" else None,
            usage=SimpleNamespace(input_tokens=1200, output_tokens=80, cache_creation_input_tokens=0,
                                  cache_read_input_tokens=0, server_tool_use=None),
            content=[SimpleNamespace(type="text", text=self.text)])


@pytest.fixture()
def family(monkeypatch):
    """Cloud switched on, with fakes in place of Claude and the hub."""
    monkeypatch.setenv("CLOUD_FOR_PRIVATE_CHATS", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("HUB_TOKEN", "hub_amo_test")
    hub, claude = FakeHub(), FakeClaude()

    def use(hub=hub, claude=claude):
        monkeypatch.setattr(cloud, "_brain", cloud._make_brain(hub, cloud_client=claude, internet_check=lambda: True))
        return hub, claude

    use()
    yield SimpleNamespace(hub=hub, claude=claude, use=use)


def ask(db, text, history=()):
    return Agent(db).chat([*history, {"role": "user", "content": text}])


def test_off_unless_everything_is_set(monkeypatch):
    assert not cloud.possible()
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("HUB_TOKEN", "hub_amo_test")
    assert not cloud.possible()  # CLOUD_FOR_PRIVATE_CHATS defaults to off
    monkeypatch.setenv("CLOUD_FOR_PRIVATE_CHATS", "true")
    assert cloud.possible()
    monkeypatch.delenv("HUB_TOKEN")
    assert not cloud.possible()  # no hub → no budget check → no cloud


def test_private_chats_stay_local_when_the_switch_is_off(db, llm, family, monkeypatch):
    monkeypatch.setenv("CLOUD_FOR_PRIVATE_CHATS", "false")
    llm.script({"role": "assistant", "content": "Local answer."})
    out = ask(db, "explain how compound interest works")
    assert out["content"] == "Local answer." and "brain" not in out
    assert family.claude.calls == [] and family.hub.checks == []


def test_chat_goes_to_the_cloud_and_the_cost_is_reported(db, llm, family):
    history = [{"role": "user", "content": "hey"}, {"role": "assistant", "content": "Hey Modd."}]
    out = ask(db, "explain how compound interest works", history)
    assert out == {"content": "Compound interest is interest on your interest.", "tool_calls": [], "brain": "cloud"}
    assert llm.calls == []  # the local model wasn't needed
    call = family.claude.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert "family's cloud brain" in call["system"] and "100% locally" not in call["system"]
    assert call["messages"][:2] == history and call["messages"][-1]["content"].endswith("compound interest works")
    assert family.hub.usage[0]["model"] == "claude-haiku-5-5" and family.hub.usage[0]["cost"] > 0
    row = db.one("SELECT * FROM timings ORDER BY id DESC LIMIT 1")
    assert (row["path"], row["brain"], row["model"]) == ("cloud", "cloud", "claude-haiku-5-5")
    assert row["cost_usd"] == family.hub.usage[0]["cost"]
    assert "☁️ cloud 1×" in perf.report_text(db)


def test_no_budget_left_means_the_usual_local_answer(db, llm, family):
    family.use(hub=FakeHub(allowed=False, reason="monthly cloud budget used up"))
    llm.script({"role": "assistant", "content": "Local answer."})
    out = ask(db, "explain how compound interest works")
    assert out["content"] == "Local answer." and out["brain"] == "local"
    assert family.claude.calls == []
    assert db.scalar("SELECT brain FROM timings ORDER BY id DESC LIMIT 1") == "local"


def test_a_refusal_is_not_retried_locally(db, llm, family):
    family.use(claude=FakeClaude(stop="refusal"))
    out = ask(db, "explain how compound interest works")
    assert out["content"].startswith("I can't help with that one") and out["brain"] == "cloud"
    assert llm.calls == []


def test_saves_lookups_and_live_questions_stay_on_the_local_tools(db, llm, family):
    for text in ("book Ty tomorrow at 2pm", "what's my schedule tomorrow", "what's the weather today",
                 "who won the game last night", "what's NQ doing"):
        llm.script({"role": "assistant", "content": "Local."})
        out = ask(db, text)
        assert out.get("brain") == "local", text
    assert family.claude.calls == []


def test_coding_questions_use_the_code_job(db, llm, family):
    out = ask(db, "why does my python function return None instead of the list?")
    assert out["brain"] == "cloud" and llm.calls == []
    assert family.claude.calls[0]["model"] == "claude-sonnet-5-5"
    assert family.hub.usage[0]["note"] == "code"


def test_big_jobs_only_when_turned_on(db, llm, family, monkeypatch):
    assert cloud.job_for("big job: write my album rollout strategy", "chat")[0] == "chat"
    monkeypatch.setenv("AMO_CLOUD_BIG", "true")
    assert cloud.job_for("big job: write my album rollout strategy", "action") == (
        "big", "write my album rollout strategy")
    ask(db, "think hard about how to price my studio packages")
    assert family.claude.calls[0]["model"] == "claude-opus-5-5"


def test_cloud_trouble_falls_back_locally_and_never_shows_the_key(db, llm, family, caplog):
    family.use(claude=FakeClaude(error=RuntimeError(f"boom {KEY}")))
    llm.script({"role": "assistant", "content": "Local answer."})
    with caplog.at_level(logging.DEBUG):
        out = ask(db, "explain how compound interest works")
    assert out["content"] == "Local answer." and out["brain"] == "local"
    assert KEY not in caplog.text and "boom" not in caplog.text
    assert KEY not in out["content"]


def test_history_alternates_and_starts_with_you():
    h = [{"role": "assistant", "content": "Morning!"}, {"role": "user", "content": "a"},
         {"role": "assistant", "content": "", "tool_calls": [{}]}, {"role": "tool", "content": "{}"},
         {"role": "user", "content": "b"}, {"role": "assistant", "content": "c"}, {"role": "user", "content": "d"}]
    assert cloud.history_for(h) == [{"role": "user", "content": "a\n\nb"}, {"role": "assistant", "content": "c"}]


def test_the_shared_files_are_untouched():
    from amo.family import family_brain

    assert family_brain.MODELS["chat"] == "claude-haiku-5-5"  # copied as-is from the hub repo
