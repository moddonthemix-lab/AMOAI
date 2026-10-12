"""Strat level alerts: AMO watches up to 3 tickers and tells you the moment one breaks the previous
candle's high or low (the Strat trigger — a 2U or 2D), so you don't have to stare at charts.

    "put an alert on PLTR for the daily"          "watch PLTR for a break"
    "alert me when NQ breaks the 4 hour high or low"
    "what alerts do I have?"   "remove the PLTR alert"   "clear my alerts"

Each candle can fire once per side: a break of the high, a break of the low, and if it does both
you hear that it's become an outside bar (a 3). The next candle re-arms it. Alerts stay on until you
remove them — same list on the dashboard, by voice or by text.

Checked every minute: crypto 24/7, futures nearly around the clock, stocks during market hours.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .db import Database, get_db, local_now, now_iso

MAX_ALERTS = 3
TFS = {"60": "hourly", "4H": "4-hour", "D": "daily", "W": "weekly", "M": "monthly"}
_PREV = {"60": "the last hourly candle's", "4H": "the last 4-hour candle's", "D": "yesterday's",
         "W": "last week's", "M": "last month's"}
_SPAN = {"60": 3600, "4H": 14400, "W": 7 * 86400, "M": 31 * 86400}


def tf_of(word: str | None) -> str:
    w = (word or "daily").lower().replace("-", " ").strip()
    if re.fullmatch(r"(?:4|four) ?h(?:ou)?r?s?|h4|4 hour", w):
        return "4H"
    return {"daily": "D", "day": "D", "d": "D", "hourly": "60", "hour": "60", "1 hour": "60", "60": "60",
            "60 minute": "60", "weekly": "W", "week": "W", "w": "W", "monthly": "M", "month": "M",
            "m": "M"}.get(w, "D")


# ------------------------------------------------------------------ the list
def active(db: Database | None = None) -> list[dict[str, Any]]:
    return (db or get_db()).all("SELECT * FROM strat_alerts WHERE active = 1 ORDER BY id")


def add(symbol: str, tf: str = "D", db: Database | None = None, now: datetime | None = None) -> dict[str, Any]:
    from .market import lookup

    db = db or get_db()
    tf = tf if tf in TFS else tf_of(tf)
    sym = lookup(symbol)
    have = active(db)
    same = [a for a in have if a["symbol"] == sym and a["tf"] == tf]
    if same:
        return {**_view(same[0]), "duplicate": True,
                "text": f"You already have a {TFS[tf]} alert on {_name(sym)}."}
    if len(have) >= MAX_ALERTS:
        names = ", ".join(f"{_name(a['symbol'])} {TFS[a['tf']]}" for a in have)
        raise ValueError(f"you're at the max of {MAX_ALERTS} alerts ({names}) — remove one first")
    aid = db.insert("strat_alerts", {"symbol": sym, "tf": tf, "active": 1, "created_at": now_iso()})
    row = db.one("SELECT * FROM strat_alerts WHERE id = ?", (aid,))
    # Read the levels now; a side that's already broken this candle won't ring for it.
    note = ""
    try:
        state = _levels(sym, tf, now)
        if state:
            _save_state(db, row, state, mark_broken=True)
            row = db.one("SELECT * FROM strat_alerts WHERE id = ?", (aid,))
            note = _now_note(row, state)
    except Exception:  # noqa: BLE001 — no data right now: it'll read the levels on the next check
        pass
    return {**_view(row), "text": f"Watching {_name(sym)} on the {TFS[tf]}" + note
            + f" ({len(have) + 1} of {MAX_ALERTS} alerts)."}


def remove(target: str = "", everything: bool = False, db: Database | None = None) -> dict[str, Any]:
    from .market import normalize

    db = db or get_db()
    have = active(db)
    if not have:
        return {"removed": 0, "text": "You don't have any alerts."}
    if everything:
        hits = have
    else:
        t = target.strip()
        if t.isdigit():
            hits = [a for a in have if a["id"] == int(t)]
        else:
            words = t.split()
            sym = normalize(words[0]) if words else ""
            tf = tf_of(" ".join(words[1:])) if len(words) > 1 else None
            hits = [a for a in have if a["symbol"] in (sym, normalize(t)) and (tf is None or a["tf"] == tf)]
        if not hits:
            return {"removed": 0, "text": f"I don't have an alert on {target}. " + list_text(db)}
    for a in hits:
        db.execute("UPDATE strat_alerts SET active = 0 WHERE id = ?", (a["id"],))
    what = ", ".join(f"{_name(a['symbol'])} {TFS[a['tf']]}" for a in hits)
    return {"removed": len(hits), "text": f"Removed the {what} alert{'s' if len(hits) != 1 else ''}."}


def list_text(db: Database | None = None) -> str:
    have = active(db)
    if not have:
        return f"No alerts set. Say “put an alert on PLTR for the daily” (up to {MAX_ALERTS})."
    parts = []
    for a in have:
        lv = (f": above {_fmt(a['high'])} or below {_fmt(a['low'])}" if a["high"] is not None else "")
        hit = [s for s, f in (("high broken", a["hit_high"]), ("low broken", a["hit_low"])) if f]
        parts.append(f"{_name(a['symbol'])} {TFS[a['tf']]}{lv}" + (f" ({', '.join(hit)})" if hit else ""))
    return f"Watching {len(have)} of {MAX_ALERTS}: " + "; ".join(parts) + "."


# ------------------------------------------------------------------ checking
def _levels(sym: str, tf: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Previous candle's high/low and the live candle. Between candles (market closed, before the open) the
    last candle IS the previous one for what's next: its high/low are returned with live=False."""
    from zoneinfo import ZoneInfo

    from .market import candles

    bars, meta = candles(sym, tf)
    if len(bars) < 2:
        return None
    prev, cur = bars[-2], bars[-1]
    if tf == "D":  # before the open Yahoo's last daily bar is yesterday's — don't treat it as today
        try:
            tz = ZoneInfo(meta.get("exchangeTimezoneName") or "America/New_York")
        except Exception:  # noqa: BLE001
            tz = ZoneInfo("America/New_York")
        live = datetime.fromtimestamp(cur.t, tz).date() == datetime.now(tz).date()
    else:
        live = datetime.now().timestamp() - cur.t <= _SPAN[tf]
    if not live:  # the next candle's triggers are this finished candle's high and low
        return {"bar": None, "high": cur.h, "low": cur.l, "cur_high": cur.c, "cur_low": cur.c, "price": cur.c,
                "live": False}
    return {"bar": cur.t, "high": prev.h, "low": prev.l, "cur_high": cur.h, "cur_low": cur.l, "price": cur.c,
            "live": True}


def _save_state(db: Database, a: dict[str, Any], s: dict[str, Any], mark_broken: bool = False) -> dict[str, bool]:
    """Store the levels; returns which sides just broke (newly, this candle)."""
    new_bar = a["bar"] != s["bar"] or s["bar"] is None
    hit_high = 0 if new_bar else a["hit_high"]
    hit_low = 0 if new_bar else a["hit_low"]
    broke_high = s["cur_high"] > s["high"] and not hit_high
    broke_low = s["cur_low"] < s["low"] and not hit_low
    db.update("strat_alerts", a["id"], {
        "bar": s["bar"], "high": s["high"], "low": s["low"], "price": s["price"], "checked_at": now_iso(),
        "hit_high": int(hit_high or broke_high), "hit_low": int(hit_low or broke_low)})
    if mark_broken:
        return {"high": False, "low": False}
    return {"high": broke_high, "low": broke_low}


def is_trading(symbol: str, now: datetime | None = None) -> bool:
    """Crypto always; futures Sunday 6 PM – Friday 5 PM New York (not the 5–6 PM break); stocks 9:30–4."""
    from zoneinfo import ZoneInfo

    from .proactive import market_open

    if symbol.endswith("-USD"):
        return True
    if symbol.endswith("=F"):
        ny = datetime.now(ZoneInfo("America/New_York"))
        wd, h = ny.weekday(), ny.hour
        if wd == 5 or (wd == 4 and h >= 17) or (wd == 6 and h < 18):
            return False
        return h != 17
    return market_open(now or local_now())


def check(db: Database | None = None, now: datetime | None = None, force: bool = False) -> int:
    """Scheduler job (every minute): announce new breaks. Returns how many alerts it said."""
    from .proactive import announce

    db = db or get_db()
    now = now or local_now()
    said = 0
    for a in active(db):
        if not force and not is_trading(a["symbol"], now):
            continue
        try:
            s = _levels(a["symbol"], a["tf"], now)
        except Exception:  # noqa: BLE001 — network hiccup: try again next minute
            continue
        if not s:
            continue
        if not s["live"]:
            _save_state(db, a, s, mark_broken=True)  # show the next triggers; nothing to announce
            continue
        broke = _save_state(db, a, s)
        if not (broke["high"] or broke["low"]):
            continue
        a = db.one("SELECT * FROM strat_alerts WHERE id = ?", (a["id"],))
        text = _break_text(a, s, broke)
        key = f"level:{a['id']}:{s['bar']}:{'H' if broke['high'] else ''}{'L' if broke['low'] else ''}"
        said += announce(text, "alert", key, ttl_minutes=30, db=db, now=now)
    return said


# ------------------------------------------------------------------ words
def _name(sym: str) -> str:
    return sym.replace("=F", "").replace("-USD", "").lstrip("^")


def _fmt(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}" if abs(v) < 1000 else f"{v:,.0f}" if abs(v) >= 10000 else f"{v:,.1f}"


def _now_note(a: dict[str, Any], s: dict[str, Any]) -> str:
    lv = f" — {_PREV[a['tf']]} high {_fmt(s['high'])}, low {_fmt(s['low'])}"
    if not s.get("live", True):
        lv = f" — next candle's triggers: above {_fmt(s['high'])} or below {_fmt(s['low'])}"
        return lv + " (market's closed now, I'll watch from the open)"
    if a["hit_high"] and a["hit_low"]:
        return lv + "; it's already broken both (outside bar), so I'll catch the next candle"
    if a["hit_high"]:
        return lv + "; it's already above the high (2U), so I'll call the low if it breaks"
    if a["hit_low"]:
        return lv + "; it's already below the low (2D), so I'll call the high if it breaks"
    return lv + f"; price {_fmt(s['price'])}, inside for now"


def _break_text(a: dict[str, Any], s: dict[str, Any], broke: dict[str, bool]) -> str:
    name, tf = _name(a["symbol"]), TFS[a["tf"]]
    if a["hit_high"] and a["hit_low"]:
        side = "high" if broke["high"] else "low"
        return (f"{name} just broke {_PREV[a['tf']]} {side} too — the {tf} is now an outside bar (3). "
                f"Price {_fmt(s['price'])}.")
    if broke["high"]:
        return (f"{name} just broke {_PREV[a['tf']]} high {_fmt(s['high'])} — {tf} 2U. "
                f"Price {_fmt(s['price'])}." + _continuity(a["symbol"], "long"))
    return (f"{name} just broke {_PREV[a['tf']]} low {_fmt(s['low'])} — {tf} 2D. "
            f"Price {_fmt(s['price'])}." + _continuity(a["symbol"], "short"))


def _continuity(sym: str, side: str) -> str:
    """One line on whether the break is with the bigger picture (best effort)."""
    try:
        from .strat import analyze

        bias = analyze(sym)["bias"]
    except Exception:  # noqa: BLE001
        return ""
    if ("bull" in bias and side == "long") or ("bear" in bias and side == "short"):
        return f" With continuity ({bias})."
    if "bull" in bias or "bear" in bias:
        return f" Against continuity ({bias}) — careful."
    return " No clear continuity."


def _view(a: dict[str, Any]) -> dict[str, Any]:
    return {"id": a["id"], "symbol": a["symbol"], "name": _name(a["symbol"]), "tf": a["tf"],
            "tf_name": TFS[a["tf"]], "high": a["high"], "low": a["low"], "price": a["price"],
            "hit_high": bool(a["hit_high"]), "hit_low": bool(a["hit_low"]), "checked_at": a["checked_at"]}


def views(db: Database | None = None) -> list[dict[str, Any]]:
    return [_view(a) for a in active(db)]


# ------------------------------------------------------------------ understanding requests
_TFW = r"(?P<tf>daily|day|hourly|hour|60 ?minute|4[ -]?h(?:ou)?rs?|4h|four[ -]hour|weekly|week|monthly|month)"
_SYMW = r"(?P<sym>\$?[A-Za-z][A-Za-z.\-=/]{0,9})"
ADD_RES = [
    re.compile(r"^(?:(?:hey )?amo[ ,]*)?(?:please |can you |could you )?(?:put|set|add|create|make)\s+(?:an?\s+|me\s+an?\s+)?"
               r"(?:strat\s+|price\s+|level\s+)?alert\s+(?:on|for)\s+" + _SYMW + r"(?:\s+(?:on|for)\s+(?:the\s+)?" + _TFW
               + r"(?:\s+(?:chart|candle|timeframe))?)?[.!]*$", re.I),
    re.compile(r"^(?:(?:hey )?amo[ ,]*)?(?:please |can you |could you )?(?:alert|tell|ping|notify) me (?:when|if)\s+"
               + _SYMW + r"\s+breaks?\s+(?:the\s+|its\s+)?(?:" + _TFW + r"\s+)?(?:highs?|lows?|levels?|range)"
               r"(?:\s+or\s+(?:the\s+)?(?:highs?|lows?))?(?:\s+(?:on|of)\s+the\s+" + _TFW.replace("?P<tf>", "?P<tf2>")
               + r")?[.!]*$", re.I),
    re.compile(r"^(?:(?:hey )?amo[ ,]*)?(?:please |can you |could you )?watch\s+" + _SYMW
               + r"\s+for\s+(?:a\s+)?(?:break|breakout|breakdown|trigger)s?(?:\s+(?:on|of)\s+the\s+" + _TFW
               + r")?(?:\s+for me)?[.!]*$", re.I),
]
LIST_RE = re.compile(r"^(?:what|which) (?:strat |price |level )?alerts (?:do i have|are (?:set|on))\??$|"
                     r"^(?:show|list|read)(?: me)? (?:my )?(?:strat |price |level )?alerts$|^my alerts\??$", re.I)
REMOVE_RE = re.compile(r"^(?:please )?(?:remove|delete|cancel|drop|clear)\s+(?:the\s+|my\s+)?"
                       r"(?:(?P<all>all(?: (?:of )?my)?|every)\s+)?(?P<what>[\w=.\- ]*?)\s*alerts?[.!]*$", re.I)


def match(text: str) -> tuple[str, dict[str, Any]] | None:
    t = (text or "").strip()
    if LIST_RE.match(t):
        return "list_level_alerts", {}
    m = REMOVE_RE.match(t)
    if m and (m.group("all") or m.group("what").strip().lower() in ("", "my", "the", "all")):
        return "remove_level_alert", {"all": True}
    if m:
        return "remove_level_alert", {"target": m.group("what").strip()}
    for rx in ADD_RES:
        m = rx.match(t)
        if m:
            sym = m.group("sym").lstrip("$")
            gd = m.groupdict()
            tf = tf_of(gd.get("tf") or gd.get("tf2"))
            return "add_level_alert", {"symbol": sym, "timeframe": tf}
    return None
