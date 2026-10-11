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
  amo listen              hands-free: say "Hey AMO" (add -v to see what it hears)
  amo setup-wake vosk     lighter "Hey AMO" detector for a Raspberry Pi (or openwakeword)
  amo calendar            show studio sessions in Apple Calendar (subscribes once, updates itself)
  amo backup / restore F  back up AMO's data now (nightly backups are automatic) / restore one
  amo lan on|off|status   let a body device on your Wi-Fi (Raspberry Pi) reach this Mac
  amo device --brain URL --key KEY   run AMO's body (mic + speaker) against a brain on your network
  amo mic-test            check the microphone, speech recognition and voice step by step
  amo voices              list voice presets and the British voices on this Mac
  amo try-voice [PRESET]  hear a voice before choosing it
  amo set-voice PRESET    switch AMO's voice + personality (computer, jarvis, british-female, default)
  amo personality NAME    switch only the personality (mood, chill, computer, jarvis, default)
  amo look [QUESTION]     take a picture and describe it   (amo setup-vision first)
  amo hw                  show connected hardware          (amo hw-agent --simulate to try it)
  amo brief               print today's brief
  amo learn               extract facts from recent conversations now
  amo reflect             run the weekly reflection now
"""

from __future__ import annotations

import argparse
import os
import json
import logging
import sys
import urllib.request
from pathlib import Path

from .config import settings



def cmd_serve(a):
    import uvicorn

    host = a.host
    if host is None:  # open to your network only when it's protected by a key
        host = "0.0.0.0" if settings.api_key else "127.0.0.1"
        if not settings.api_key:
            print("Serving on this computer only (no AMO_API_KEY set). To let devices on your Wi-Fi in: amo lan on")
    uvicorn.run("amo.api.main:app", host=host, port=a.port, log_level="info")


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


def cmd_listen(a):
    from .voice.listen import run
    from .voice.stt import VoiceUnavailable

    try:
        run(verbose=a.verbose)
    except VoiceUnavailable as e:
        print(f"Voice isn't available: {e}")
        sys.exit(1)


def cmd_mic_test(_a):
    """Step-by-step check of everything hands-free mode needs."""
    ok = lambda m: print(f"  ✓ {m}")  # noqa: E731
    bad = lambda m: print(f"  ✗ {m}")  # noqa: E731
    fix_pkgs = "~/.local/bin/uv pip install --python .venv/bin/python sounddevice faster-whisper numpy"

    print("1. Software")
    missing = []
    for mod, label in (("numpy", "numpy"), ("sounddevice", "microphone access (sounddevice)"),
                       ("faster_whisper", "speech recognition (faster-whisper)")):
        try:
            __import__(mod)
            ok(label)
        except Exception as e:  # noqa: BLE001 — ImportError or a broken native library
            bad(f"{label}: {e}")
            missing.append(mod)
    from .voice.tts import piper_installed

    (ok if piper_installed() else bad)("AMO's voice engine (Piper)" if piper_installed()
                                       else "Piper not installed — AMO will use the Mac voice instead")
    if missing:
        print(f"\n  Fix: cd ~/AMO && {fix_pkgs}\n  Then run amo mic-test again.")
        sys.exit(1)

    import numpy as np
    import sounddevice as sd

    print("\n2. Microphone")
    try:
        dev = sd.query_devices(kind="input")
        ok(f"using: {dev['name']}")
    except Exception as e:  # noqa: BLE001
        bad(f"no microphone found ({e})")
        sys.exit(1)

    print("\n3. Hearing you — say:  “Hey AMO, what time is it?”  (recording 5 seconds)")
    for i in (3, 2, 1):
        print(f"   {i}…", end=" ", flush=True)
        import time

        time.sleep(0.7)
    print("GO — speak now")
    audio = sd.rec(int(5 * 16000), samplerate=16000, channels=1, dtype="float32")
    sd.wait()
    audio = audio[:, 0]
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    bar = "█" * min(30, int(peak * 60))
    print(f"   level {bar or '(nothing)'}")
    if peak < 0.003:
        bad("only silence — macOS is probably blocking the mic for Terminal.")
        print("    Fix: System Settings → Privacy & Security → Microphone → turn on Terminal, "
              "then quit Terminal completely (⌘Q), reopen it and run amo mic-test again.")
        sys.exit(1)
    if peak < 0.03:
        bad("very quiet — move closer to the mic or raise Input volume (System Settings → Sound → Input)")
    else:
        ok("microphone works")

    print("\n4. Understanding you")
    from .voice.listen import WAKE_PROMPT, match_wake
    from .voice.stt import transcribe_array

    print("   (first run downloads the speech models, ~150 MB)")
    quick = transcribe_array(audio, model=settings.wake_model, prompt=WAKE_PROMPT)
    print(f"   wake-word model heard:  “{quick}”")
    full = transcribe_array(audio, prompt="AMO")
    print(f"   main model heard:       “{full}”")
    woke, rest = match_wake(quick)
    if woke:
        ok(f"wake phrase recognised, request: “{rest}”" if rest else "wake phrase recognised")
    else:
        bad("didn't recognise “Hey AMO”. Speak a little slower, or tell Claude the line above so "
            "it can add that spelling (AMO_WAKE_WORDS).")

    print("\n5. Speaking")
    try:
        from .voice.loop import play_wav
        from .voice.tts import synthesize

        play_wav(synthesize("Microphone check complete. I can hear you perfectly. Unfortunately."))
        ok("you should have just heard AMO speak")
    except Exception as e:  # noqa: BLE001
        bad(f"couldn't speak: {e}")

    print("\n6. AI model")
    from .llm import get_llm

    (ok if get_llm().list_models() else bad)(
        "Ollama is running" if get_llm().list_models() else "Ollama isn't running — open the Ollama app")
    print("\nIf everything is ✓, run:  amo listen")


def _lan_ip() -> str:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def cmd_lan(a):
    import secrets
    import socket
    import subprocess

    from .config import PROJECT_ROOT

    if a.action == "status":
        print(f"LAN access: {'ON' if os.environ.get('AMO_LAN') == '1' else 'off'}")
        return
    if a.action == "on":
        key = settings.api_key or secrets.token_hex(16)
        _set_env("AMO_API_KEY", key)
        _set_env("AMO_LAN", "1")
    else:
        _set_env("AMO_LAN", "0")
    script = PROJECT_ROOT / "scripts" / "mac-autostart.sh"
    if sys.platform == "darwin" and script.exists():
        subprocess.run([str(script), "install"], check=False)
    if a.action == "on":
        host = socket.gethostname().split(".")[0]
        print("\nAMO's brain is now reachable on your Wi-Fi (other devices need the key; this Mac doesn't).")
        print(f"  Address:  http://{host}.local:8765   (or http://{_lan_ip()}:8765)")
        print(f"  Key:      {key}")
        print(f"\nOn the Raspberry Pi:\n  amo device --brain http://{host}.local:8765 --key {key}")
    else:
        print("LAN access off — only this Mac can reach AMO.")


def cmd_device(a):
    from .voice.device import run

    run(a.brain, a.key or settings.api_key, verbose=a.verbose)


def cmd_hw_agent(a):
    from .hw_agent import load_config, run

    cfg = load_config(a.config) if a.config else None
    if cfg is None and not a.simulate:
        print("Give it a parts file (--config hw.json, see amo/hw_agent.py) or try --simulate.")
        sys.exit(1)
    try:
        run(a.brain.rstrip("/"), a.key or settings.api_key, cfg, simulate=a.simulate or cfg is None)
    except KeyboardInterrupt:
        print()


def cmd_hw(_a):
    from .hardware import devices

    devs = devices()
    if not devs:
        print("No hardware connected yet. Try:  amo hw-agent --simulate   (in another window)")
    for d in devs:
        print(f"{d['name']} ({d['kind']}, {'online' if d['online'] else 'offline'}, last seen {d['last_seen']})")
        for p in d["parts"]:
            v = d["state"].get(p["id"])
            print(f"  {p['name']:<16} {p['type']:<8} {'' if v is None else v}{p.get('unit', '')}")


def cmd_setup_vision(a):
    from .vision import VisionUnavailable, capture

    model = a.model or settings.vision_model
    print(f"downloading the vision model {model} …")
    try:
        _pull(model)
    except Exception as e:  # noqa: BLE001
        print(f"couldn't download {model}: {e}  (is the Ollama app running?)")
        sys.exit(1)
    if a.model:
        _set_env("AMO_VISION_MODEL", a.model)
        _restart_server()
    try:
        img = capture()
        print(f"camera ✓ ({len(img) // 1024} KB picture). Ask AMO: \"what do you see?\"")
    except VisionUnavailable as e:
        print(f"camera: {e}")


def cmd_setup_coder(a):
    model = a.model or settings.coder_model
    print(f"downloading the code model {model} … (about 2 GB for the 3b, 4.7 GB for the 7b)")
    try:
        _pull(model)
    except Exception as e:  # noqa: BLE001
        print(f"couldn't download {model}: {e}  (is the Ollama app running?)")
        sys.exit(1)
    if a.model:
        _set_env("AMO_CODER_MODEL", a.model)
        _restart_server()
    print("Coding ready. Ask AMO about code, paste an error, or say: fix ~/Projects/yourapp/main.py")


def cmd_persona(_a):
    import subprocess

    from .persona import ensure

    path = ensure()
    print(f"AMO's identity file: {path}\nEdit it, save — AMO uses it on the next message.")
    if sys.platform == "darwin":
        subprocess.run(["open", "-e", str(path)], check=False)


def cmd_knowledge(_a):
    import subprocess

    from .db import get_db
    from .knowledge import ensure_folder, sync

    f = ensure_folder()
    s = sync(get_db(), budget=400)
    print(f"Knowledge folder: {f}\n{s['files']} file(s) — {s['added']} new/changed read, {s['removed']} removed, "
          f"{s['embedded']} piece(s) indexed.")
    if sys.platform == "darwin":
        subprocess.run(["open", str(f)], check=False)


def cmd_repair(_a):
    from .selfrepair import run, status_text

    print(status_text())
    for r in run(restart=True):
        print(f"• {r['file']}: {r['result']}" + (f"\n{r['diff']}" if r["result"] == "applied" else
                                                 f" — {r['detail'][-300:]}" if r["detail"] else ""))


def cmd_rollback(_a):
    from .selfrepair import rollback

    try:
        print(rollback()["text"])
    except ValueError as e:
        print(e)


def cmd_look(a):
    from .vision import VisionUnavailable, look

    try:
        r = look(" ".join(a.question))
    except VisionUnavailable as e:
        print(e)
        sys.exit(1)
    print(r["answer"] + (f"   ({r['faces']} face{'s' if r['faces'] != 1 else ''} seen)" if r["faces"] else ""))


def cmd_calendar(_a):
    from .integrations import subscribe_calendar

    print(subscribe_calendar())


def cmd_backup(_a):
    from .integrations import backup

    print(f"Backed up to {backup()}")


def cmd_restore(a):
    from .integrations import restore

    live = restore(a.file)
    print(f"Restored {a.file} → {live}. Restart AMO: ~/AMO/scripts/mac-autostart.sh restart")


def cmd_setup_wake(a):
    from .voice.wake import setup

    setup(a.engine)


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


def cmd_personality(a):
    from .agent import PERSONALITIES
    from .voice.tts import active_personality

    if not a.name:
        print(f"personality: {active_personality()}   (options: {', '.join(PERSONALITIES)})")
        return
    if a.name not in PERSONALITIES:
        print("unknown personality. Options: " + ", ".join(PERSONALITIES))
        sys.exit(1)
    _set_env("AMO_PERSONALITY", a.name)
    print(f"personality: {a.name}")
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
    from .briefing import prepare
    from .db import get_db

    b = prepare(get_db(), render=False)
    print(b["text"])
    if not b.get("weather"):
        print("\n(no weather: set AMO_LOCATION=\"Your City, ST\" in .env)")
    elif b.get("location"):
        print(f"\n(weather for {b['location']} — change it with AMO_LOCATION in .env)")


def cmd_stats(a):
    from .perf import report_text

    print(report_text(days=a.days))


def cmd_eval(a):
    from . import evals

    if a.add:
        c = evals.add_custom(a.add, a.tool, a.expect)
        print(f"added: “{c.say}” → {', '.join(c.tool) or (c.reply_has[0] if c.reply_has else 'no tool')}"
              f"  ({evals.CUSTOM})")
        return
    models = [m.strip() for m in (a.models or "").split(",") if m.strip()] or [None]

    def progress(i, n, r):
        mark = "✓" if r["ok"] else "✗"
        print(f"\r  {i}/{n} {mark} {r['say'][:50]:<50}", end="", flush=True)

    summaries = []
    for m in models:
        print(f"\n▸ {m or settings.chat_model}")
        s = evals.run(model=m, mine=a.mine, offline=a.offline, only=a.only, progress=progress)
        print("\r" + " " * 70 + "\r" + evals.text(s))
        summaries.append(s)
    if len(summaries) > 1:
        print("\n" + evals.compare(summaries))


def cmd_mistakes(a):
    from .selfreview import mistakes_text

    print(mistakes_text(limit=a.limit))


def cmd_review(_a):
    from .selfreview import review

    print(review()["text"])


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
    s.add_argument("--host", default=None, help="default: 127.0.0.1, or 0.0.0.0 when AMO_API_KEY is set")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_serve)

    sub.add_parser("doctor", help="check that everything is installed").set_defaults(fn=cmd_doctor)
    sub.add_parser("chat", help="chat in the terminal").set_defaults(fn=cmd_chat)
    v = sub.add_parser("voice", help="push-to-talk voice mode")
    v.add_argument("--no-speak", action="store_true", help="print replies instead of speaking them")
    v.set_defaults(fn=cmd_voice)
    ls = sub.add_parser("listen", help='hands-free mode: say "Hey AMO"')
    ls.add_argument("-v", "--verbose", action="store_true", help="print everything it hears")
    ls.set_defaults(fn=cmd_listen)
    ln = sub.add_parser("lan", help="let a body device on your Wi-Fi reach this Mac")
    ln.add_argument("action", choices=["on", "off", "status"])
    ln.set_defaults(fn=cmd_lan)
    dv = sub.add_parser("device", help="run AMO's body (mic + speaker) against a brain on your network")
    dv.add_argument("--brain", required=True, help="e.g. http://Your-Mac.local:8765")
    dv.add_argument("--key", default="", help="the brain's AMO_API_KEY")
    dv.add_argument("-v", "--verbose", action="store_true")
    dv.set_defaults(fn=cmd_device)
    hw = sub.add_parser("hw-agent", help="connect a Raspberry Pi's GPIO (or a simulated device) to AMO")
    hw.add_argument("--brain", default="http://localhost:8765")
    hw.add_argument("--key", default="")
    hw.add_argument("--config", default="", help="parts file (JSON), see amo/hw_agent.py")
    hw.add_argument("--simulate", action="store_true", help="pretend hardware: a desk light, fan, servo, sensor")
    hw.set_defaults(fn=cmd_hw_agent)
    sub.add_parser("hw", help="show AMO's connected hardware").set_defaults(fn=cmd_hw)
    sv2 = sub.add_parser("setup-vision", help="download the vision model and test the camera")
    sv2.add_argument("model", nargs="?", default="", help="e.g. gemma3:4b (sees better, slower)")
    sv2.set_defaults(fn=cmd_setup_vision)
    sub.add_parser("persona", help="edit AMO's identity file (who AMO is)").set_defaults(fn=cmd_persona)
    sub.add_parser("knowledge", help="open your knowledge folder and read new files now").set_defaults(fn=cmd_knowledge)
    sub.add_parser("repair", help="fix AMO's own recorded bugs now (tested first)").set_defaults(fn=cmd_repair)
    sub.add_parser("rollback", help="undo AMO's last fix to itself").set_defaults(fn=cmd_rollback)
    sc = sub.add_parser("setup-coder", help="download AMO's local code model")
    sc.add_argument("model", nargs="?", default="", help="e.g. qwen2.5-coder:7b (smarter, slower)")
    sc.set_defaults(fn=cmd_setup_coder)
    lk = sub.add_parser("look", help="take a picture and describe it")
    lk.add_argument("question", nargs="*")
    lk.set_defaults(fn=cmd_look)
    sub.add_parser("calendar", help="subscribe Apple Calendar to AMO's sessions").set_defaults(fn=cmd_calendar)
    sub.add_parser("backup", help="back up AMO's data now").set_defaults(fn=cmd_backup)
    rs = sub.add_parser("restore", help="restore AMO's data from a backup file")
    rs.add_argument("file")
    rs.set_defaults(fn=cmd_restore)
    sw = sub.add_parser("setup-wake", help="install a lighter wake-word engine (vosk / openwakeword)")
    sw.add_argument("engine", choices=["vosk", "openwakeword"])
    sw.set_defaults(fn=cmd_setup_wake)
    sub.add_parser("mic-test", help="check mic, speech recognition and voice").set_defaults(fn=cmd_mic_test)
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
    pe = sub.add_parser("personality", help="show or switch AMO's personality")
    pe.add_argument("name", nargs="?")
    pe.set_defaults(fn=cmd_personality)

    sub.add_parser("brief", help="print today's brief").set_defaults(fn=cmd_brief)
    sub.add_parser("learn", help="learn from recent conversations now").set_defaults(fn=cmd_learn)
    ev = sub.add_parser("eval", help="score AMO on the test set (and compare AI models)")
    ev.add_argument("--models", default="", help="comma-separated, e.g. gemma4:e2b,qwen3:4b")
    ev.add_argument("--mine", action="store_true", help="also replay your own past requests (on a copy of your data)")
    ev.add_argument("--offline", action="store_true", help="skip cases that need the internet")
    ev.add_argument("--only", default="", help="a tag (goals, studio, trading, ai, instant…) or source (mine, custom)")
    ev.add_argument("--add", default="", help="add your own case: the request")
    ev.add_argument("--tool", default="", help="with --add: the tool it should use (e.g. add_goal)")
    ev.add_argument("--expect", default="", help="with --add: text the reply should contain")
    ev.set_defaults(fn=cmd_eval)
    mk = sub.add_parser("mistakes", help="what AMO got wrong lately (corrections, undo, 'that's wrong', failures)")
    mk.add_argument("--limit", type=int, default=20)
    mk.set_defaults(fn=cmd_mistakes)
    st = sub.add_parser("stats", help="speed report: where AMO's time goes and what would help")
    st.add_argument("--days", type=int, default=7)
    st.set_defaults(fn=cmd_stats)
    sub.add_parser("review", help="AMO's self-review: your corrections this week and what it learned").set_defaults(
        fn=cmd_review)
    sub.add_parser("reflect", help="run the weekly reflection now").set_defaults(fn=cmd_reflect)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
