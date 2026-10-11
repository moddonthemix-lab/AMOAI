"""The pocket pet (ESP32-S3 body): what the brain gives it, and what it brings back.

While connected the pet keeps a small copy of your day on its SD card (snapshot) plus voice clips in
AMO's voice (clips), so it stays useful with no Wi-Fi: it shows your sessions and goals, rings your
timers and reminders, and tells the time. Anything it records offline (voice notes, photos) is
uploaded when it's back (sync) and handled here:
  - "note: …" → saved as a note
  - a request that would change your data ("book Jay Friday at 7") → AMO asks first:
    "While I was offline you asked me to book Jay Friday at 7 — want me to do it?"
  - anything else → kept as a voice note
Photos are described by the vision model and saved as notes.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import time
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, settings
from .db import Database, get_db, local_now, now_iso

RATE = 16000                 # the pet plays and records 16 kHz mono 16-bit
DIR = PROJECT_ROOT / "data" / "pocket"
PENDING_KEY = "offline_requests"
PENDING_HOURS = 48

STATIC = {
    "chime": "",  # made on the device
    "offline": "I'm offline right now. I saved that, and I'll take care of it when I'm back with my brain.",
    "saved": "Saved.",
    "photo": "Got it, I saved the picture.",
    "back": "I'm back online.",
    "its": "It's",
    "am": "AM",
    "pm": "PM",
    "timer": "Your timer is done!",
    "hello": f"Hey {settings.owner_name}.",
    "sleepy": "I can't reach my brain right now.",
}
_NUM = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
        "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}


def _minute_words(m: int) -> str:
    if m == 0:
        return "o'clock"
    if m < 10:
        return f"oh {_NUM[m]}"
    if m < 20:
        return _NUM[m]
    return _TENS[m // 10] + ("" if m % 10 == 0 else f" {_NUM[m % 10]}")


def clip_text(clip_id: str, db: Database | None = None) -> str | None:
    """The words for a clip id: static phrases, time parts (h1–h12, m00–m59), timers (t<id>), the brief."""
    if clip_id in STATIC:
        return STATIC[clip_id] or None
    if m := re.fullmatch(r"h(\d{1,2})", clip_id):
        h = int(m.group(1))
        return _NUM[h] if 1 <= h <= 12 else None
    if m := re.fullmatch(r"m(\d{2})", clip_id):
        mm = int(m.group(1))
        return _minute_words(mm) if mm < 60 else None
    db = db or get_db()
    if m := re.fullmatch(r"t(\d+)", clip_id):
        from .timers import _spoken

        r = db.one("SELECT * FROM timers WHERE id = ?", (int(m.group(1)),))
        return _spoken(r) if r else None
    if m := re.fullmatch(r"brief-(\d{4}-\d{2}-\d{2})", clip_id):
        raw = db.get_kv("brief")
        b = json.loads(raw) if raw else None
        return b["text"] if b and b["date"] == m.group(1) else None
    if m := re.fullmatch(r"say-([0-9a-f]{12})", clip_id):
        return db.get_kv(f"pocket_say:{m.group(1)}")
    return None


def remember_say(text: str, db: Database | None = None) -> str:
    """A one-off phrase for the pet to fetch as a clip (e.g. the 'while I was offline…' summary)."""
    key = hashlib.sha1(text.encode()).hexdigest()[:12]
    (db or get_db()).set_kv(f"pocket_say:{key}", text)
    return f"say-{key}"


def _version(text: str) -> str:
    return hashlib.sha1(f"{settings.voice}|{text}".encode()).hexdigest()[:10]


# ------------------------------------------------------------------ audio for a small speaker
def to_pcm16k(wav: bytes) -> bytes:
    """Any WAV → raw 16 kHz mono 16-bit (what the pet plays)."""
    import numpy as np

    with wave.open(io.BytesIO(wav)) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != RATE and len(x):
        n = int(len(x) * RATE / rate)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


def to_wav16k(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def clip_wav(clip_id: str, db: Database | None = None) -> bytes | None:
    """The clip as a 16 kHz WAV (rendered once, then served from disk)."""
    from .voice.tts import synthesize

    text = clip_text(clip_id, db)
    if not text:
        return None
    path = DIR / "clips" / f"{clip_id}-{_version(text)}.wav"
    if path.is_file():
        return path.read_bytes()
    wav = to_wav16k(to_pcm16k(synthesize(text)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(wav)
    return wav


# ------------------------------------------------------------------ the pet's copy of your day
def _next_clock_change(real: float, days: int = 60) -> dict[str, int]:
    """When the clocks next change (daylight saving) and the offset after — so a pet that stays offline
    across the change still shows the right time. {} if there's none coming (or no timezone data)."""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(settings.timezone)
    except Exception:  # noqa: BLE001
        return {}

    def off(t: float) -> int:
        return int(datetime.fromtimestamp(t, tz).utcoffset().total_seconds())

    lo, hi = real, real + days * 86400
    if off(lo) == off(hi):
        return {}
    while hi - lo > 60:  # find the minute it changes
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if off(mid) == off(lo) else (lo, mid)
    return {"tz_change": int(hi), "tz_offset_after": off(hi)}


def snapshot(db: Database | None = None, now: datetime | None = None) -> dict[str, Any]:
    from . import mood
    from .goals import Goals

    db = db or get_db()
    now = now or local_now()
    real = time.time()
    epoch = lambda iso: int(real + (datetime.fromisoformat(iso) - now).total_seconds())  # noqa: E731
    day = now.date().isoformat()
    sessions = db.all("SELECT s.starts_at, c.name, c.artist_name FROM studio_sessions s JOIN clients c "
                      "ON c.id = s.client_id WHERE substr(s.starts_at, 1, 10) = ? AND s.status = 'booked' "
                      "ORDER BY s.starts_at", (day,))
    timers = db.all("SELECT * FROM timers WHERE status = 'active' AND due_at <= ? ORDER BY due_at",
                    ((now + timedelta(days=2)).isoformat(),))
    goals = Goals(db).today_list()
    clips = {k: clip_text(k) for k in STATIC if STATIC[k]}
    clips.update({f"h{h}": clip_text(f"h{h}") for h in range(1, 13)})
    clips.update({f"m{m:02d}": clip_text(f"m{m:02d}") for m in range(60)})
    for t in timers:
        clips[f"t{t['id']}"] = clip_text(f"t{t['id']}", db)
    raw = db.get_kv("brief")
    brief = json.loads(raw) if raw else None
    if brief and brief["date"] == day:
        clips[f"brief-{day}"] = brief["text"]
    return {
        "v": 1,
        "now": int(real),
        "tz_offset": int(round((now - datetime.fromtimestamp(real, timezone.utc).replace(tzinfo=None))
                               .total_seconds() / 900) * 900),
        **_next_clock_change(real),
        "assistant": settings.assistant_name,
        "owner": settings.owner_name,
        "mood": mood.current(db).name,
        "sessions": [{"time": s["starts_at"][11:16], "who": s["artist_name"] or s["name"]} for s in sessions],
        "goals": [{"id": g["id"], "title": g["title"][:40], "done": bool(g["done_today"])} for g in goals][:8],
        "timers": [{"id": t["id"], "due": epoch(t["due_at"]), "label": (t["name"] or t["message"] or t["kind"])[:18],
                    "clip": f"t{t['id']}"} for t in timers],
        "brief": f"brief-{day}" if brief and brief["date"] == day else "",
        "clips": [{"id": k, "v": _version(v)} for k, v in clips.items() if v],
    }


# ------------------------------------------------------------------ what it brings back
def receive(event: dict[str, Any], body: bytes = b"", db: Database | None = None) -> dict[str, Any]:
    """Store one uploaded event (idempotent: the same event_id twice is ignored)."""
    db = db or get_db()
    eid = str(event.get("id") or "").strip()[:64]
    etype = str(event.get("type") or "")
    if not eid or etype not in ("voice_note", "photo", "pet", "timer_fired"):
        raise ValueError("bad event")
    if db.one("SELECT id FROM pocket_events WHERE event_id = ?", (eid,)):
        return {"ok": True, "duplicate": True}
    at = event.get("at")
    when = (_local(int(at)) if isinstance(at, (int, float)) and at > 1_600_000_000 else local_now())
    when = when.isoformat(timespec="seconds")
    path = None
    if body:
        ext = {"voice_note": "wav", "photo": "jpg"}.get(etype, "bin")
        path = DIR / "inbox" / f"{re.sub(r'[^A-Za-z0-9_-]', '', eid)}.{ext}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    db.insert("pocket_events", {"event_id": eid, "type": etype, "at": when,
                                "data": json.dumps({k: v for k, v in event.items() if k not in ("id", "type", "at")}),
                                "file": str(path) if path else None, "created_at": now_iso()})
    return {"ok": True}


def _local(epoch: int) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.fromtimestamp(epoch, ZoneInfo(settings.timezone)).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return datetime.fromtimestamp(epoch)


def _is_request(text: str) -> bool:
    """Would doing this change your data (so AMO should ask first)?"""
    from . import fastpath
    from .timers import match as timer_match
    from .voice.acks import kind_of

    return bool(timer_match(text) or fastpath.match(text) or kind_of(text) == "action")


def process(db: Database | None = None, transcribe=None, describe=None) -> dict[str, Any]:
    """Handle everything uploaded since last time. Returns counts and a summary to say."""
    from .notes import NOTE_RE, add as add_note

    db = db or get_db()
    rows = db.all("SELECT * FROM pocket_events WHERE status = 'received' ORDER BY at, id")
    notes = photos = 0
    asked: list[str] = []
    for r in rows:
        # claim it first: two syncs at once (retry + background job) must not save the same note twice
        if not db.execute("UPDATE pocket_events SET status = 'processing' WHERE id = ? AND status = 'received'",
                          (r["id"],)):
            continue
        try:
            when = datetime.fromisoformat(r["at"])
            stamp = f"{when:%b} {when.day}, {when.hour % 12 or 12}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"
            result = ""
            if r["type"] == "voice_note" and r["file"]:
                if transcribe is None:
                    from .voice.stt import transcribe as transcribe
                text = (transcribe(Path(r["file"]).read_bytes()) or "").strip()
                if not text:
                    result = "empty"
                elif NOTE_RE.match(text):
                    body = NOTE_RE.match(text).group("text").strip()
                    add_note(body[:1].upper() + body[1:], db)
                    notes += 1
                    result = "note"
                elif _is_request(text):
                    add_pending(text, r["at"], db)
                    asked.append(text)
                    result = "asked"
                else:
                    add_note(f"Voice note ({stamp}): {text}", db)
                    notes += 1
                    result = "note"
            elif r["type"] == "photo" and r["file"]:
                saved = DIR / "photos" / Path(r["file"]).name
                saved.parent.mkdir(parents=True, exist_ok=True)
                Path(r["file"]).replace(saved)
                what = ""
                try:
                    if describe is None:
                        from .vision import describe
                    what = describe(saved.read_bytes(), "Describe this photo in one sentence.")
                except Exception:  # noqa: BLE001 — no vision model: still keep the photo
                    pass
                add_note(f"Photo ({stamp}){': ' + what if what else ''} [{saved.name}]", db)
                photos += 1
                result = "photo"
            db.execute("UPDATE pocket_events SET status = 'done', result = ? WHERE id = ?", (result, r["id"]))
        except Exception as e:  # noqa: BLE001 — one bad upload mustn't block the rest
            db.execute("UPDATE pocket_events SET status = 'failed', result = ? WHERE id = ?", (str(e)[:200], r["id"]))
    parts = []
    if notes:
        parts.append(f"saved {notes} note{'s' if notes != 1 else ''}")
    if photos:
        parts.append(f"{photos} photo{'s' if photos != 1 else ''}")
    summary = ""
    if parts:
        summary = "While I was offline I " + " and ".join(parts) + "."
    if asked:
        summary += (" " if summary else "") + f"You also asked me to {asked[0].rstrip('.?!')} — want me to do it?"
        if len(asked) > 1:
            summary += f" (And {len(asked) - 1} more after that.)"
    if summary:
        from .proactive import announce

        announce(summary, "checkin", f"pocket-sync:{now_iso()}", ttl_minutes=240, db=db)
    return {"notes": notes, "photos": photos, "asked": asked, "summary": summary,
            "say": remember_say(summary, db) if summary else ""}


# ------------------------------------------------------------------ "want me to do it?"
def add_pending(text: str, at: str, db: Database | None = None) -> None:
    db = db or get_db()
    items = pending(db)
    items.append({"text": text, "at": at})
    db.set_kv(PENDING_KEY, json.dumps(items))


def pending(db: Database | None = None) -> list[dict[str, Any]]:
    db = db or get_db()
    raw = db.get_kv(PENDING_KEY)
    items = json.loads(raw) if raw else []
    cutoff = (local_now() - timedelta(hours=PENDING_HOURS)).isoformat()
    return [i for i in items if i["at"] >= cutoff]


def pop_pending(db: Database | None = None) -> dict[str, Any] | None:
    db = db or get_db()
    items = pending(db)
    first = items.pop(0) if items else None
    db.set_kv(PENDING_KEY, json.dumps(items))
    return first


YES_RE = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|do it|go ahead|please do)(?:[ ,]+(?:do it|go ahead|please))*[.!]*$",
                    re.I)
NO_RE = re.compile(r"^(?:no|nope|nah|skip(?: it)?|don'?t|never ?mind|forget it)(?:[ ,]+(?:thanks|skip it))*[.!]*$", re.I)
