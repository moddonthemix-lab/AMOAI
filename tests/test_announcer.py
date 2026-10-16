"""AMO speaks up out loud from the server, without a page open."""

from amo import announcer
from amo.proactive import announce


def test_says_whats_waiting_once(db, monkeypatch):
    said = []
    monkeypatch.setattr(announcer, "listener_running", lambda: False)
    monkeypatch.setattr("amo.proactive.quiet_now", lambda now=None: False)
    a = announcer.Announcer(speak=said.append)
    announce("PLTR broke the daily high.", push=False, db=db)
    announce("Scholar finished the mic research.", push=False, db=db)
    assert a.tick() == 2
    assert said == ["PLTR broke the daily high.", "Scholar finished the mic research."]
    assert a.tick() == 0  # never twice


def test_hands_free_listening_does_the_talking_when_it_runs(db, monkeypatch, tmp_path):
    monkeypatch.setattr(announcer.settings, "db_path", str(tmp_path / "amo.db"))
    said = []
    a = announcer.Announcer(take=lambda: ["hello"], speak=said.append)
    announcer.listener_checked_in()
    assert announcer.listener_running() and a.tick() == 0 and said == []


def test_a_voice_problem_never_stops_it(monkeypatch):
    monkeypatch.setattr(announcer, "listener_running", lambda: False)

    def broken(text):
        raise RuntimeError("no voice")

    assert announcer.Announcer(take=lambda: ["a", "b"], speak=broken).tick() == 0


def test_on_by_default_only_on_a_mac(monkeypatch):
    monkeypatch.delenv("AMO_SPEAK_ALOUD", raising=False)
    monkeypatch.setattr(announcer.sys, "platform", "darwin")
    assert announcer.enabled()
    monkeypatch.setenv("AMO_SPEAK_ALOUD", "0")
    assert not announcer.enabled()
    monkeypatch.delenv("AMO_SPEAK_ALOUD")
    monkeypatch.setattr(announcer.sys, "platform", "linux")
    assert not announcer.enabled()
