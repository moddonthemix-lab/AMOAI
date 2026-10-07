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


def test_saves_confirm_instantly_without_second_model_pass(db, llm):
    StudioCRM(db).add_client("Jay Carter", artist_name="Lil Jay")
    llm.script(tool_call("book_session", client="Lil Jay", starts_at="2030-01-04 19:00", hours=3, rate=50))
    out = Agent(db).chat([{"role": "user", "content": "book lil jay friday 7pm 3h at 50"}])
    assert len(llm.calls) == 1  # one model pass, not two
    assert out["content"] == "Booked Lil Jay (Jay Carter) for Fri Jan 4 at 7 PM — 3h recording at $50/h ($150)."


def test_questions_still_get_a_model_answer(db, llm):
    llm.script(tool_call("revenue", period="month"), {"role": "assistant", "content": "You're at $0 this month."})
    out = Agent(db).chat([{"role": "user", "content": "how much did I make this month"}])
    assert len(llm.calls) == 2 and out["content"] == "You're at $0 this month."


def test_failed_save_goes_back_to_the_model(db, llm):
    llm.script(tool_call("book_session", client="Nobody", starts_at="2030-01-04 19:00"),
               {"role": "assistant", "content": "I don't have a client called Nobody — who did you mean?"})
    out = Agent(db).chat([{"role": "user", "content": "book nobody friday"}])
    assert "who did you mean" in out["content"]


def test_learning_waits_until_quiet(db, llm, monkeypatch):
    from datetime import datetime, timedelta

    from amo.config import settings
    from amo.scheduler import tick

    monkeypatch.setattr(settings, "auto_learn", True)

    Agent(db).chat([{"role": "user", "content": "hey"}])
    last = datetime.fromisoformat(db.scalar("SELECT MAX(created_at) FROM conversations"))
    assert "learn" not in tick(db, last + timedelta(minutes=1))
    llm.script({"role": "assistant", "content": '{"facts": []}'})
    assert "learn" in tick(db, last + timedelta(minutes=6))


def test_voice_presets_and_effects(monkeypatch):
    import io
    import wave

    from amo.config import settings
    from amo.voice import tts

    monkeypatch.setattr(settings, "voice", "computer")
    style = tts.resolve_style()
    assert style.personality == "computer" and style.pitch < 0 and style.robot > 0
    assert tts.resolve_style("say:Daniel").say == ("Daniel",)
    assert tts.resolve_style("en_GB-alan-medium").piper == "en_GB-alan-medium"
    assert tts.clean_for_speech("**Booked** ✅ Jay") == "Booked Jay"

    # 1s of 220Hz tone through the effects: still valid mono 16-bit audio, pitch-shifted shorter/longer.
    import numpy as np

    rate = 22050
    tone = (np.sin(2 * np.pi * 220 * np.arange(rate) / rate) * 12000).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate), w.writeframes(tone.tobytes())
    out = tts.apply_effects(buf.getvalue(), pitch=-1.5, robot=0.3)
    with wave.open(io.BytesIO(out)) as w:
        assert w.getframerate() == rate and w.getsampwidth() == 2
        assert w.getnframes() > rate  # lower pitch → resampled longer


def test_computer_personality(db, llm, monkeypatch):
    from amo.config import settings
    from amo.confirm import COMPUTER_QUIPS

    monkeypatch.setattr(settings, "personality", "computer")
    assert "sarcastic" in Agent(db).build_system_prompt("hi")
    llm.script(tool_call("add_goal", title="Make one beat"))
    out = Agent(db).chat([{"role": "user", "content": "add a daily goal: make one beat"}])
    assert out["content"].startswith("New daily goal: Make one beat.")
    assert any(out["content"].endswith(q) for q in COMPUTER_QUIPS)


def test_piper_speaker_names(tmp_path, monkeypatch):
    import json

    from amo.config import settings
    from amo.voice import tts

    monkeypatch.setattr(settings, "piper_voice", str(tmp_path / "x.onnx"))
    (tmp_path / "en_GB-semaine-medium.onnx.json").write_text(json.dumps({"speaker_id_map": {"prudence": 0, "obadiah": 2}}))
    assert tts.split_speaker("en_GB-semaine-medium:obadiah") == ("en_GB-semaine-medium", "obadiah")
    assert tts.piper_path("en_GB-semaine-medium:obadiah").name == "en_GB-semaine-medium.onnx"
    assert tts.speaker_id("en_GB-semaine-medium:obadiah") == 2
    assert tts.speaker_id("en_GB-semaine-medium:3") == 3
    assert tts.speaker_id("en_GB-alan-medium") is None


def test_amo_voice_is_clean_obadiah_with_computer_personality(monkeypatch):
    from amo.config import settings
    from amo.voice import tts

    monkeypatch.setattr(settings, "voice", "amo")
    monkeypatch.setattr(settings, "personality", "")
    st = tts.resolve_style()
    assert st.piper == "en_GB-semaine-medium:obadiah" and st.pitch == 0 and st.robot == 0
    assert st.noise < 0.667 and st.noise_w < 0.8
    assert tts.active_personality() == "computer"
    monkeypatch.setattr(settings, "personality", "jarvis")
    assert tts.active_personality() == "jarvis"
