"""Trading coach: checks trades against your own rules and The Strat, before and after.

Your rules are plain English (trading_rules table). Common kinds are checked automatically:
  "no trades in the first 5 minutes"     "no trades after 11am" / "before 10am"
  "max 3 trades a day"                   "always use a stop"
  "at least 2 to 1" / "minimum 2R"       "only trade with continuity" / "FTFC"
Anything else is read back to you as a reminder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from typing import Any

from .db import Database, get_db

OPEN = dtime(9, 30)  # US market open (New York)


@dataclass
class Rule:
    text: str
    kind: str                    # first_minutes | after | before | max_per_day | stop | min_rr | continuity | reminder
    value: Any = None


@dataclass
class Check:
    issues: list[str] = field(default_factory=list)     # broken rules / against continuity
    good: list[str] = field(default_factory=list)       # what's in your favour
    reminders: list[str] = field(default_factory=list)  # rules AMO can't check automatically
    with_continuity: bool | None = None
    rr: float | None = None
    symbol: str = ""

    @property
    def verdict(self) -> str:
        return "against" if self.issues else "ok"


def _clock(h: str, m: str | None, ampm: str | None) -> dtime:
    hour = int(h)
    if ampm:
        ampm = ampm.lower()
        if ampm.startswith("p") and hour < 12:
            hour += 12
        if ampm.startswith("a") and hour == 12:
            hour = 0
    elif hour < 7:  # "after 3" during the trading day means 3pm
        hour += 12
    return dtime(hour, int(m or 0))


def parse_rule(text: str) -> Rule:
    t = text.lower()
    if m := re.search(r"first (\d+)\s*(?:min|minutes)", t):
        return Rule(text, "first_minutes", int(m.group(1)))
    if m := re.search(r"(?:no|don'?t|never|stop)\b.*\bafter (\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", t):
        return Rule(text, "after", _clock(m.group(1), m.group(2), m.group(3)))
    if m := re.search(r"(?:no|don'?t|never)\b.*\bbefore (\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", t):
        return Rule(text, "before", _clock(m.group(1), m.group(2), m.group(3)))
    if m := re.search(r"(?:max(?:imum)?|no more than|at most|only|limit(?: of)?)\s*(\d+)\s*trades?\s*(?:a|per|each)\s*day", t):
        return Rule(text, "max_per_day", int(m.group(1)))
    if re.search(r"\bstop(?:\s*loss)?\b", t) and re.search(r"always|must|every|use|have|need", t):
        return Rule(text, "stop")
    if m := re.search(r"(?:at least|minimum|min\.?|no less than)\s*(\d+(?:\.\d+)?)\s*(?:r\b|to 1|:1|-to-1|x)", t):
        return Rule(text, "min_rr", float(m.group(1)))
    if re.search(r"continuity|ftfc|with the trend|timeframes? (?:agree|align)", t):
        return Rule(text, "continuity")
    return Rule(text, "reminder")


def _ny_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return datetime.now()


def check_trade(symbol: str, side: str, entry: float | None = None, stop: float | None = None,
                target: float | None = None, when: datetime | None = None, db: Database | None = None,
                strat_read: dict[str, Any] | None = None, exclude_trade_id: int | None = None,
                planned: bool = False) -> Check:
    """planned=True: asking before a trade — a missing stop/target is a reminder, not a violation."""
    """Check a (planned or just-logged) trade against your rules and The Strat."""
    db = db or get_db()
    when = when or _ny_now()
    side = "long" if side.lower() in ("long", "buy", "call", "calls") else "short"
    c = Check()

    # ---- The Strat: is the trade with continuity?
    a = strat_read
    if a is None:
        try:
            from . import strat

            a = strat.analyze(symbol)
        except Exception:  # noqa: BLE001 — no market data: still check the rules
            a = None
    c.symbol = symbol.upper()
    if a is not None:
        from .strat import short_reason

        bull, bear = "bull" in a["bias"], "bear" in a["bias"]
        sym = c.symbol = a["symbol"].replace("=F", "").replace("-USD", "")
        reason = short_reason(a) if "continuity" in a else a.get("why", "")
        if (side == "long" and bull) or (side == "short" and bear):
            c.with_continuity = True
            c.good.append(f"with continuity — {a['bias']}, {reason}")
        elif bull or bear:
            c.with_continuity = False
            c.issues.append(f"against continuity — {sym} is {a['bias']} ({reason})")
        else:
            c.reminders.append(f"{sym} has no continuity right now — timeframes are split")
        if a.get("_setups") is not None:
            from .strat import take

            t = take(a)
            if t["best"] and t["best"]["direction"] == side:
                c.good.append(f"matches the {t['grade']}-grade {t['best']['name']} on the "
                              f"{ {'M': 'monthly', 'W': 'weekly', 'D': 'daily'}[t['best']['tf']] }")

    # ---- risk:reward
    if entry is not None and stop is not None and target is not None and entry != stop:
        c.rr = round(abs(target - entry) / abs(entry - stop), 2)
    if stop is not None and entry is not None:
        wrong_side = (side == "long" and stop >= entry) or (side == "short" and stop <= entry)
        if wrong_side:
            c.issues.append(f"your stop {stop:g} is on the wrong side of the entry {entry:g}")

    # ---- your rules
    rules = [parse_rule(r["rule"]) for r in db.all("SELECT rule FROM trading_rules WHERE active = 1")]
    today = when.date().isoformat()
    trades_today = db.scalar("SELECT COUNT(*) FROM trades WHERE substr(opened_at, 1, 10) = ?"
                             + (" AND id != ?" if exclude_trade_id else ""),
                             (today, exclude_trade_id) if exclude_trade_id else (today,))
    t_now = when.time()
    for r in rules:
        broken = False
        if r.kind == "first_minutes":
            opened = datetime.combine(when.date(), OPEN)
            broken = opened <= when < opened + timedelta(minutes=r.value)
        elif r.kind == "after":
            broken = t_now >= r.value
        elif r.kind == "before":
            broken = t_now < r.value
        elif r.kind == "max_per_day":
            broken = trades_today >= r.value
        elif r.kind == "stop":
            if stop is None and planned:
                c.reminders.append("decide your stop before you enter")
                continue
            broken = stop is None
        elif r.kind == "min_rr":
            broken = c.rr is not None and c.rr < r.value
            if c.rr is None and stop is not None and target is None:
                c.reminders.append(f"set a target to check your {r.value:g}:1 rule")
        elif r.kind == "continuity":
            broken = c.with_continuity is False
        else:
            c.reminders.append(f"your rule: {r.text}")
            continue
        if broken:
            c.issues.append(f"breaks your rule “{r.text}”" + (f" (it's {t_now.strftime('%-I:%M')})"
                                                               if r.kind in ("first_minutes", "after", "before") else ""))
        elif r.kind not in ("continuity",):
            c.good.append(f"follows “{r.text}”")
    return c


def summary(c: Check, symbol: str, side: str, planned: bool) -> str:
    """One readable paragraph."""
    sym = c.symbol or symbol.upper().replace("=F", "").replace("-USD", "")
    if planned:
        head = (f"I'd pass on that {side} in {sym}." if c.issues
                else f"The {side} in {sym} checks out.")
    else:
        head = ""
    parts = [head] if head else []
    if c.issues:
        parts.append("⚠ " + "; ⚠ ".join(c.issues) + ".")
    if c.good and (planned or not c.issues):
        parts.append("✓ " + "; ✓ ".join(c.good[:3]) + ".")
    if c.rr is not None:
        parts.append(f"Risk to reward {c.rr:g}:1.")
    if c.reminders and planned:
        parts.append("Remember: " + "; ".join(c.reminders[:2]) + ".")
    return " ".join(p for p in parts if p).strip()


# ------------------------------------------------------------------ reviews
def review(period: str = "week", db: Database | None = None) -> dict[str, Any]:
    from .finance import period_start
    from .trading import TradingJournal

    db = db or get_db()
    since = period_start(period).isoformat()
    tj = TradingJournal(db)
    st = tj.stats(since)
    closed = tj.closed_since(since)
    label = {"today": "today", "week": "this week", "month": "this month", "year": "this year"}.get(period, period)
    if not closed:
        text = f"No closed trades {label} yet."
        return {"text": text, "spoken": text, "stats": st}

    def money(v: float) -> str:
        return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"

    lines = [f"Trading {label}: {st['trades']} trades, net {money(st['net_pnl'])}"
             + (f", win rate {st['win_rate'] * 100:.0f}%" if st["win_rate"] is not None else "")
             + (f", average {st['avg_r']:+g}R" if st["avg_r"] is not None else "") + "."]
    with_c = [t for t in closed if t.get("with_continuity") == 1]
    against_c = [t for t in closed if t.get("with_continuity") == 0]
    if with_c or against_c:
        lines.append(f"With continuity: {len(with_c)} trades, {money(sum(t['pnl'] or 0 for t in with_c))}. "
                     f"Against: {len(against_c)} trades, {money(sum(t['pnl'] or 0 for t in against_c))}.")
    broke = [t for t in closed if t.get("followed_rules") == 0]
    if broke:
        lines.append(f"Breaking your rules cost {money(sum(t['pnl'] or 0 for t in broke))} over {len(broke)} trades.")
    elif st["rule_adherence"] is not None:
        lines.append("You followed your rules every time. Good.")
    setups = sorted(st["by_setup"].items(), key=lambda kv: kv[1]["pnl"], reverse=True)
    if len(setups) > 1:
        lines.append(f"Best setup: {setups[0][0]} ({money(setups[0][1]['pnl'])}); "
                     f"worst: {setups[-1][0]} ({money(setups[-1][1]['pnl'])}).")
    # one lesson
    if against_c and sum(t["pnl"] or 0 for t in against_c) < 0:
        lesson = "Lesson: your losses come from trading against continuity — wait for the timeframes to agree."
    elif broke and sum(t["pnl"] or 0 for t in broke) < 0:
        lesson = "Lesson: the rule breaks are what's costing you. Same rules next week, no exceptions."
    elif st["net_pnl"] > 0:
        lesson = "Lesson: keep doing exactly this — size up only on A-grade setups."
    else:
        lesson = "Lesson: fewer trades, only with continuity, until the numbers turn."
    lines.append(lesson)
    text = " ".join(lines)
    return {"text": text, "spoken": text, "stats": st}
