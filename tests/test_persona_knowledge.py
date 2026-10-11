"""Persona file (who AMO is) and the knowledge folder (your documents)."""

import zipfile

from amo import knowledge, persona
from amo.agent import Agent


class EmbedLLM:
    VOCAB = ["mix", "master", "price", "hour", "album", "cravvr", "menu", "lyrics", "beat", "contract"]

    def __init__(self, base):
        self.base = base

    def embed(self, text, model=None):
        t = text.lower()
        return [float(t.count(w)) + 0.01 for w in self.VOCAB]

    def __getattr__(self, name):
        return getattr(self.base, name)


def test_persona_is_part_of_who_amo_is(db, llm, monkeypatch, tmp_path):
    monkeypatch.setattr(persona, "PATH", tmp_path / "persona.md")
    persona.ensure()
    assert "Who AMO is" in persona.PATH.read_text()
    assert "<!--" not in persona.text()  # notes for you are left out
    persona.PATH.write_text("# Who AMO is\nAMO grew up in the studio. Catchphrase: \"Let's cook.\"\n<!-- secret -->")
    prompt = Agent(db).build_system_prompt()
    assert "AMO grew up in the studio" in prompt and "Let's cook" in prompt and "secret" not in prompt


def _docx(path, text):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body>" + "".join(
            f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text.split("\n")) + "</w:body></w:document>")


def test_knowledge_folder(db, llm, monkeypatch, tmp_path):
    from amo.config import settings
    from amo.llm import set_llm

    set_llm(EmbedLLM(llm))
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path / "AMO Knowledge"))
    f = knowledge.ensure_folder()
    (f / "prices.md").write_text("# Studio prices\nMixing is $150 per song. Mastering is $60 per song. "
                                 "Recording is $50 an hour, 3 hour minimum.")
    _docx(f / "Cravvr plan.docx", "Cravvr menu launch in March.\nFirst menu: tacos and wings.")
    s = knowledge.sync(db)
    assert s["files"] == 3 and s["added"] == 3 and s["embedded"] >= 3
    assert knowledge.sync(db)["added"] == 0  # nothing changed → nothing re-read
    hits = knowledge.search("how much is mixing a song?")
    assert hits[0]["file"] == "prices.md" and "$150" in hits[0]["text"]
    assert "Cravvr menu launch" in knowledge.search("when is the cravvr menu launch")[0]["text"]
    (f / "prices.md").unlink()
    assert knowledge.sync(db)["removed"] == 1


def test_amo_answers_from_your_files(db, llm, monkeypatch, tmp_path):
    from amo.config import settings
    from amo.llm import set_llm

    set_llm(EmbedLLM(llm))
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path / "k"))
    f = knowledge.ensure_folder()
    (f / "prices.md").write_text("Mixing is $150 per song. Mastering is $60 per song.")
    knowledge.sync(db)
    llm.script({"role": "assistant", "content": "Mixing is $150 a song."})
    out = Agent(db).chat([{"role": "user", "content": "what do my docs say about mixing price?"}])
    assert out["content"] == "Mixing is $150 a song."
    sent = [m for m in llm.calls[-1]["messages"] if m["role"] == "user"][-1]["content"]
    assert "From your file “prices.md”: Mixing is $150 per song" in sent
