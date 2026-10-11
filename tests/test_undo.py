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
    assert "Correction to what I asked before: Thursdays" in sent and "undid" in sent
    assert [m["content"] for m in db.all("SELECT content FROM memories WHERE archived = 0")] == ["Jay likes Thursdays"]
    assert out["content"].startswith("Undid that")


def test_you_misheard_me(db, llm):
    a = Agent(db)
    a.chat([{"role": "user", "content": "add a daily goal: make one bet"}])
    out = a.chat([{"role": "user", "content": "you misheard me"}])
    assert _goals(db) == [] and out["content"].endswith("Sorry — what did you say?")
    assert Agent(db).chat([{"role": "user", "content": "you misheard me"}])["content"] == "Sorry — what did you say?"


def test_a_story_is_not_a_correction(db, llm):
    assert correction("Actually I asked Jay to come in at 5, can you text him") is None
    assert correction("No I said I'd pay him Friday") is None
    assert correction("Wait, I said that to Marcus already") is None
    assert correction("Sorry I asked you earlier about my rate") is None
    a = Agent(db)
    a.chat([{"role": "user", "content": "add a daily goal: make one beat"}])
    llm.script({"role": "assistant", "content": "Want me to draft it?"})
    a.chat([{"role": "user", "content": "Actually I asked Jay to come in at 5, can you text him"}])
    assert _goals(db) == ["Make one beat"]  # nothing undone


def test_a_corrected_timer_is_set_from_what_you_meant(db, llm):
    a = Agent(db)
    a.chat([{"role": "user", "content": "remind me at 4 to call Jay"}])
    out = a.chat([{"role": "user", "content": "remind me at 4 to call Jay"},
                  {"role": "assistant", "content": "Okay."},
                  {"role": "user", "content": "No, I said remind me at 5 to call Jay"}])["content"]
    rows = db.all("SELECT message, status FROM timers ORDER BY id")
    assert [r["status"] for r in rows] == ["cancelled", "active"] and rows[1]["message"] == "call Jay"
    assert out.startswith("Undid that") and llm.calls == []


def test_undoing_a_remember_brings_back_the_old_fact(db, llm):
    a = Agent(db)
    a.chat([{"role": "user", "content": "remember that Jay's rate is $50 an hour"}])
    a.chat([{"role": "user", "content": "remember that Jay's rate is $60 an hour"}])
    rows = db.all("SELECT content, archived FROM memories ORDER BY id")
    assert [r["archived"] for r in rows] == [1, 0]  # the new fact replaced the old one
    a.chat([{"role": "user", "content": "undo that"}])
    assert db.all("SELECT content FROM memories WHERE archived = 0") == [{"content": "Jay's rate is $50 an hour"}]


def test_shortcut_results_are_not_second_guessed(db, llm):
    from amo import timers

    timers.start_timer(10, label="pasta", db=db)
    out = Agent(db).chat([{"role": "user", "content": "cancel the pasta timer"}])["content"]
    assert out == "Cancelled the pasta timer." and llm.calls == []
