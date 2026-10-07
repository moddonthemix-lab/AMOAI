"""Background jobs, run inside the API server (no cron needed).

- every minute: studio session reminders (2h before)
- 08:00 daily: morning brief notification
- hourly: catch-up fact learning
- Sunday 20:00: weekly reflection (the "gets smarter every week" loop)
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from .crm import StudioCRM
from .db import Database, get_db, local_now
from .finance import dashboard
from .learning import learn_from_conversations, weekly_reflection
from .llm import LLMError
from .notify import notify

log = logging.getLogger(__name__)

REMINDER_LEAD = timedelta(hours=2)
BRIEF_HOUR = 8
REFLECTION_WEEKDAY, REFLECTION_HOUR = 6, 20  # Sunday 8pm


def session_reminders(db: Database, now: datetime | None = None) -> int:
    now = now or local_now()
    rows = db.all(
        "SELECT s.id FROM studio_sessions s WHERE s.status = 'booked' AND s.reminded = 0 "
        "AND length(s.starts_at) > 10 AND s.starts_at >= ? AND s.starts_at <= ?",
        (now.isoformat(), (now + REMINDER_LEAD).isoformat()),
    )
    crm = StudioCRM(db)
    for r in rows:
        s = crm.get_session(r["id"])
        who = s["artist_name"] or s["client_name"]
        body = f"{who} at {s['starts_at'][11:16]} — {s['service']}, {s['hours']:g}h"
        if s["phone"]:
            body += f"\nText them: \"Hey {s['client_name'].split()[0]}, see you at the studio at " \
                    f"{s['starts_at'][11:16]} today.\" ({s['phone']})"
        notify("Studio session soon", body, "info", db=db)
        db.update("studio_sessions", r["id"], {"reminded": 1})
    return len(rows)


def morning_brief_text(db: Database) -> str:
    d = dashboard(db)
    lines = [f"{d['weekday']} {d['date']}"]
    if d["studio"]["today"]:
        lines.append("Studio: " + ", ".join(f"{s['time']} {s['client']}" for s in d["studio"]["today"]))
    else:
        lines.append("Studio: no sessions booked today")
    if d["studio"]["unpaid"]:
        lines.append(f"{d['studio']['unpaid']} session(s) still unpaid")
    todo = [g["title"] for g in d["goals"] if not g["done"]]
    if todo:
        lines.append("Goals: " + "; ".join(todo))
    rev = d["revenue"]
    month = f"Month so far: ${rev['month']:,.0f}"
    if rev.get("month_target"):
        month += f" of ${rev['month_target']:,.0f} ({rev['month_pct'] * 100:.0f}%)"
    lines.append(month)
    if d["trading"]["open_positions"]:
        lines.append(f"{d['trading']['open_positions']} open trade(s)")
    if d["reselling"]["stale"]:
        lines.append(f"{d['reselling']['stale']} resale item(s) sitting 30+ days — consider a price drop")
    if d["cravvr"]["open_tasks"]:
        lines.append(f"Cravvr: {d['cravvr']['open_tasks']} open task(s)")
    return "\n".join(lines)


def tick(db: Database, now: datetime | None = None) -> list[str]:
    """Run whatever is due. Returns names of jobs that ran (for logging/tests)."""
    now = now or local_now()
    ran: list[str] = []
    if session_reminders(db, now):
        ran.append("reminders")

    today = now.date().isoformat()
    if now.hour >= BRIEF_HOUR and db.get_kv("last_brief") != today:
        notify("Morning brief", morning_brief_text(db), db=db)
        db.set_kv("last_brief", today)
        ran.append("brief")

    hour_key = now.strftime("%Y-%m-%dT%H")
    if db.get_kv("last_learn") != hour_key:
        db.set_kv("last_learn", hour_key)
        try:
            learn_from_conversations(db)
            ran.append("learn")
        except (LLMError, ValueError) as e:
            log.warning("learning skipped: %s", e)

    week_key = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]}"
    if (now.weekday() == REFLECTION_WEEKDAY and now.hour >= REFLECTION_HOUR
            and db.get_kv("last_reflection") != week_key):
        try:
            r = weekly_reflection(db)
            db.set_kv("last_reflection", week_key)
            notify("Weekly reflection", r["summary"][:1500], db=db)
            ran.append("reflection")
        except (LLMError, ValueError) as e:
            log.warning("weekly reflection failed, will retry: %s", e)
    return ran


class Scheduler:
    def __init__(self, db: Database | None = None, interval: float = 60):
        self.db = db or get_db()
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="modd-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                tick(self.db)
            except Exception:  # keep the scheduler alive no matter what
                log.exception("scheduler tick failed")
            self._stop.wait(self.interval)
