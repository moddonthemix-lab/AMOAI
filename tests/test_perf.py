"""Speed report: every request is timed and explained."""

from amo import perf
from amo.agent import Agent
from conftest import tool_call


def rows(db):
    return db.all("SELECT * FROM timings ORDER BY id")


def test_every_path_is_timed(db, llm):
    a = Agent(db)
    a.chat([{"role": "user", "content": "what's 12 times 12"}])                 # instant
    a.chat([{"role": "user", "content": "add a daily goal: make one beat"}])    # direct action
    llm.script(tool_call("remember", content="Ty likes 808s"), {"role": "assistant", "content": "Noted."})
    a.chat([{"role": "user", "content": "keep in mind Ty likes 808s"}])          # AI + a tool
    r = rows(db)
    assert [x["path"] for x in r] == ["instant", "fastpath", "model"]
    assert r[1]["tools"] == "add_goal" and r[2]["model_calls"] >= 1 and r[2]["tools"].startswith("remember")
    assert all(x["total_ms"] is not None and x["first_ms"] is not None for x in r)
    assert r[2]["model_ms"] is not None and r[2]["first_token_ms"] is not None


def test_voice_turns_include_speech_and_first_sound(db, llm):
    t = perf.Turn("voice", "what's on today")
    t.add("stt", 900)
    t.mark("first_audio")
    t.save(db)
    r = rows(db)[0]
    assert r["channel"] == "voice" and r["stt_ms"] == 900 and r["first_ms"] is not None


def test_report_explains_where_time_goes(db):
    for i in range(25):
        db.insert("timings", {"at": perf.now_iso(), "channel": "voice",
                              "request": f"book Jay {i}", "path": "model", "first_ms": 6000, "total_ms": 9000,
                              "stt_ms": 2200, "model_ms": 7000, "first_token_ms": 5200, "tool_ms": 300,
                              "tts_ms": 600, "model_calls": 2, "tools": "book_session", "ok": 1})
    rep = perf.report(db)
    assert rep["count"] == 25 and rep["by_path"]["model"]["first_median"] == 6000
    text = perf.report_text(db)
    assert "Median time to first word/sound: 6.0s" in text and "AI first word 5.2s" in text
    advice = " ".join(rep["advice"])
    assert "smaller/faster model" in advice and "2+ model rounds" in advice and "Speech-to-text takes 2.2s" in advice


def test_empty_report(db):
    assert perf.report_text(db).startswith("No requests timed yet")
