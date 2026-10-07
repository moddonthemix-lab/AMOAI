"""Connections to the rest of your life (macOS):

- Calendar: an iCalendar feed of studio sessions and Cravvr due dates, subscribed in Apple Calendar
  (`amo calendar`), refreshed automatically.
- iMessage: AMO drafts texts to clients; nothing is sent until you say "send it".
- Backups: nightly copy of AMO's database to iCloud Drive (or ./backups), last 14 kept.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, settings
from .db import Database, get_db, local_now, now_iso, today


# ------------------------------------------------------------------ calendar (iCalendar feed)
def _esc(text: str) -> str:
    return str(text).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _dt(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S")  # floating local time — your calendar shows it as-is


def calendar_ics(db: Database | None = None) -> str:
    db = db or get_db()
    start = (today() - timedelta(days=30)).isoformat()
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//AMO//Studio//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:AMO", "X-PUBLISHED-TTL:PT15M", "REFRESH-INTERVAL;VALUE=DURATION:PT15M"]
    stamp = _dt(local_now())
    sessions = db.all(
        "SELECT s.*, c.name, c.artist_name, c.phone FROM studio_sessions s JOIN clients c ON c.id = s.client_id "
        "WHERE s.starts_at >= ? AND s.status IN ('booked', 'completed') ORDER BY s.starts_at", (start,))
    for s in sessions:
        who = s["artist_name"] or s["name"]
        if len(s["starts_at"]) > 10:
            begin = datetime.fromisoformat(s["starts_at"])
            end = begin + timedelta(hours=s["hours"] or 1)
            when = [f"DTSTART:{_dt(begin)}", f"DTEND:{_dt(end)}"]
        else:
            d = date.fromisoformat(s["starts_at"][:10])
            when = [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"]
        price = f" · ${s['hours'] * s['rate']:,.0f}" if s["rate"] else ""
        desc = f"{s['service']} · {s['hours']:g}h{price}" + (f"\\nPhone: {s['phone']}" if s["phone"] else "") + \
               (f"\\n{s['notes']}" if s["notes"] else "")
        title = f"Studio: {who} ({s['service']})"
        lines += ["BEGIN:VEVENT", f"UID:session-{s['id']}@amo", f"DTSTAMP:{stamp}", *when,
                  f"SUMMARY:{_esc(title)}", f"DESCRIPTION:{desc}", "END:VEVENT"]
    for t in db.all("SELECT * FROM cravvr_tasks WHERE status != 'done' AND due_date IS NOT NULL"):
        d = date.fromisoformat(t["due_date"][:10])
        lines += ["BEGIN:VEVENT", f"UID:cravvr-{t['id']}@amo", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}",
                  f"SUMMARY:{_esc('Cravvr: ' + t['title'])}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def subscribe_calendar(url: str = "webcal://localhost:8765/calendar.ics") -> str:
    """Opens Apple Calendar's 'subscribe' dialog for AMO's feed (macOS)."""
    if sys.platform == "darwin":
        subprocess.run(["open", url], check=False)
        return "Calendar should be asking you to subscribe — set Auto-refresh to every 15 minutes."
    return f"Subscribe to {url.replace('webcal', 'http')} in your calendar app."


# ------------------------------------------------------------------ iMessage (with your OK)
PENDING_KEY = "pending_text"
PENDING_MINUTES = 10


def draft_text(client: str, message: str, db: Database | None = None) -> dict[str, Any]:
    from .crm import StudioCRM

    db = db or get_db()
    c = StudioCRM(db).resolve_client(client)
    if not c.get("phone"):
        raise ValueError(f"I don't have a phone number for {c['name']} — add one first.")
    draft = {"client": c["artist_name"] or c["name"], "name": c["name"], "phone": c["phone"],
             "message": message.strip(), "at": now_iso()}
    db.set_kv(PENDING_KEY, json.dumps(draft))
    return {**draft, "drafted": True}


def pending_text(db: Database | None = None) -> dict[str, Any] | None:
    db = db or get_db()
    raw = db.get_kv(PENDING_KEY)
    if not raw:
        return None
    draft = json.loads(raw)
    if datetime.fromisoformat(draft["at"]) < local_now() - timedelta(minutes=PENDING_MINUTES):
        db.execute("DELETE FROM kv WHERE key = ?", (PENDING_KEY,))
        return None
    return draft


def cancel_text(db: Database | None = None) -> dict[str, Any]:
    db = db or get_db()
    had = pending_text(db)
    db.execute("DELETE FROM kv WHERE key = ?", (PENDING_KEY,))
    return {"cancelled": bool(had), "client": had["client"] if had else None}


_SEND_SCRIPT = """on run argv
    set thePhone to item 1 of argv
    set theText to item 2 of argv
    tell application "Messages"
        set svc to 1st account whose service type = iMessage
        send theText to participant thePhone of svc
    end tell
end run"""


def send_pending_text(db: Database | None = None, sender=None) -> dict[str, Any]:
    """Send the drafted text (only ever after you said "send it")."""
    db = db or get_db()
    draft = pending_text(db)
    if not draft:
        raise ValueError("There's no text waiting to be sent.")
    send = sender or _imessage
    send(draft["phone"], draft["message"])
    db.execute("DELETE FROM kv WHERE key = ?", (PENDING_KEY,))
    return {"sent": True, "client": draft["client"], "message": draft["message"]}


def _imessage(phone: str, message: str) -> None:
    if sys.platform != "darwin" or not shutil.which("osascript"):
        raise ValueError("Texting works on the Mac (it uses the Messages app).")
    r = subprocess.run(["osascript", "-e", _SEND_SCRIPT, phone, message], capture_output=True, text=True)
    if r.returncode != 0:
        raise ValueError("Messages couldn't send it — make sure you're signed in to iMessage, and allow "
                         f"Terminal/AMO to control Messages if macOS asks. ({r.stderr.strip()[:120]})")


# ------------------------------------------------------------------ backups
def backup_dir() -> Path:
    if settings.backup_dir:
        return Path(settings.backup_dir).expanduser()
    icloud = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    if icloud.is_dir():
        return icloud / "AMO Backups"
    return PROJECT_ROOT / "backups"


def backup(db: Database | None = None, keep: int = 14) -> Path:
    """Consistent copy of the live database (safe while AMO is running)."""
    db = db or get_db()
    folder = backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"amo-{local_now():%Y-%m-%d}.db"
    with db._lock:
        dest = sqlite3.connect(target)
        try:
            db.conn.backup(dest)
        finally:
            dest.close()
    for old in sorted(folder.glob("amo-*.db"))[:-keep]:
        old.unlink()
    return target


def restore(path: str, db_path: str | None = None) -> Path:
    """Replace AMO's database with a backup (the current one is kept as .before-restore)."""
    src = Path(path).expanduser()
    if not src.is_file():
        raise ValueError(f"no backup at {src}")
    live = Path(db_path or settings.db_path)
    if live.exists():
        shutil.copy2(live, live.with_suffix(".before-restore.db"))
    for suffix in ("-wal", "-shm"):
        Path(str(live) + suffix).unlink(missing_ok=True)
    shutil.copy2(src, live)
    return live


def nightly_backup(db: Database, now: datetime) -> bool:
    """Scheduler job: once a day after 3am."""
    if now.hour < 3 or db.get_kv("last_backup") == now.date().isoformat():
        return False
    backup(db)
    db.set_kv("last_backup", now.date().isoformat())
    return True
