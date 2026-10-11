"""The test set runner, the mistake log, and model comparison."""

import json

from amo import evals, selfreview
from amo.agent import Agent
from conftest import tool_call


def test_cases_run_in_a_throwaway_database(db, llm):
    before = db.scalar("SELECT COUNT(*) FROM goals")
    cases = [evals.Case(say="add a daily goal: drink water", tool=["add_goal"]),
             evals.Case(say="what's 12 times 12", path="instant", reply_has=["144"]),
             evals.Case(say="delete the gym goal", tool=["delete_record"],
                        sql="SELECT COUNT(*) FROM goals WHERE active = 1 AND title = 'Gym'", sql_equals=0)]
    s = evals.run(cases=cases)
    assert s["passed"] == 3 and s["score"] == 100.0
    assert db.scalar("SELECT COUNT(*) FROM goals") == before  # your data untouched


def test_model_cases_and_failure_reasons(db, llm):
    llm.script(tool_call("add_goal", title="Call mom"), {"role": "assistant", "content": "Added."})
    llm.script({"role": "assistant", "content": "Sure, I've added that for you!"},  # claims, never saves
               {"role": "assistant", "content": "Done!"})
    cases = [evals.Case(say="I want to call my mom more, make it a daily thing", tool=["add_goal"], args={"title": "mom"},
                        tags=["ai"]),
             evals.Case(say="I want to stretch more, track that for me", tool=["add_goal"], tags=["ai"])]
    s = evals.run(cases=cases)
    assert [r["ok"] for r in s["results"]] == [True, False]
    assert "expected add_goal, got no tool" in s["failures"][0]["why"][0]
    assert s["ai_score"] == 50.0
    assert "1/2 passed" in evals.text(s)


def test_built_in_cases_load_and_deterministic_ones_pass(db, llm):
    cases = evals.builtin_cases()
    assert len(cases) >= 70 and all(c.say for c in cases)
    instant = [c for c in cases if c.path == "instant" or "timers" in c.tags or "undo" in c.tags]
    s = evals.run(cases=instant)
    assert s["score"] == 100.0, [f["say"] + str(f["why"]) for f in s["failures"]]


def test_your_past_requests_become_regression_cases(db, llm):
    a = Agent(db)
    a.chat([{"role": "user", "content": "add a daily goal: drink water"}])
    a.chat([{"role": "user", "content": "what's 2 plus 2"}])  # no tool → not a case
    mine = evals.mine_cases(db)
    assert [(c.say, c.tool) for c in mine] == [("add a daily goal: drink water", ["add_goal"])]
    s = evals.run(cases=mine)  # replayed on a copy of your data
    assert s["passed"] == 1


def test_add_your_own_case(tmp_path, monkeypatch):
    monkeypatch.setattr(evals, "CUSTOM", tmp_path / "my_cases.json")
    evals.add_custom("put calling mom on my goals", tool="add_goal")
    assert json.loads((tmp_path / "my_cases.json").read_text())[0]["tool"] == ["add_goal"]
    assert evals.custom_cases()[0].source == "custom"


def _res(kind, ok, ms, crashed=False):
    return {"kind": kind, "ok": ok, "first_ms": ms, "total_ms": ms, "path": "model", "tags": ["ai"],
            "why": ["crashed: LLMError: The AI model 'q' isn't downloaded yet."] if crashed else ([] if ok else ["x"])}


def test_compare_picks_the_best_model_and_a_two_tier_split():
    # big: right on everything, slow.  small: as good at chat and lookups, worse at saving things, fast.
    big = [_res("chat", True, 20000)] * 4 + [_res("lookup", True, 22000)] * 4 + [_res("action", True, 25000)] * 4
    small = ([_res("chat", True, 6000)] * 4 + [_res("lookup", True, 7000)] * 4
             + [_res("action", True, 8000)] * 2 + [_res("action", False, 8000)] * 2)
    broken = [_res("chat", False, 100, crashed=True)] * 12
    a = {"model": "big", "score": 90.0, "ai_score": 100.0, "ai_first_median_ms": 22000, "total_median_ms": 9000,
         "ai_total_median_ms": 22000, "results": big}
    b = {"model": "small", "score": 88.0, "ai_score": 83.0, "ai_first_median_ms": 7000, "total_median_ms": 3000,
         "ai_total_median_ms": 7000, "results": small}
    c = {"model": "q", "score": 50.0, "ai_score": 0.0, "ai_first_median_ms": 100, "total_median_ms": 100,
         "ai_total_median_ms": 100, "results": broken}
    out = evals.compare([a, b, c])
    assert "One model: small" in out and "amo use small" in out
    assert "q " in out and "didn't run — LLMError: The AI model 'q' isn't downloaded yet." in out
    assert "chat → small, lookup → small, action → big" in out and "amo brain --auto" in out


def test_mistake_log(db, llm):
    a = Agent(db)
    a.chat([{"role": "user", "content": "add a daily goal: drink waiter"}])
    a.chat([{"role": "user", "content": "add a daily goal: drink waiter"},
            {"role": "assistant", "content": "New daily goal: Drink waiter."},
            {"role": "user", "content": "that's wrong"}])
    rows = selfreview.mistakes(db)
    assert rows[0]["kind"] == "wrong" and rows[0]["said"] == "add a daily goal: drink waiter"
    assert rows[0]["reply"] == "New daily goal: Drink waiter."
    assert "[you said wrong]" in selfreview.mistakes_text(db)


def test_duplicate_goals_are_not_created(db, llm):
    a = Agent(db)
    a.chat([{"role": "user", "content": "add a daily goal: make one beat"}])
    out = a.chat([{"role": "user", "content": "add a daily goal: Make one beat"}])["content"]
    assert "already have" in out and db.scalar("SELECT COUNT(*) FROM goals") == 1
