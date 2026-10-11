"""Memory cleanup, preferences, favorites, pronunciations, quick notes, weekly self-review."""

from datetime import datetime, timedelta

from amo.agent import Agent
from amo.memory import Memory


def ask(db, text, history=()):
    return Agent(db).chat([*history, {"role": "user", "content": text}])["content"]


# ------------------------------------------------------------------ memory cleanup
def test_updated_fact_replaces_the_old_one(db, llm):
    m = Memory(db)
    m.add("Jay's rate is $50 an hour", "studio")
    new = m.add("Jay's rate is $60 an hour now", "studio")
    assert new["replaced"] == ["Jay's rate is $50 an hour"]
    live = [r["content"] for r in db.all("SELECT content FROM memories WHERE archived = 0")]
    assert live == ["Jay's rate is $60 an hour now"]
    assert db.scalar("SELECT superseded_by FROM memories WHERE content LIKE '%$50%'") == new["id"]
    m.add("Jay likes trap beats", "person")
    m.add("Jay likes R&B too", "person")  # different likes aren't "updates"
    assert db.scalar("SELECT COUNT(*) FROM memories WHERE archived = 0 AND content LIKE 'Jay likes%'") == 2


def test_tidy_and_stale(db, llm):
    db.insert("memories", {"content": "Studio rent is $1200 a month", "category": "studio", "created_at": "2025-01-01T00:00:00"})
    db.insert("memories", {"content": "studio rent is $1200 a month.", "category": "studio", "created_at": "2025-02-01T00:00:00"})
    assert Memory(db).tidy() == {"merged": 1}
    stale = Memory(db).stale(days=90)
    assert [s["content"] for s in stale] == ["studio rent is $1200 a month."]


def test_remember_confirms_the_update(db, llm):
    ask(db, "remember that Ty's rate is $40")
    assert ask(db, "remember that Ty's rate is $45 now").endswith("Updated — that replaces “Ty's rate is $40”.")


# ------------------------------------------------------------------ preferences + favorites
def test_preferences_are_saved_and_always_in_the_prompt(db, llm):
    assert ask(db, "From now on, give me the numbers first") == "Got it — from now on: give me the numbers first."
    assert ask(db, "That's too long").endswith("from now on: keep answers short — a sentence or two.")
    prompt = Agent(db).build_system_prompt()
    assert "preference: give me the numbers first" in prompt and "keep answers short" in prompt
    assert llm.calls == []


def test_favorites_in_prompt_and_whisper_hint(db, llm):
    from amo.crm import StudioCRM
    from amo.profile import prompt_line, whisper_prompt

    crm = StudioCRM(db)
    crm.add_client("Jay Carter", artist_name="Lil Jay")
    crm.add_client("Tyrese Brown", artist_name="Ty")
    for d in (1, 2, 3):
        crm.book_session("Lil Jay", (datetime.now() + timedelta(days=d)).strftime("%Y-%m-%d 19:00"))
    crm.book_session("Ty", (datetime.now() + timedelta(days=4)).strftime("%Y-%m-%d 19:00"))
    db.insert("watchlist", {"symbol": "AMZN", "created_at": "2026-01-01"})
    assert "clients Lil Jay, Ty; tickers AMZN" in prompt_line(db)
    hint = whisper_prompt(db)
    assert "Lil Jay" in hint and "Jay Carter" in hint and "AMZN" in hint


# ------------------------------------------------------------------ pronunciations
def test_teach_a_pronunciation(db, llm):
    from amo.voice import tts

    history = [{"role": "user", "content": "who's booked friday"},
               {"role": "assistant", "content": "Karim Haddad is booked Friday at 7 PM."}]
    out = ask(db, "No, it's pronounced kah-reem", history)
    assert out == "Got it — I'll say Karim like that from now on."
    assert "kah-reem" in tts.clean_for_speech("Booked Karim for Friday.")
    assert ask(db, "it's pronounced zzzqqq", history) == "Which word should I say like that?"


def test_pronunciation_lesson_is_not_a_mishearing():
    from amo.voice.listen import barge_in

    assert barge_in("No, it's pronounced kah-reem", "Booked Karim for Friday") == (
        True, "No, it's pronounced kah-reem", False)


# ------------------------------------------------------------------ quick notes
def test_quick_notes(db, llm):
    assert ask(db, "Note: beat idea, 140 BPM, dark piano") == "Noted: “Beat idea, 140 BPM, dark piano”."
    ask(db, "take a note call the venue about the 20th")
    assert db.scalar("SELECT tags FROM notes WHERE text LIKE 'Beat%'") == "music, idea"
    found = ask(db, "what notes do I have about beats?")
    assert found.startswith("1 note about beats: Beat idea, 140 BPM, dark piano")
    assert ask(db, "read my last note").startswith("Your last note (today): Call the venue")
    assert ask(db, "undo that").startswith("Undid that: removed the note Call the venue")
    assert llm.calls == []


# ------------------------------------------------------------------ weekly self-review
def test_weekly_self_review(db, llm):
    from amo.selfreview import log_correction, review

    log_correction("misheard", "book Jay friday at 5 am", "book Jay friday at 5 pm", db)
    log_correction("misheard", "book Jay at 3", "3 pm", db)
    log_correction("misheard", "add Tyrone to the list", "add Tyrese to the list", db)
    log_correction("pronunciation", "kah-reem", "Karim", db)
    db.insert("memories", {"content": "Jay's rate is $50", "category": "studio", "created_at": "2025-03-01T00:00:00"})
    r = review(db)
    assert r["text"].startswith("This week you corrected me 3 times (2 on times, 1 on names).")
    assert "I'll assume PM for 1 to 6 o'clock" in r["text"] and "I'll listen for “Tyrese”" in r["text"]
    assert "I learned to say Karim right." in r["text"]
    assert "“Jay's rate is $50” (from 2025-03)" in r["text"]
    assert "1 to 6 without AM/PM means PM" in Agent(db).build_system_prompt()


def test_corrections_are_logged_from_chat(db, llm):
    ask(db, "add a daily goal: make one bet")
    ask(db, "no, I said add a daily goal: make one beat",
        [{"role": "user", "content": "add a daily goal: make one bet"},
         {"role": "assistant", "content": "New daily goal: Make one bet."}])
    row = db.one("SELECT kind, said, meant FROM corrections")
    assert row == {"kind": "misheard", "said": "add a daily goal: make one bet", "meant": "add a daily goal: make one beat"}


def test_everyday_sentences_dont_trigger_shortcuts():
    from amo import coding, fastpath, profile, pronounce

    assert profile.match("I want you to book Jay Friday") is None          # a request, not a preference
    assert profile.match("from now on Jay's rate is 60") is None           # a fact for the AI to save
    assert profile.match("I want you to keep answers short")[0] == "set_preference"
    assert pronounce.match("Say it like you mean it") is None
    assert pronounce.match("Say hi to Jay like a friend") is None
    assert pronounce.match("Say Nguyen like win") == ("set_pronunciation", {"word": "Nguyen", "say": "win"})
    assert fastpath.match("Should I sell my Jordans") is None              # not a ticker
    assert fastpath.match("should I short SPY at 500")[0] == "check_trade"
    assert fastpath.match("I want to set a goal but I'm not sure what") is None
    assert fastpath.match("Remember to text Jay tomorrow") is None         # a reminder, not a fact
    assert coding.match("any errors in Jay's invoice?") is None
    assert coding.match("what errors did I make on trades") is None
    assert coding.match("do you have any errors?") == ("self_repair", {})
