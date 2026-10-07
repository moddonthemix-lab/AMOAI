"""`amo` command line.

  amo doctor              check Ollama, models, voice, database
  amo serve               run the API + dashboard + scheduler (Open WebUI connects here)
  amo chat                chat in the terminal
  amo voice               push-to-talk voice mode (Whisper + Piper)
  amo setup-voice         download the default Piper voice
  amo remember "fact"     save a memory
  amo memories [query]    list or search memories
  amo import FILE         import facts (one per line / bullet) from a text or markdown file
  amo use MODEL           download a model and switch AMO to it (e.g. amo use gemma4:e2b)
  amo bench [MODEL ...]   time models on this computer and check they can save data
  amo voices              list voice presets and the British voices on this Mac
  amo try-voice [PRESET]  hear a voice before choosing it
  amo set-voice PRESET    switch AMO's voice + personality (computer, jarvis, british-female, default)
  amo brief               print today's brief
  amo learn               extract facts from recent conversations now
  amo reflect             run the weekly reflection now
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from pathlib import Path

from .config import settings



def cmd_serve(a):
    import uvicorn

    uvicorn.run("amo.api.main:app", host=a.host, port=a.port, log_level="info")


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
    from .voice.tts import piper_installed, piper_path, resolve_style

    vs = resolve_style()
    if not piper_installed():
        print("Voice (TTS)     – Piper not installed; using the built-in Mac voice")
    else:
        have = piper_path(vs.piper).is_file()
        print(f"Voice (TTS)     {'✓' if have else '–'} {settings.voice} ({vs.piper}){'' if have else ' — downloads on first use'}")
    print(f"API key         {'✓ set' if settings.api_key and settings.api_key != 'change-me' else '⚠ set AMO_API_KEY in .env'}")
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
    """Download AMO's voice (the Piper voice behind AMO_VOICE)."""
    from .voice.tts import download_piper_voice, piper_installed, resolve_style

    style = resolve_style()
    if not piper_installed():
        print("Piper isn't installed (pip install piper-tts) — AMO will use the built-in Mac voice.")
        return
    print(f"downloading voice {style.piper} …")
    download_piper_voice(style.piper)
    print("voice ready.")

def _restart_server() -> None:
    """Restart the background AMO server (macOS launchd) so it picks up .env changes."""
    import os
    import subprocess

    plist = Path.home() / "Library/LaunchAgents/com.amo.server.plist"
    if sys.platform == "darwin" and plist.exists():
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.amo.server"], check=False)
        print("restarted AMO")
    else:
        print("restart AMO (amo serve) to apply")


def _set_env(key: str, value: str) -> Path:
    import re

    from .config import env_file

    path = env_file()
    text = path.read_text() if path.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    path.write_text(text)
    return path


def _pull(model: str) -> None:
    from .llm import get_llm

    last = ""
    for status in get_llm().pull(model):
        if status != last:
            print(f"\r  {status:<60}", end="", flush=True)
            last = status
    print()


def cmd_use(a):
    from .llm import LLMError, get_llm

    if not get_llm().list_models() and not a.skip_pull:
        print("Ollama isn't running — open the Ollama app first.")
        sys.exit(1)
    if not a.skip_pull:
        print(f"downloading {a.model} …")
        try:
            _pull(a.model)
        except (LLMError, Exception) as e:  # noqa: BLE001 — show any pull failure plainly
            print(f"couldn't download {a.model}: {e}")
            sys.exit(1)
    key = "AMO_FAST_MODEL" if a.fast else "AMO_CHAT_MODEL"
    path = _set_env(key, a.model)
    print(f"{key}={a.model}  (saved in {path})")
    _restart_server()


def cmd_bench(a):
    """Time each model on this machine: load, a greeting, and a real booking that must be saved."""
    import time
    from datetime import timedelta

    from .agent import Agent
    from .crm import StudioCRM
    from .db import Database, set_db, today
    from .llm import LLMError, get_llm

    llm = get_llm()
    installed = llm.list_models()
    models = a.models or [settings.chat_model]
    tomorrow = (today() + timedelta(days=1)).isoformat()
    print(f"{'model':<22} {'load':>7} {'greeting':>9} {'booking':>9}  saved booking?")
    for m in models:
        if not any(i == m or i == f"{m}:latest" for i in installed):
            print(f"{m:<22} not downloaded — run: ollama pull {m}   (or: amo use {m})")
            continue
        db = Database(":memory:")
        set_db(db)
        StudioCRM(db).add_client("Jay Carter", artist_name="Lil Jay")
        agent = Agent(db)
        try:
            t = time.time()
            llm.chat([{"role": "user", "content": "hi"}], model=m)
            load = time.time() - t
            t = time.time()
            agent.chat([{"role": "user", "content": "hey AMO"}], channel="cli", model=m, learn=False)
            greet = time.time() - t
            t = time.time()
            agent.chat([{"role": "user", "content": "Book Lil Jay tomorrow at 7pm for 3 hours at $50 an hour"}],
                       channel="cli", model=m, learn=False)
            book = time.time() - t
        except LLMError as e:
            print(f"{m:<22} error: {e}")
            continue
        s = db.one("SELECT starts_at, hours, rate FROM studio_sessions")
        ok = bool(s) and s["starts_at"].startswith(f"{tomorrow}T19") and s["hours"] == 3 and s["rate"] == 50
        detail = "✓ correct" if ok else (f"✗ wrong ({s['starts_at']}, {s['hours']}h, ${s['rate']})" if s else "✗ not saved")
        print(f"{m:<22} {load:>6.1f}s {greet:>8.1f}s {book:>8.1f}s  {detail}")
    print("\nPick the fastest model with ✓, then:  amo use <model>")


SAMPLE_LINES = {
    "computer": "Good evening. I've reviewed your schedule. It's about as organised as I expected.",
    "amo": "Good evening. I've reviewed your schedule. It's about as organised as I expected.",
    "jarvis": "Good evening. Your studio is booked at seven, and revenue is up twelve percent this week.",
    "british-female": "Good evening. You have two sessions today and one item ready to list.",
    "default": "Hey, I'm AMO. You have two sessions today and one item ready to list.",
}


def _play(wav: bytes) -> None:
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav)
    if sys.platform == "darwin":
        subprocess.run(["afplay", f.name], check=False)
    else:
        print(f"saved to {f.name}")


def _ensure_piper_voice(style) -> None:
    from .voice.tts import download_piper_voice, piper_installed, piper_path

    if piper_installed() and not piper_path(style.piper).is_file():
        print(f"downloading Piper voice {style.piper} …")
        try:
            download_piper_voice(style.piper)
        except Exception as e:  # noqa: BLE001
            print(f"  couldn't download it ({e}); using the built-in Mac voice instead")


def cmd_voices(_a):
    from .voice.tts import PRESETS, mac_voices, piper_installed

    print("Presets (amo set-voice NAME):")
    for name, st in PRESETS.items():
        print(f"  {name:<15} pitch {st.pitch:+g}, robot {st.robot:g}, personality {st.personality}")
    print(f"\nEngine: {'Piper (neural voices)' if piper_installed() else 'built-in macOS voices'}")
    if getattr(_a, "all", False):
        import json

        url = "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                catalog = json.load(r)
        except Exception as e:  # noqa: BLE001
            print(f"couldn't fetch the Piper catalog: {e}")
            return
        print("\nPiper English voices (amo try-voice NAME, then amo set-voice NAME --keep-personality):")
        for name, v in sorted(catalog.items()):
            if v["language"]["family"] != "en":
                continue
            speakers = v.get("speaker_id_map") or {}
            extra = f"  speakers: {', '.join(list(speakers)[:8])}{' …' if len(speakers) > 8 else ''}" if len(speakers) > 1 else ""
            print(f"  {name:<38} {v['language']['country_english']}{extra}")
        print("\nMulti-speaker voices: add :speaker, e.g. en_GB-semaine-medium:obadiah")
    british = [v for v in mac_voices() if v in ("Daniel", "Oliver", "Arthur", "Kate", "Serena", "Stephanie", "Martha", "Jamie")]
    if british:
        print("British voices on this Mac: " + ", ".join(british))
        print("More (higher quality): System Settings → Accessibility → Spoken Content → System voice → Manage Voices… → English (UK)")


def cmd_try_voice(a):
    from .voice.tts import PRESETS, resolve_style, synthesize

    name = a.preset or settings.voice
    style = resolve_style(name)
    _ensure_piper_voice(style)
    text = " ".join(a.text) if a.text else SAMPLE_LINES.get(name, SAMPLE_LINES["default"])
    if name not in PRESETS:
        print(f"(custom voice {name})")
    _play(synthesize(text, name))


def cmd_set_voice(a):
    from .voice.tts import PRESETS, resolve_style

    if a.preset not in PRESETS and not a.preset.startswith("say:") and "-" not in a.preset:
        print("unknown voice. Options: " + ", ".join(PRESETS) + "  (or a Piper voice name, or say:<Mac voice>)")
        sys.exit(1)
    style = resolve_style(a.preset)
    _ensure_piper_voice(style)
    _set_env("AMO_VOICE", a.preset)
    if not a.keep_personality:
        _set_env("AMO_PERSONALITY", style.personality)
    print(f"voice: {a.preset}" + ("" if a.keep_personality else f", personality: {style.personality}"))
    _restart_server()


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
    p = argparse.ArgumentParser(prog="amo", description="AMO — your local personal AI OS")
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

    u = sub.add_parser("use", help="download a model and switch AMO to it")
    u.add_argument("model")
    u.add_argument("--fast", action="store_true", help="set the background (learning) model instead")
    u.add_argument("--skip-pull", action="store_true", help="don't download, just switch")
    u.set_defaults(fn=cmd_use)

    b = sub.add_parser("bench", help="time models on this computer")
    b.add_argument("models", nargs="*")
    b.set_defaults(fn=cmd_bench)

    vs = sub.add_parser("voices", help="list voice presets")
    vs.add_argument("--all", action="store_true", help="also list every Piper English voice")
    vs.set_defaults(fn=cmd_voices)
    tv = sub.add_parser("try-voice", help="hear a voice")
    tv.add_argument("preset", nargs="?")
    tv.add_argument("text", nargs="*")
    tv.set_defaults(fn=cmd_try_voice)
    sv = sub.add_parser("set-voice", help="switch AMO's voice (and personality)")
    sv.add_argument("preset")
    sv.add_argument("--keep-personality", action="store_true", help="change only the voice")
    sv.set_defaults(fn=cmd_set_voice)

    sub.add_parser("brief", help="print today's brief").set_defaults(fn=cmd_brief)
    sub.add_parser("learn", help="learn from recent conversations now").set_defaults(fn=cmd_learn)
    sub.add_parser("reflect", help="run the weekly reflection now").set_defaults(fn=cmd_reflect)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
