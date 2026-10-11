"""Background jobs, run inside the API server (no cron needed).

- every minute: studio session reminders (2h before)
- 08:00 daily: morning brief notification
- after 5 quiet minutes: learn facts from new conversations
- Sunday 20:00: weekly reflection (the "gets smarter every week" loop)
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from .config import settings
from .crm import StudioCRM
from .db import Database, get_db, local_now
from .learning import learn_from_conversations, weekly_reflection
from .llm import LLMError
from .notify import notify

log = logging.getLogger(__name__)

REMINDER_LEAD = timedelta(hours=2)
LEARN_IDLE = timedelta(minutes=5)
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
    """Today's brief (see briefing.py) — prepared ahead with weather, calendar, studio, goals, money."""
    from .briefing import today

    return today(db)["text"]


def tick(db: Database, now: datetime | None = None) -> list[str]:
    """Run whatever is due. Returns names of jobs that ran (for logging/tests)."""
    now = now or local_now()
    ran: list[str] = []
    if session_reminders(db, now):
        ran.append("reminders")

    today = now.date().isoformat()
    from . import briefing

    if briefing.due_to_prepare(db, now, BRIEF_HOUR):  # ready (with audio) before you wake up
        try:
            briefing.prepare(db, now)
            ran.append("brief_prepared")
        except Exception:  # noqa: BLE001 — the brief must never stop the scheduler
            log.exception("preparing the brief failed")
    if BRIEF_HOUR <= now.hour < 18 and db.get_kv("last_brief") != today:  # late is fine, not at 11pm
        notify("Morning brief", morning_brief_text(db), db=db)
        db.set_kv("last_brief", today)
        ran.append("brief")

    # Learn from new conversations once you've been quiet for a few minutes,
    # so learning never slows down a conversation in progress.
    pending = db.scalar("SELECT COUNT(*) FROM conversations WHERE learned = 0 AND role = 'user'")
    last_msg = db.scalar("SELECT MAX(created_at) FROM conversations")
    if settings.auto_learn and pending and last_msg and last_msg <= (now - LEARN_IDLE).isoformat():
        try:
            learn_from_conversations(db)
            ran.append("learn")
        except (LLMError, ValueError) as e:
            log.warning("learning skipped: %s", e)

    # Long-term recall: embed past conversations in the background once you've gone quiet.
    if last_msg and last_msg <= (now - LEARN_IDLE).isoformat():
        try:
            from .notes import index_missing
            from .recall import index_conversations

            if index_conversations(db) + index_missing(db):
                ran.append("recall_index")
        except Exception:  # noqa: BLE001 — recall indexing is best effort
            log.exception("conversation indexing failed")

    last_sync = db.get_kv("knowledge_synced")
    if not last_sync or last_sync <= (now - timedelta(minutes=5)).isoformat():  # your knowledge folder
        db.set_kv("knowledge_synced", now.isoformat(timespec="seconds"))
        try:
            from .knowledge import sync

            if any(sync(db).get(k) for k in ("added", "removed", "embedded")):
                ran.append("knowledge")
        except Exception:  # noqa: BLE001
            log.exception("knowledge sync failed")

    stale = (now - timedelta(minutes=10)).isoformat()
    if db.scalar("SELECT 1 FROM pocket_events WHERE status = 'received' AND created_at < ? LIMIT 1", (stale,)):
        try:  # the pet uploaded things but never said it was done — handle them anyway
            from .pocket import process

            process(db)
            ran.append("pocket")
        except Exception:  # noqa: BLE001
            log.exception("pocket processing failed")

    from .selfrepair import maybe_run as self_repair
    from .warmup import keep_warm

    try:
        if self_repair(db, now):  # quiet for a while + a recorded bug → try a tested fix
            ran.append("selfrepair")
    except Exception:  # noqa: BLE001
        log.exception("self-repair check failed")

    if keep_warm():  # Ollama dropped the model (restart / keep_alive) → load it again
        ran.append("warm")

    if now.hour >= 3 and db.get_kv("last_tidy") != today:  # nightly: merge duplicate / outdated facts
        try:
            from .memory import Memory

            Memory(db).tidy()
            db.set_kv("last_tidy", today)
            ran.append("tidy")
        except Exception:  # noqa: BLE001
            log.exception("memory tidy failed")

    from .integrations import nightly_backup
    from .proactive import run_jobs

    ran += run_jobs(db, now)
    try:
        if nightly_backup(db, now):
            ran.append("backup")
    except Exception:  # noqa: BLE001 — a failed backup must not stop the scheduler
        log.exception("nightly backup failed")

    week_key = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]}"
    if (now.weekday() == REFLECTION_WEEKDAY and now.hour >= REFLECTION_HOUR
            and db.get_kv("last_selfreview") != week_key):
        try:
            from .proactive import announce
            from .selfreview import review

            r = review(db, now)
            db.set_kv("last_selfreview", week_key)
            announce("Weekly self-review: " + r["text"], "checkin", f"selfreview:{week_key}",
                     ttl_minutes=180, db=db, now=now)
            ran.append("selfreview")
        except Exception:  # noqa: BLE001
            log.exception("self-review failed")
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
        self._thread = threading.Thread(target=self._run, name="amo-scheduler", daemon=True)
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
