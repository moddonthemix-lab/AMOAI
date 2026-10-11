"""Long-term recall, eyes (vision) and hands (hardware) — the capabilities AMO's body will use."""

import threading

import pytest

from amo import hardware, recall
from amo.agent import Agent
from amo.fastpath import match


# ------------------------------------------------------------------ recall
class EmbedLLM:
    """Bag-of-words 'embeddings' so meaning-search can be tested without Ollama."""
    VOCAB = ["album", "jay", "mix", "tesla", "trade", "goal", "beat", "money", "studio", "record"]

    def __init__(self, base):
        self.base = base

    def embed(self, text, model=None):
        t = text.lower()
        return [float(t.count(w)) + 0.01 for w in self.VOCAB]

    def __getattr__(self, name):
        return getattr(self.base, name)


def test_vector_index_and_conversation_recall(db, llm):
    from amo.llm import set_llm
    from amo.memory import Memory

    set_llm(EmbedLLM(llm))
    a = Agent(db)
    for text in ["Jay wants the album mix done by Friday", "what's tesla doing", "add a daily goal: make one beat"]:
        a.chat([{"role": "user", "content": text}])
    assert recall.index_conversations(db) == 3
    assert recall.index_conversations(db) == 0  # nothing new
    hits = recall.search_conversations("what did I say about the album mix?", db=db)
    assert hits and "album" in hits[0]["you"]

    Memory(db).add("Jay records on Fridays", "studio", 3)
    Memory(db).add("Tesla trades need a stop", "trading", 3)
    assert Memory(db).search("jay studio record")[0]["content"] == "Jay records on Fridays"


def test_conversation_recall_without_embeddings(db, llm):
    a = Agent(db)
    a.chat([{"role": "user", "content": "Jay wants the album mix done by Friday"}])
    hits = recall.search_conversations("what did I say about the album?", db=db)
    assert hits and hits[0]["you"].startswith("Jay wants")


# ------------------------------------------------------------------ vision
def test_vision_fast_path(db, llm, monkeypatch):
    from amo import vision

    assert match("What do you see?") == ("look", {"question": "What do you see?"})
    assert match("what am I holding") and match("read this for me") and match("is anyone here?")
    assert (match("what's this charge on my card") or ("",))[0] != "look"

    monkeypatch.setattr(vision, "capture", lambda camera=None: b"\xff\xd8fakejpeg")
    monkeypatch.setattr(vision, "faces_in", lambda img: 1)
    llm.script({"role": "assistant", "content": "You're holding a red coffee mug."})
    out = Agent(db).chat([{"role": "user", "content": "what am I holding?"}])
    assert out["content"] == "You're holding a red coffee mug."
    sent = llm.calls[-1]
    assert sent["model"] == "moondream" and sent["messages"][0]["images"]


def test_vision_without_camera(db, llm, monkeypatch):
    from amo.config import settings

    monkeypatch.setattr(settings, "camera", "none")
    out = Agent(db).chat([{"role": "user", "content": "what do you see"}])
    assert "No camera" in out["content"]


# ------------------------------------------------------------------ hardware
def _fake_device(db, stop):
    """An ESP32 stand-in: polls for commands, does them, reports back."""
    while not stop.is_set():
        cmds = hardware.poll("desk", wait=0.3, db=db)
        if cmds:
            state = {c["part"]: c["action"] == "on" if c["action"] in ("on", "off") else c["value"] for c in cmds}
            hardware.report("desk", state, [{"id": c["id"], "ok": True} for c in cmds], db=db)


@pytest.fixture()
def desk(db):
    hardware.hello("desk", [{"id": "fan", "type": "switch"}, {"id": "light", "type": "rgb", "name": "desk light"},
                            {"id": "head", "type": "servo"}, {"id": "temp", "type": "sensor", "unit": "°C"}],
                   state={"temp": 22.5}, db=db)
    stop = threading.Event()
    t = threading.Thread(target=_fake_device, args=(db, stop), daemon=True)
    t.start()
    yield
    stop.set()
    t.join(2)


def test_hardware_commands_are_confirmed(db, llm, desk):
    assert match("turn on the fan") == ("hardware_control", {"target": "fan", "action": "on"})
    assert match("turn the desk light off") == ("hardware_control", {"target": "desk light", "action": "off"})
    assert match("make the light blue") == ("hardware_control", {"target": "light", "action": "set", "value": "blue"})

    out = Agent(db).chat([{"role": "user", "content": "turn on the fan"}])
    assert out["content"].endswith("Turned the fan on.")
    assert hardware.devices(db)[0]["state"]["fan"] is True
    out = Agent(db).chat([{"role": "user", "content": "make the desk light blue"}])
    assert out["content"].endswith("Made the desk light blue.")
    r = hardware.command("head", "set", "120", db=db)
    assert r["done"] and r["value"] == 120
    assert hardware.read("temperature", db)["value"] == 22.5
    with pytest.raises(ValueError):
        hardware.command("temp", "on", db=db)  # sensors are read-only
    assert hardware.command("fan", "set", "off", db=db, wait=0)["action"] == "off"  # set → off, not a toggle
    with pytest.raises(ValueError, match="what angle"):
        hardware.command("head", "set", None, db=db)
    with pytest.raises(ValueError):
        hardware.command("fan", "explode", db=db)


def test_hardware_offline_and_unknown(db, llm):
    hardware.hello("garage", [{"id": "door", "type": "switch"}], db=db)
    db.execute("UPDATE hw_devices SET last_seen = '2020-01-01T00:00:00'")
    r = hardware.command("door", "on", db=db)
    assert r["done"] is False and r["status"] == "pending"
    assert hardware.poll("garage", db=db)[0]["action"] == "on"  # delivered when it reconnects soon
    hardware.command("door", "off", db=db)
    db.execute("UPDATE hw_commands SET created_at = '2020-01-01T00:00:00' WHERE status = 'pending'")
    assert hardware.poll("garage", db=db) == []  # …but not hours later
    with pytest.raises(ValueError, match="don't have a device"):
        hardware.command("gar", "on", db=db)  # sensitive parts are never guessed from part of a name
    # not a device → falls through to the model instead of claiming anything
    llm.script({"role": "assistant", "content": "Alerts are off."})
    assert Agent(db).chat([{"role": "user", "content": "turn off the alerts"}])["content"] == "Alerts are off."


def test_hw_agent_parts_simulated():
    from amo.hw_agent import SIM_CONFIG, Part

    parts = {p["id"]: Part(p, simulate=True, log=lambda *_: None) for p in SIM_CONFIG["parts"]}
    assert parts["fan"].apply("on", None) is True and parts["fan"].apply("toggle", None) is False
    assert parts["light"].apply("set", "#0000ff") == "#0000ff"
    assert parts["head"].apply("set", 45) == 45
    assert isinstance(parts["temp"].read(), float)


def test_face_now_for_small_screens(db):
    from amo import face

    face.publish_state("thinking", db)
    snap = face.now_snapshot(db)
    assert snap["state"] == "thinking" and snap["speaking"] is False
    face._publish("say", {"words": [{"w": "Hello", "t": 0}], "env": [0.2, 0.9], "step": 50, "dur": 100,
                          "start": 1000}, db)
    snap = face.now_snapshot(db, now_ms=1060)
    assert snap["speaking"] and snap["level"] == 0.9 and snap["word"] == "Hello"
