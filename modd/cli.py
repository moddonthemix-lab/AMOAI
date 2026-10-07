"""`modd` command line.

  modd doctor              check Ollama, models, voice, database
  modd serve               run the API + dashboard + scheduler (Open WebUI connects here)
  modd chat                chat in the terminal
  modd voice               push-to-talk voice mode (Whisper + Piper)
  modd setup-voice         download the default Piper voice
  modd remember "fact"     save a memory
  modd memories [query]    list or search memories
  modd import FILE         import facts (one per line / bullet) from a text or markdown file
  modd brief               print today's brief
  modd learn               extract facts from recent conversations now
  modd reflect             run the weekly reflection now
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from pathlib import Path

from .config import settings

PIPER_VOICE_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/"


def cmd_serve(a):
    import uvicorn

    uvicorn.run("modd.api.main:app", host=a.host, port=a.port, log_level="info")


def cmd_doctor(_a):
    from .db import get_db
    from .llm import get_llm

    ok = True
    print(f"Database        {Path(settings.db_path).resolve()}  ", end="")
    db = get_db()
    print(f"✓ ({db.scalar('SELECT COUNT(*) FROM memories WHERE archived = 0')} memories)")

    models = get_llm().list_models()
    print(f"Ollama          {settings.ollama_url}  ", end="")
    if not models:
        print("✗ not reachable — install from https://ollama.com and run `ollama serve`")
        ok = False
    else:
        print(f"✓ ({len(models)} models)")
        for need in (settings.chat_model, settings.fast_model, settings.embed_model):
            have = any(m == need or m.split(":")[0] == need for m in models)
            print(f"  model {need:<22} {'✓' if have else '✗  run: ollama pull ' + need}")
            ok &= have or need == settings.embed_model  # embeddings are optional

    try:
        import faster_whisper  # noqa: F401

        print(f"Whisper (STT)   ✓ model {settings.whisper_model}")
    except ImportError:
        print('Whisper (STT)   – not installed (optional): pip install -e ".[voice]"')
    voice = Path(settings.piper_voice)
    print(f"Piper (TTS)     {'✓ ' + voice.name if voice.is_file() else '– no voice yet: modd setup-voice'}")
    print(f"API key         {'✓ set' if settings.api_key and settings.api_key != 'change-me' else '⚠ set MODD_API_KEY in .env'}")
    sys.exit(0 if ok else 1)


def cmd_chat(_a):
    from .agent import Agent

    agent = Agent()
    history: list[dict] = []
    print(f"{settings.assistant_name} — local chat. Ctrl+C or /quit to exit.")
    while True:
        try:
            text = input("\nyou › ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if text in ("/quit", "/exit"):
            return
        if not text:
            continue
        history.append({"role": "user", "content": text})
        result = agent.chat(history[-16:], channel="cli")
        history.append({"role": "assistant", "content": result["content"]})
        for t in result["tool_calls"]:
            print(f"  ↳ {t['name']}({json.dumps(t['arguments'])})")
        print(f"\n{settings.assistant_name.lower()} › {result['content']}")


def cmd_voice(a):
    from .voice.loop import run

    run(speak=not a.no_speak)


def cmd_setup_voice(_a):
    target = Path(settings.piper_voice)
    target.parent.mkdir(parents=True, exist_ok=True)
    name = "en_US-lessac-medium.onnx"
    for suffix in ("", ".json"):
        dest = target.with_name(target.name + suffix) if suffix else target
        if dest.exists():
            print(f"✓ {dest} already exists")
            continue
        print(f"downloading {name + suffix} …")
        urllib.request.urlretrieve(PIPER_VOICE_BASE + name + suffix, dest)
    print("Piper voice ready.")


def cmd_remember(a):
    from .memory import Memory

    m = Memory().add(" ".join(a.text), a.category, a.importance)
    print(f"{'already known' if m.get('duplicate') else 'saved'} #{m['id']}: {m['content']}")


def cmd_memories(a):
    from .memory import Memory

    mem = Memory()
    rows = mem.search(" ".join(a.query), limit=30, touch=False) if a.query else mem.list(a.category)
    for r in rows:
        print(f"#{r['id']:<4} [{r['category']}] ({'★' * r['importance']}) {r['content']}")
    if not rows:
        print("(no memories)")


def cmd_import(a):
    from .memory import Memory

    mem = Memory()
    n = 0
    for line in Path(a.file).read_text().splitlines():
        line = line.strip().lstrip("-*•").strip()
        if len(line) < 4 or line.startswith("#"):
            continue
        m = mem.add(line, a.category, a.importance, source="import")
        n += 0 if m.get("duplicate") else 1
    print(f"imported {n} memories")


def cmd_brief(_a):
    from .db import get_db
    from .scheduler import morning_brief_text

    print(morning_brief_text(get_db()))


def cmd_learn(_a):
    from .learning import learn_from_conversations

    added = learn_from_conversations()
    for m in added:
        print(f"+ [{m['category']}] {m['content']}")
    print(f"learned {len(added)} new facts")


def cmd_reflect(_a):
    from .learning import weekly_reflection

    r = weekly_reflection()
    print(r["summary"])
    if r["insights"]:
        print("\nSaved insights:")
        for i in r["insights"]:
            print(f"- {i}")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="modd", description="Modd — your local personal AI OS")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the API, dashboard and scheduler")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_serve)

    sub.add_parser("doctor", help="check that everything is installed").set_defaults(fn=cmd_doctor)
    sub.add_parser("chat", help="chat in the terminal").set_defaults(fn=cmd_chat)
    v = sub.add_parser("voice", help="push-to-talk voice mode")
    v.add_argument("--no-speak", action="store_true", help="print replies instead of speaking them")
    v.set_defaults(fn=cmd_voice)
    sub.add_parser("setup-voice", help="download the default Piper voice").set_defaults(fn=cmd_setup_voice)

    r = sub.add_parser("remember", help="save a memory")
    r.add_argument("text", nargs="+")
    r.add_argument("-c", "--category", default="general")
    r.add_argument("-i", "--importance", type=int, default=3)
    r.set_defaults(fn=cmd_remember)

    m = sub.add_parser("memories", help="list or search memories")
    m.add_argument("query", nargs="*")
    m.add_argument("-c", "--category")
    m.set_defaults(fn=cmd_memories)

    im = sub.add_parser("import", help="import facts from a text/markdown file")
    im.add_argument("file")
    im.add_argument("-c", "--category", default="general")
    im.add_argument("-i", "--importance", type=int, default=4)
    im.set_defaults(fn=cmd_import)

    sub.add_parser("brief", help="print today's brief").set_defaults(fn=cmd_brief)
    sub.add_parser("learn", help="learn from recent conversations now").set_defaults(fn=cmd_learn)
    sub.add_parser("reflect", help="run the weekly reflection now").set_defaults(fn=cmd_reflect)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
