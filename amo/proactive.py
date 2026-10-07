"""AMO speaking up on its own.

Jobs run from the scheduler; anything worth saying goes into the `announcements` queue, which the
hands-free listener (Mac) or a body device speaks when you're not mid-conversation. Everything is
also posted as a dashboard notification (and phone push, if set up). Quiet hours are respected.

- Strat watchlist alerts (market hours): a setup with continuity triggers, or an A-grade forms
- Studio: spoken heads-up before sessions, end-of-day unpaid balances, quiet clients on Mondays
- Daily rhythm: the morning brief, out loud; an evening check-in
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from .config import settings
from .db import Database, get_db, local_now, now_iso

log = logging.getLogger(__name__)

STRAT_EVERY = timedelta(minutes=15)


# ------------------------------------------------------------------ the queue
def announce(text: str, kind: str = "info", key: str | None = None, ttl_minutes: int = 90,
             db: Database | None = None, push: bool = True, now: datetime | None = None) -> bool:
    """Queue something for AMO to say. `key` makes it one-time (the same alert never repeats).
    Returns False if it was already announced."""
    db = db or get_db()
    if key and db.scalar("SELECT 1 FROM announcements WHERE key = ?", (key,)):
        return False
    now = now or local_now()
    expires = (now + timedelta(minutes=ttl_minutes)).isoformat()
    db.insert("announcements", {"text": text, "kind": kind, "key": key, "created_at": now.isoformat(),
                                "expires_at": expires})
    if push:
        from .notify import notify

        title = {"alert": "Strat alert", "reminder": "Reminder", "brief": "Morning brief",
                 "checkin": "Check-in"}.get(kind, "AMO")
        notify(title, text, "warn" if kind == "alert" else "info", db=db)
    return True


def quiet_now(now: datetime | None = None) -> bool:
    """Inside AMO_QUIET_HOURS (e.g. "22-8") nothing is spoken; it waits until morning."""
    spec = settings.quiet_hours.strip()
    if not spec or "-" not in spec:
        return False
    start, end = (int(x) for x in spec.split("-", 1))
    h = (now or local_now()).hour
    return (start <= h or h < end) if start > end else (start <= h < end)


def take_pending(db: Database | None = None, now: datetime | None = None) -> list[str]:
    """Things to say now (oldest first), marked as spoken. Expired ones are dropped."""
    db = db or get_db()
    now = now or local_now()
    if quiet_now(now):
        return []
    rows = db.all("SELECT id, text, expires_at FROM announcements WHERE spoken_at IS NULL ORDER BY id")
    out = []
    for r in rows:
        db.update("announcements", r["id"], {"spoken_at": now.isoformat()})
        if not r["expires_at"] or r["expires_at"] >= now.isoformat():
            out.append(r["text"])
    return out


# ------------------------------------------------------------------ jobs
def market_open(now_local: datetime) -> bool:
    """US stock market hours (Mon–Fri 9:30–16:00 New York)."""
    try:
        from zoneinfo import ZoneInfo

        ny = datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        ny = now_local
    if ny.weekday() >= 5:
        return False
    minutes = ny.hour * 60 + ny.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def _always_open(symbol: str) -> bool:
    return symbol.endswith("-USD")  # crypto trades 24/7


def strat_watch(db: Database, now: datetime, force: bool = False) -> int:
    """Check the watchlist; announce new triggers and new A-grade setups (with continuity)."""
    from . import strat
    from .strat import TF_NAMES, _fmt_factory, risk_reward

    rows = db.all("SELECT symbol FROM watchlist WHERE alerts = 1")
    if not rows:
        return 0
    last = db.get_kv("last_strat_watch")
    if not force and last and datetime.fromisoformat(last) > now - STRAT_EVERY:
        return 0
    db.set_kv("last_strat_watch", now.isoformat())
    is_open = market_open(now)
    said = 0
    for r in rows:
        sym = r["symbol"]
        if not (is_open or _always_open(sym) or force):
            continue
        try:
            a = strat.analyze(sym)
        except Exception as e:  # noqa: BLE001 — one bad ticker shouldn't stop the rest
            log.warning("strat watch %s: %s", sym, e)
            continue
        fmt = _fmt_factory(a["price"])
        name = a["symbol"].replace("=F", " futures").replace("-USD", "")
        today = now.date().isoformat()
        for s in a["_setups"]:
            if not s.with_continuity:
                continue
            verb = "above" if s.direction == "long" else "below"
            tgt = f", target {fmt(s.target)}" if s.target is not None else ""
            if s.status == "triggered":
                text = (f"Heads up: {name} just triggered the {TF_NAMES[s.tf].lower()} {s.name} {verb} "
                        f"{fmt(s.trigger)}, with continuity{tgt}, stop {fmt(s.stop)}.")
                key = f"trig:{a['symbol']}:{s.tf}:{s.name}:{s.trigger}:{today if s.tf == 'D' else ''}"
                said += announce(text, "alert", key, ttl_minutes=45, db=db, now=now)
        t = strat.take(a)
        if t["grade"] == "A" and t["best"] and t["best"]["status"] == "waiting":
            b = t["best"]
            key = f"agrade:{a['symbol']}:{b['tf']}:{b['name']}:{b['trigger']}"
            text = f"{name}: {t['verdict'].split(' Why:')[0]}"
            said += announce(text, "alert", key, ttl_minutes=120, db=db, now=now)
    return said


def studio_nudges(db: Database, now: datetime) -> int:
    from .crm import StudioCRM

    crm = StudioCRM(db)
    said = 0
    # spoken heads-up ~15 minutes before a session
    soon = db.all("SELECT id FROM studio_sessions WHERE status = 'booked' AND length(starts_at) > 10 "
                  "AND starts_at > ? AND starts_at <= ?",
                  (now.isoformat(), (now + timedelta(minutes=16)).isoformat()))
    for r in soon:
        s = crm.get_session(r["id"])
        who = s["artist_name"] or s["client_name"]
        said += announce(f"{who}'s {s['service']} session starts at {s['starts_at'][11:16]}, about 15 minutes from now.",
                         "reminder", f"sess15:{r['id']}", ttl_minutes=20, db=db, now=now)
    # end of day: unpaid balances
    if now.hour >= 18:
        unpaid = crm.unpaid_sessions()
        if unpaid:
            owed = sum(u["total"] - u["paid"] for u in unpaid)
            names = ", ".join(dict.fromkeys(u["client_name"] for u in unpaid))
            said += announce(f"End of day: ${owed:,.0f} is still owed for studio sessions — {names}.",
                             "reminder", f"unpaid:{now.date()}", ttl_minutes=180, db=db, now=now)
    # Monday morning: clients who've gone quiet
    if now.weekday() == 0 and 10 <= now.hour < 13:
        quiet = crm.inactive_clients(45)
        if quiet:
            names = ", ".join(c["artist_name"] or c["name"] for c in quiet[:5])
            said += announce(f"Clients you haven't seen in over six weeks: {names}. Worth a message?",
                             "reminder", f"quiet:{now.isocalendar()[0]}-{now.isocalendar()[1]}",
                             ttl_minutes=240, db=db, now=now)
    return said


def daily_rhythm(db: Database, now: datetime) -> int:
    from .goals import Goals
    from .scheduler import BRIEF_HOUR, morning_brief_text
    from .voice.listen import brief_for_speech

    said = 0
    today = now.date().isoformat()
    if BRIEF_HOUR <= now.hour < 12:
        said += announce(brief_for_speech(morning_brief_text(db)), "brief", f"brief:{today}",
                         ttl_minutes=(12 - now.hour) * 60, db=db, push=False, now=now)
    if now.hour >= settings.checkin_hour:
        goals = Goals(db).today_list()
        if goals:
            done = sum(1 for g in goals if g["done_today"])
            left = [g["title"] for g in goals if not g["done_today"]]
            text = (f"Evening check-in: you've done {done} of {len(goals)} daily goals."
                    + (f" Still open: {', '.join(left[:3])}." if left else " All done — nice.")
                    + " Anything to log before tomorrow?")
            said += announce(text, "checkin", f"checkin:{today}", ttl_minutes=120, db=db, now=now)
    return said


def run_jobs(db: Database, now: datetime | None = None) -> list[str]:
    """Called by the scheduler every minute."""
    now = now or local_now()
    ran = []
    if not settings.proactive:
        return ran
    for name, job in (("strat_watch", strat_watch), ("studio", studio_nudges), ("rhythm", daily_rhythm)):
        try:
            if job(db, now):
                ran.append(name)
        except Exception:  # noqa: BLE001 — a failing job must never stop the scheduler
            log.exception("proactive job %s failed", name)
    return ran


def watchlist_symbols(db: Database | None = None) -> list[str]:
    return [r["symbol"] for r in (db or get_db()).all("SELECT symbol FROM watchlist ORDER BY id")]


def add_to_watchlist(symbols: list[str], db: Database | None = None) -> dict[str, Any]:
    from .market import lookup

    db = db or get_db()
    added, already, bad = [], [], []
    have = set(watchlist_symbols(db))
    for raw in symbols:
        try:
            sym = lookup(raw)
        except Exception:  # noqa: BLE001
            bad.append(raw)
            continue
        if sym in have:
            already.append(sym)
            continue
        db.insert("watchlist", {"symbol": sym, "created_at": now_iso()})
        have.add(sym)
        added.append(sym)
    return {"added": added, "already": already, "not_found": bad, "watchlist": watchlist_symbols(db)}


def remove_from_watchlist(symbols: list[str], db: Database | None = None) -> dict[str, Any]:
    from .market import normalize

    db = db or get_db()
    removed = []
    for raw in symbols:
        sym = normalize(raw)
        hit = db.scalar("SELECT symbol FROM watchlist WHERE symbol = ? OR symbol = ?", (sym, raw.upper()))
        if hit:
            db.execute("DELETE FROM watchlist WHERE symbol = ?", (hit,))
            removed.append(hit)
    return {"removed": removed, "watchlist": watchlist_symbols(db)}
