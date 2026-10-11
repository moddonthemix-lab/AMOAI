"""Everyday requests about your own data, done directly — instant, and never misread by the AI.

Built from what the AI got wrong in `amo eval`:
    "I finished my beat today" / "done with the gym"        check in a goal
    "put calling my mom on my daily goals"                  add a goal
    "rename my gym goal to gym 5 days a week"               rename
    "Lil Jay paid 150 cash app" / "log that Ty paid me 100" record a payment
    "bought / sold / listed the Jordan 4s …"                reselling
    "long TSLA at 250 stop 245" / "closed my NQ at 18040"   trading journal
    "add a rule: no trades in the first 5 minutes"
    "what are my goals", "who owes me", "what's my schedule tomorrow", "what's in my inventory",
    "what are my open positions", "what are my trading rules", "what's open for Cravvr"

Each one checks that the thing you named exists (a client, a goal, an item, an open trade) before
taking over; otherwise the AI handles it as before.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

from .db import Database

_N = r"\$?(?P<n>\d[\d,]*(?:\.\d+)?)"
_LEAD = r"^(?:(?:hey )?amo[ ,]*)?(?:ok(?:ay)?[ ,]+)?(?:please |can you |could you |go ahead and )?"
_STOP = {"the", "my", "a", "an", "of", "pair", "for", "today", "goal", "goals", "some"}


def _num(raw: str) -> float:
    return float(raw.replace(",", ""))


def _words(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in _STOP:
            continue
        plural = w.endswith("s") and not w.endswith("ss") and (len(w) > 3 or w[:-1].isdigit())  # "beats", "4s"
        out.append(w[:-1] if plural else w)
    return out


def _covers(want: str, name: str) -> bool:
    """Every word you said is (the start of) a word in the name: 'Jordan 4s' ↔ 'Jordan 4 Bred size 10'."""
    have = _words(name)
    ws = _words(want)
    return bool(ws) and all(any(h.startswith(w) or w.startswith(h) and len(h) > 2 for h in have) for w in ws)


def _one(rows: list[dict[str, Any]], want: str, field: str) -> dict[str, Any] | None:
    hits = [r for r in rows if _covers(want, r[field] or "")]
    return hits[0] if len(hits) == 1 else None


def _client(who: str, db: Database) -> dict[str, Any] | None:
    clients = db.all("SELECT id, name, artist_name FROM clients")
    hit = [c for c in clients if _covers(who, c["name"]) or _covers(who, c["artist_name"] or "")]
    return hit[0] if len(hit) == 1 else None


def _when(phrase: str, current: str) -> str | None:
    """'Saturday at 6pm', 'tomorrow', '8' → 'YYYY-MM-DD HH:MM' (same time as before if only a day is said,
    same day if only a time is said)."""
    from .db import local_now
    from .timers import _CLOCK_RE, _DAY_RE, parse_reminder_request

    now = local_now()
    has_clock, has_day = bool(_CLOCK_RE.search("at " + phrase)), bool(_DAY_RE.search(phrase))
    if not has_clock and not has_day:
        return None
    old = datetime.fromisoformat(current) if len(current) > 10 else None
    if has_clock:
        text = "remind me " + (phrase if re.search(r"\b(?:at|by)\b", phrase) or has_day else "at " + phrase)
        if not has_day and old:  # "move it to 8" — same day
            text += " on " + old.strftime("%A").lower() if old.date() > now.date() else ""
        r = parse_reminder_request(text, now)
        return r["due"].strftime("%Y-%m-%d %H:%M") if r else None
    r = parse_reminder_request("remind me " + phrase, now)  # a day only: keep the time
    if not r:
        return None
    return r["due"].date().isoformat() + (old.strftime(" %H:%M") if old else "")


# ------------------------------------------------------------------ actions
CHECKIN_RE = re.compile(
    _LEAD + r"(?:i\s+)?(?:just\s+)?(?:finished|did|completed|knocked out|crushed|hit|got in|done with|i'?m done with)\s+"
    r"(?:my\s+|the\s+)?(?P<goal>[\w' -]{2,40}?)(?:\s+(?:today|for today|already|just now|this morning|tonight))?"
    r"[.!]*$", re.I)
ADD_GOAL_RE = re.compile(
    _LEAD + r"(?:put|add)\s+[\"'“‘]?(?P<title>.+?)[\"'”’]?\s+(?:on|to|in)\s+(?:my\s+|the\s+)?"
    r"(?:(?P<cad>daily|weekly|monthly)\s+)?goals?(?:\s+list)?(?:\s+please)?[.!]*$", re.I)
RENAME_RE = re.compile(
    _LEAD + r"rename\s+(?:my\s+|the\s+)?(?P<old>.+?)\s+(?P<kind>goal|cravvr task|task)\s+(?:to|as)\s+"
    r"[\"'“]?(?P<new>.+?)[\"'”]?[.!]*$", re.I)
PAID_RE = re.compile(
    _LEAD + r"(?:log(?: that)?\s+|record(?: that)?\s+)?(?P<who>[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*){0,2})\s+"
    r"(?:paid|sent|cashapped|zelled|venmoed)\s+(?:me\s+)?" + _N + r"(?:\s+dollars|\s+bucks)?"
    r"(?:\s+(?:in|with|via|on|through|by|over)?\s*(?P<method>cash ?app|cash|zelle|venmo|paypal|apple pay|card|check))?"
    r"(?:\s+(?:for|today).*)?[.!]*$")
BOUGHT_RE = re.compile(
    _LEAD + r"(?:i\s+)?(?:just\s+)?(?:bought|copped|picked up)\s+(?:a pair of\s+|a\s+|an\s+|some\s+|the\s+)?"
    r"(?P<name>.+?)\s+for\s+" + _N + r"(?:\s+dollars|\s+bucks)?(?:\s+(?:on|from|at)\s+(?P<src>[\w .]+?))?[.!]*$", re.I)
SOLD_RE = re.compile(
    _LEAD + r"(?:i\s+)?(?:just\s+)?sold\s+(?:the\s+|my\s+|a\s+|an\s+)?(?P<item>.+?)(?:\s+on\s+(?P<plat>[\w.]+))?\s+for\s+"
    + _N + r"(?:\s+(?:with|and|plus)\s+" + r"\$?(?P<fees>\d[\d,]*(?:\.\d+)?)\s+(?:in\s+)?fees?)?"
    r"(?:\s+on\s+(?P<plat2>[\w.]+))?[.!]*$", re.I)
LISTED_RE = re.compile(
    _LEAD + r"(?:i\s+)?(?:just\s+)?(?:listed|put up)\s+(?:the\s+|my\s+)?(?P<item>.+?)(?:\s+on\s+(?P<plat>[\w.]+))?"
    r"\s+(?:for|at)\s+" + _N + r"(?:\s+on\s+(?P<plat2>[\w.]+))?[.!]*$", re.I)
TRADE_RE = re.compile(
    _LEAD + r"(?:i'?m\s+|i\s+)?(?:went\s+|got\s+|took\s+(?:a\s+)?|entered\s+(?:a\s+)?)?(?P<side>long|short)\s+"
    r"(?:(?P<qty>\d+)\s+(?:contracts?\s+(?:of\s+)?|shares?\s+(?:of\s+)?|x\s+)?)?(?P<sym>[A-Za-z]{1,5}(?:=F|1!)?)\s+"
    r"(?:at|@|from)\s+" + _N +
    r"(?:.*?\bstop\s+(?:at\s+|loss\s+(?:at\s+)?)?(?P<stop>\d[\d,]*(?:\.\d+)?))?"
    r"(?:.*?\b(?:target|tp|take profit)\s+(?:at\s+|of\s+)?(?P<target>\d[\d,]*(?:\.\d+)?))?[.!]*$", re.I)
CLOSE_RE = re.compile(
    _LEAD + r"(?:i\s+)?(?:just\s+)?(?:closed|exited|got out of|covered|took profit on|stopped out of)\s+"
    r"(?:my\s+|the\s+)?(?P<sym>[A-Za-z]{1,5}(?:=F|1!)?)(?:\s+(?:trade|position|long|short))?\s+(?:at|@|for)\s+"
    + _N + r"[.!]*$", re.I)
SESSION_RE = re.compile(
    _LEAD + r"(?P<verb>cancel|move|push|reschedule)\s+(?P<who>[\w' .-]+?)(?:'s|s')\s+(?:next\s+)?session"
    r"(?:\s+(?:to|until|till)\s+(?P<when>.+?))?[.!]*$", re.I)
MADE_RE = re.compile(
    r"^(?:how much (?:money )?(?:did i|have i) (?:make|made|bring in|brought in)|what(?:'s| is| was) my (?:revenue|income))"
    r"(?:\s+(?:this|so far this|for the|in the)?\s*(?P<period>today|week|month|year))?(?: so far)?\??$", re.I)
_DAYS = r"(?:today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
BOOK_RE = re.compile(
    _LEAD + r"book\s+(?:a\s+(?:session|slot)\s+(?:for|with)\s+)?(?P<who>[\w' .-]+?)\s+(?:in\s+)?"
    r"(?P<when>(?:(?:on|next|this)\s+)?" + _DAYS + r"\b.*?|at\s+\d.*?)"
    r"(?:\s+for\s+(?P<hours>\d+(?:\.\d+)?)\s+(?:hours?|hrs?))?"
    r"(?:\s+(?:at|for)\s+\$?(?P<rate>\d+(?:\.\d+)?)\s*(?:an?\s+hour|per\s+hour|/\s*hr|/\s*hour|an\s+hr|hourly))?[.!]*$",
    re.I)
# "bought lunch for 15" isn't inventory — only things that look like a flip
_RESALE = re.compile(r"\b(?:size|sz|jordans?|yeezys?|dunks?|nike|adidas|new balance|asics|supreme|bape|travis|"
                     r"off-?white|ps5|playstation|xbox|switch|iphone|ipad|airpods|pokemon|cards?|funko|lego)\b", re.I)
_FLIP = re.compile(r"\b(?:to (?:re)?sell|to flip|for (?:re)?sale|for inventory|for the (?:re)?sell)", re.I)
CRAVVR_RE = re.compile(
    _LEAD + r"(?:add|new|make)\s+(?:a\s+)?(?:new\s+)?cravvr\s+(?:task|to-?do)\s*[:\-—]?\s*(?P<title>.+?)"
    r"(?:,?\s+(?P<prio>high|top|urgent|low)\s+priority)?[.!]*$", re.I)
RULE_RE = re.compile(_LEAD + r"(?:add|new|make|save)\s+(?:a\s+)?(?:new\s+)?(?:trading\s+)?rule\s*[:\-—]\s*(?P<rule>.{4,})$",
                     re.I)

# ------------------------------------------------------------------ lookups
_ASK = r"^(?:(?:hey )?amo[ ,]*)?(?:what are|what're|what's|whats|what is|show(?: me)?|list|read(?: me)?|tell me|give me)\s+"
LOOKUPS: list[tuple[re.Pattern, str]] = [
    (re.compile(_ASK + r"(?:all\s+)?my\s+(?:(?P<cad>daily|weekly|monthly)\s+)?goals\??$"
                r"|^how(?:'re| are) my goals(?: going| looking)?\??$", re.I), "list_goals"),
    (re.compile(r"^who (?:still )?owes me(?: money| anything)?\??$|^who hasn'?t paid(?: me)?(?: yet)?\??$|"
                + _ASK + r"(?:my\s+)?unpaid sessions\??$", re.I), "unpaid_sessions"),
    (re.compile(_ASK + r"(?:on\s+)?my\s+(?:studio\s+)?schedule(?:\s+(?:for\s+)?(?P<day>today|tomorrow|tonight))?\??$"
                r"|^what(?:'s| is) on (?:the books |my calendar )?(?P<day2>today|tomorrow|tonight)\??$"
                r"|^who(?:'s| is) (?:coming in|booked|in the studio) (?P<day3>today|tomorrow|tonight)\??$", re.I),
     "studio_schedule"),
    (re.compile(_ASK + r"(?:in\s+)?my\s+(?:resale\s+)?inventory\??$|^what do i have in stock\??$",
                re.I), "resale_inventory"),
    (re.compile(_ASK + r"my\s+open\s+(?:positions|trades)\??$|^(?:am i in|do i have) any (?:open )?trades\??$", re.I),
     "open_positions"),
    (re.compile(_ASK + r"my\s+(?:trading\s+)?rules\??$", re.I), "trading_rules"),
    (re.compile(r"^what(?:'s| is) (?:open|left|pending|on deck) (?:for|on|in) cravvr\??$|"
                + _ASK + r"(?:my\s+|the\s+)?(?:open\s+)?cravvr tasks\??$", re.I), "cravvr_tasks"),
]


def match(text: str, db: Database, now: datetime | None = None) -> tuple[str, dict[str, Any]] | None:
    t = (text or "").strip()
    if not t:
        return None
    for rx, tool in LOOKUPS:
        m = rx.match(t)
        if m:
            args: dict[str, Any] = {}
            gd = {k: v for k, v in m.groupdict().items() if v}
            if tool == "studio_schedule":
                day = (gd.get("day") or gd.get("day2") or gd.get("day3") or "today").lower()
                args["day"] = "today" if day == "tonight" else day
            return tool, args
    m = MADE_RE.match(t)
    if m:
        return "revenue", {"period": (m.group("period") or "month").lower()}
    return _action(t, db)


def _action(t: str, db: Database) -> tuple[str, dict[str, Any]] | None:
    m = RULE_RE.match(t)
    if m:
        return "add_trading_rule", {"rule": m.group("rule").strip().rstrip(".")}

    m = ADD_GOAL_RE.match(t)
    if m:
        title = m.group("title").strip(" \"'“”‘’")
        if 1 <= len(title.split()) <= 10:
            return "add_goal", {"title": title[:1].upper() + title[1:], "cadence": (m.group("cad") or "daily").lower()}

    m = CHECKIN_RE.match(t)
    if m:
        g = _one(db.all("SELECT id, title FROM goals WHERE active = 1"), m.group("goal"), "title")
        if g:
            return "check_in_goal", {"goal": str(g["id"])}

    m = RENAME_RE.match(t)
    if m:
        kind = "goals" if m.group("kind").lower() == "goal" else "cravvr"
        table = "goals WHERE active = 1" if kind == "goals" else "cravvr_tasks WHERE status != 'done'"
        row = _one(db.all(f"SELECT id, title FROM {table}"), m.group("old"), "title")
        if row:
            return "update_record", {"kind": kind, "item": f"#{row['id']}", "changes": {"title": m.group("new").strip()}}

    m = BOOK_RE.match(t)
    if m:
        hit = _client(m.group("who"), db)
        when = _when(m.group("when"), "") if hit else None
        if hit and when and len(when) > 10:  # a client you have, and a day + time
            args = {"client": hit["name"], "starts_at": when}
            if m.group("hours"):
                args["hours"] = float(m.group("hours"))
            if m.group("rate"):
                args["rate"] = float(m.group("rate"))
            return "book_session", args

    m = CRAVVR_RE.match(t)
    if m:
        args = {"title": m.group("title").strip(" ,")}
        if m.group("prio"):
            args["priority"] = 2 if m.group("prio").lower() == "low" else 5
        return "add_cravvr_task", args

    m = SESSION_RE.match(t)
    if m:
        hit = _client(m.group("who"), db)
        if hit:
            booked = db.all("SELECT id, starts_at, hours FROM studio_sessions WHERE client_id = ? AND status = 'booked' "
                            "AND starts_at >= ? ORDER BY starts_at", (hit["id"], datetime.now().date().isoformat()))
            if len(booked) == 1:  # only when it's clear which session
                if m.group("verb").lower() == "cancel" and not m.group("when"):
                    return "update_session", {"session_id": booked[0]["id"], "status": "cancelled"}
                if m.group("when"):
                    when = _when(m.group("when"), booked[0]["starts_at"])
                    if when:
                        return "update_session", {"session_id": booked[0]["id"], "starts_at": when}

    m = PAID_RE.match(t)
    if m:
        who = m.group("who")
        hit = _client(who, db)
        if hit:
            args = {"amount": _num(m.group("n")), "client": hit["name"]}
            if m.group("method"):
                args["method"] = m.group("method").lower().replace("cashapp", "cash app")
            return "record_payment", args

    m = SOLD_RE.match(t)
    if m:
        item = _one(db.all("SELECT id, name FROM resale_items WHERE status != 'sold'"), m.group("item"), "name")
        if item:
            args = {"item": str(item["id"]), "sold_price": _num(m.group("n"))}
            if m.group("fees"):
                args["fees"] = _num(m.group("fees"))
            plat = m.group("plat") or m.group("plat2")
            if plat:
                args["platform"] = plat.lower()
            return "mark_item_sold", args

    m = LISTED_RE.match(t)
    if m:
        item = _one(db.all("SELECT id, name FROM resale_items WHERE status != 'sold'"), m.group("item"), "name")
        if item:
            args = {"item": str(item["id"]), "price": _num(m.group("n"))}
            plat = m.group("plat") or m.group("plat2")
            if plat:
                args["platform"] = plat.lower()
            return "list_resale_item", args

    m = BOUGHT_RE.match(t)
    if m and 1 <= len(m.group("name").split()) <= 8 and (_RESALE.search(m.group("name")) or _FLIP.search(t)):
        args = {"name": m.group("name").strip(), "cost": _num(m.group("n"))}
        if m.group("src"):
            args["source"] = m.group("src").strip()
        return "add_resale_item", args

    m = TRADE_RE.match(t)
    if m:
        from .fastpath import _tickers

        from .market import ALIASES

        raw = m.group("sym")
        syms = _tickers(raw, t + " trade")
        if syms and (raw.isupper() or raw.upper() in ALIASES):  # a real ticker, not "long day at work"
            args = {"symbol": syms[0].upper(), "side": m.group("side").lower(), "entry": _num(m.group("n"))}
            for k in ("stop", "target"):
                if m.group(k):
                    args[k] = _num(m.group(k))
            if m.group("qty"):
                args["quantity"] = float(m.group("qty"))
            return "log_trade", args

    m = CLOSE_RE.match(t)
    if m:
        sym = m.group("sym").upper().replace("1!", "")
        rows = [r for r in db.all("SELECT id, symbol FROM trades WHERE closed_at IS NULL AND exit IS NULL")
                if r["symbol"].upper().replace("=F", "").replace("1!", "") == sym.replace("=F", "")]
        if len(rows) == 1:
            return "close_trade", {"trade_id": rows[0]["id"], "exit": _num(m.group("n"))}
    return None


# ------------------------------------------------------------------ saying lookups out loud
def _money(v: Any) -> str:
    from .confirm import _money as money

    return money(v)


def _clock(iso: str) -> str:
    if len(iso) <= 10:
        return ""
    from .timers import _clock_words

    return _clock_words(datetime.fromisoformat(iso))


def _goals(r: list, a: dict) -> str:
    if not r:
        return "You don't have any goals yet. Say “add a daily goal: …” to make one."
    parts = []
    for g in r:
        bits = [g["cadence"]]
        if g.get("done_today"):
            bits.append("done today")
        if g.get("streak"):
            bits.append(f"{g['streak']} in a row")
        parts.append(f"{g['title']} ({', '.join(bits)})")
    return f"Your goals: {'; '.join(parts)}."


def _schedule(r: list, a: dict) -> str:
    day = a.get("day", "today")
    if not r:
        return f"Nothing booked {day}."
    parts = [f"{s.get('artist_name') or s.get('client_name')} at {_clock(s['starts_at'])}"
             + (f" ({s['hours']:g} hours)" if s.get("hours") else "") for s in r]
    return f"{day.capitalize()}: " + "; ".join(parts) + "."


def _unpaid(r: list, a: dict) -> str:
    if not r:
        return "Nobody owes you right now — everything's paid."
    owed: dict[str, float] = {}
    for s in r:
        owed[s["client_name"]] = owed.get(s["client_name"], 0) + (s["total"] or 0) - (s["paid"] or 0)
    parts = [f"{name} {_money(v)}" for name, v in sorted(owed.items(), key=lambda x: -x[1])]
    return f"{len(owed)} owe{'s' if len(owed) == 1 else ''} you {_money(sum(owed.values()))}: " + ", ".join(parts) + "."


def _inventory(r: list, a: dict) -> str:
    if not r:
        return "Your inventory's empty."
    parts = [f"{i['name']} (in {_money(i['cost'])}" + (f", listed {_money(i['list_price'])}" if i.get("list_price") else "")
             + ")" for i in r[:10]]
    more = f" and {len(r) - 10} more" if len(r) > 10 else ""
    return f"{len(r)} item{'s' if len(r) != 1 else ''}, {_money(sum(i['cost'] or 0 for i in r))} tied up: " \
        + "; ".join(parts) + more + "."


def _positions(r: list, a: dict) -> str:
    if not r:
        return "No open positions."
    parts = [f"{p['side']} {p['symbol'].replace('=F', '')} from {p['entry']:g}"
             + (f", stop {p['stop']:g}" if p.get("stop") else "") for p in r]
    return "Open: " + "; ".join(parts) + "."


def _rules(r: list, a: dict) -> str:
    if not r:
        return "You haven't set any trading rules yet. Say “add a rule: …”."
    return "Your rules: " + "; ".join(x["rule"] for x in r) + "."


def _cravvr(r: list, a: dict) -> str:
    if not r:
        return "Nothing open for Cravvr."
    return f"{len(r)} open for Cravvr: " + "; ".join(t["title"] for t in r[:8]) + "."


def _revenue(r: dict, a: dict) -> str:
    period = {"today": "today", "week": "this week", "month": "this month", "year": "this year"}.get(
        r.get("period", "month"), r.get("period", ""))
    parts = [f"{k} {_money(v)}" for k, v in sorted(r.get("by_business", {}).items(), key=lambda x: -float(x[1] or 0))
             if v]
    return f"You've made {_money(r.get('total', 0))} {period}" + (f": {', '.join(parts)}." if parts else ".")


SAY: dict[str, Callable[[Any, dict], str]] = {
    "revenue": _revenue,
    "list_goals": _goals, "studio_schedule": _schedule, "unpaid_sessions": _unpaid,
    "resale_inventory": _inventory, "open_positions": _positions, "trading_rules": _rules,
    "cravvr_tasks": _cravvr,
}
