"""Interrupting / correcting AMO: undo what it saved from a misheard request."""

from amo.agent import Agent
from amo.fastpath import correction
from conftest import tool_call


def _goals(db):
    return [g["title"] for g in db.all("SELECT title FROM goals WHERE active = 1")]


def test_correction_phrases():
    assert correction("No, I said book Jay on Thursday") == "book Jay on Thursday"
    assert correction("you misheard me") == ""
    assert correction("That's not what I said, I said Thursday") == "Thursday"
    assert correction("I meant Tesla") == "Tesla"
    assert correction("I meant to tell you about Jay") is None  # a new request, not a correction
    assert correction("I said I'd make three beats") is None    # needs a "no" / "wait" first
    assert correction("add a goal") is None


def test_undo_that(db, llm):
    a = Agent(db)
    a.chat([{"role": "user", "content": "add a daily goal: make one bet"}])
    assert _goals(db) == ["Make one bet"]
    out = a.chat([{"role": "user", "content": "undo that"}])
    assert _goals(db) == [] and out["content"].startswith("Undid that") and "Make one bet" in out["content"]
    assert a.chat([{"role": "user", "content": "undo that"}])["content"] == "There's nothing recent to undo."


def test_no_i_said_undoes_and_redoes(db, llm):
    a = Agent(db)
    a.chat([{"role": "user", "content": "add a daily goal: make one bet"}])
    out = a.chat([{"role": "user", "content": "add a daily goal: make one bet"},
                  {"role": "assistant", "content": "New daily goal: Make one bet."},
                  {"role": "user", "content": "No, I said add a daily goal: make one beat"}])
    assert _goals(db) == ["Make one beat"]
    assert out["content"].startswith("Undid that") and "Make one beat" in out["content"]


def test_short_correction_goes_to_model_with_context(db, llm):
    a = Agent(db)
    a.chat([{"role": "user", "content": "remember that Jay likes Fridays"}])  # done directly, no model
    assert db.scalar("SELECT COUNT(*) FROM memories WHERE archived = 0") == 1
    llm.script(tool_call("remember", content="Jay likes Thursdays"),
               {"role": "assistant", "content": "Noted."})
    out = a.chat([{"role": "user", "content": "remember that Jay likes Fridays"},
                  {"role": "assistant", "content": "Noted."},
                  {"role": "user", "content": "no I meant Thursdays"}])
    sent = [m for m in llm.calls[-1]["messages"] if m["role"] == "user"][-1]["content"]
    assert sent.startswith("Correction to what I asked before: Thursdays") and "undid" in sent
    assert [m["content"] for m in db.all("SELECT content FROM memories WHERE archived = 0")] == ["Jay likes Thursdays"]
    assert out["content"].startswith("Undid that")


def test_you_misheard_me(db, llm):
    a = Agent(db)
    a.chat([{"role": "user", "content": "add a daily goal: make one bet"}])
    out = a.chat([{"role": "user", "content": "you misheard me"}])
    assert _goals(db) == [] and out["content"].endswith("Sorry — what did you say?")
    assert Agent(db).chat([{"role": "user", "content": "you misheard me"}])["content"] == "Sorry — what did you say?"
