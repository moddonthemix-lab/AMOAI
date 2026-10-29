"""The trading workflow, Amo's part (the hub's docs/TRADING_WORKFLOW.md, sections 2 and 3).

    19:00 "Top setups for tomorrow"  → a `trade_setup` form (Trader reads it as exact data)
    16:15 "Grade today's setups"      → a `setup_scorecard` form

How setups are found doesn't change: AMO's Strat engine (strat.py) reads the charts. The shared brain's
fill_form() writes the form from that analysis; then every number is checked against what the engine
computed, because Trader acts on them. Outcomes for grading are worked out from the hourly bars, never
guessed. If the AI can't fill the form, AMO builds it from the engine alone, so Trader always gets one.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any

from .db import Database, now_iso

log = logging.getLogger(__name__)

SETUP_FORM, SCORE_FORM = "trade_setup", "setup_scorecard"
TOP_RE = re.compile(r"\btop setups?\b", re.I)
GRADE_RE = re.compile(r"\bgrade\b.*\bsetups?\b", re.I)
TF_FORM = {"M": "monthly", "W": "weekly", "D": "daily", "4H": "4h", "60": "1h"}
MAX_SETUPS = 5
MAX_SYMBOLS = 10


def wanted_form(task: dict[str, Any]) -> str | None:
    """The form this task should be answered with (asked for by the routine, or recognised by its title)."""
    if task.get("expects_form"):
        return task["expects_form"]
    title = task.get("title") or ""
    if TOP_RE.search(title):
        return SETUP_FORM
    if GRADE_RE.search(title):
        return SCORE_FORM
    return None


def plain(symbol: str) -> str:
    """The plain ticker the form wants: NQ=F → NQ, BTC-USD → BTC, PEPE24478-USD → PEPE."""
    if symbol.endswith("-USD"):
        return re.sub(r"\d+$", "", symbol[:-4])
    return symbol.removesuffix("=F").lstrip("^")


def is_inside(pattern: str) -> bool:
    return "-1-" in pattern or "inside" in pattern.lower()  # the trigger bar is an inside bar (2-1-2, 3-1-2…)


# ------------------------------------------------------------------ 19:00: the setups
def _next_session(sym: str, now: datetime | None = None) -> bool:
    """Stocks after the close: today's candle is finished, so tomorrow's setups are built on it."""
    from .proactive import market_open
    from .db import local_now

    return not (sym.endswith("-USD") or sym.endswith("=F")) and not market_open(now or local_now())


def candidates(symbols: list[str], now: datetime | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Every setup AMO's Strat engine sees for the next session, with exact numbers. (candidates, problems)"""
    from . import strat
    from .market import Bar, candles

    out: list[dict[str, Any]] = []
    problems: list[str] = []
    for sym in symbols[:MAX_SYMBOLS]:
        try:
            a = strat.analyze(sym)
        except Exception as e:  # noqa: BLE001 — one bad ticker never sinks the plan
            problems.append(f"{sym}: {e}")
            continue
        sym = a["symbol"]
        want = "long" if "bullish" in a["bias"] else "short" if "bearish" in a["bias"] else None
        tfs = a["timeframes"]
        continuity = ", ".join(f"{tf} {'inside' if tfs[tf]['type'] == '1' else 'up' if tfs[tf]['up'] else 'down'}"
                               for tf in ("M", "W", "D") if tf in tfs)
        setups = [s for s in a["_setups"] if s.status == "waiting"]
        if _next_session(sym, now):
            setups = [s for s in setups if s.tf in ("M", "W")]  # still-running candles: as they are
            for tf in ("D", "4H"):
                try:
                    bars, _ = candles(sym, tf)
                except Exception:  # noqa: BLE001
                    continue
                if len(bars) < 4:
                    continue
                last = bars[-1]
                ahead = bars + [Bar(last.t + 1, last.c, last.c, last.c, last.c)]  # tomorrow, not yet traded
                for s in strat.find_setups(tf, ahead, strat.label(ahead)):
                    if s.status == "waiting":
                        setups.append(s)
        for s in setups:
            s.with_continuity = None if want is None else s.direction == want
            s.trigger, s.stop = _price(s.trigger), _price(s.stop)
            s.target = None if s.target is None else _price(s.target)
            risk = abs(s.trigger - s.stop)
            if risk <= 0:
                continue
            target, two_r = s.target, False
            if target is None:  # price discovery: no prior level to aim at
                target, two_r = _price(s.trigger + (2 * risk if s.direction == "long" else -2 * risk)), True
            rr = round(abs(target - s.trigger) / risk, 2)
            conf = ("high" if s.with_continuity and a["continuity"]["ftfc"] and rr >= 1.5 else
                    "medium" if s.with_continuity else "low")
            out.append({"id": len(out) + 1, "symbol": plain(sym), "market_symbol": sym,
                        "timeframe": TF_FORM.get(s.tf, "daily"), "pattern": s.name + (
                            " (inside bar)" if is_inside(s.name) and "inside" not in s.name else ""),
                        "direction": s.direction, "trigger": s.trigger, "stop": s.stop, "targets": [target],
                        "two_r_target": two_r, "rr": rr, "with_continuity": s.with_continuity,
                        "continuity": continuity, "bias": a["bias"], "confidence": conf})
    return out, problems


_SUFFIX = re.compile(r" \(.*$| \+.*$")  # " (inside bar)", " + hammer"


def _price(v: float) -> float:
    """Prices as a chart shows them: cents above $1, more digits for tiny coins."""
    return round(v, 2) if abs(v) >= 1 else float(f"{v:.6g}")


def _stats_key(timeframe: str, pattern: str) -> str:
    kind = "inside bar" if is_inside(pattern) else _SUFFIX.sub("", pattern)
    return f"{timeframe}|{kind}"


def hit_rates(db: Database) -> dict[str, dict[str, Any]]:
    """Running hit rate per pattern and timeframe (hit target vs hit stop)."""
    out: dict[str, dict[str, Any]] = {}
    for r in db.all("SELECT timeframe, pattern, outcome FROM setup_grades WHERE outcome IN ('hit target','hit stop')"):
        k = _stats_key(r["timeframe"] or "", r["pattern"] or "")
        s = out.setdefault(k, {"won": 0, "done": 0})
        s["done"] += 1
        s["won"] += r["outcome"] == "hit target"
    for s in out.values():
        s["rate"] = round(s["won"] / s["done"], 2)
    return out


def rank(cands: list[dict[str, Any]], rates: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Inside bars first (the boss's rule), then what's been working, then continuity, confidence and R:R."""
    conf = {"high": 2, "medium": 1, "low": 0}

    def key(c: dict[str, Any]) -> tuple:
        st = rates.get(_stats_key(c["timeframe"], c["pattern"]))
        rate = st["rate"] if st and st["done"] >= 3 else 0.5
        return (is_inside(c["pattern"]), bool(c["with_continuity"]), rate, conf[c["confidence"]], c["rr"])

    return sorted(cands, key=key, reverse=True)


def _engine_why(c: dict[str, Any]) -> str:
    side = "with" if c["with_continuity"] else "against" if c["with_continuity"] is False else "without clear"
    tail = ", target is 2R (no prior level)" if c["two_r_target"] else ""
    return f"{c['timeframe']} {c['pattern']} {c['direction']}, {side} timeframe continuity, R:R {c['rr']:g}{tail}"


def _item(c: dict[str, Any], why: str | None = None, invalidation: str | None = None) -> dict[str, Any]:
    stop_word = "close below" if c["direction"] == "long" else "close above"
    why = (why or "").strip() or _engine_why(c)
    if c["two_r_target"] and "2R" not in why:
        why += " (target is 2R: no prior level)"
    return {"symbol": c["symbol"], "timeframe": c["timeframe"], "pattern": c["pattern"], "direction": c["direction"],
            "trigger": c["trigger"], "stop": c["stop"], "targets": c["targets"],
            "invalidation": (invalidation or "").strip() or f"{c['timeframe']} {stop_word} {c['stop']} before it triggers",
            "continuity": c["continuity"], "confidence": c["confidence"], "why": why[:300]}


def check_setups(filled: dict[str, Any] | None, cands: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    """The AI's items, matched to the engine's setups. Numbers always come from the engine; items that match
    no real setup are dropped. Returns (items, corrected, dropped)."""
    items, used, corrected, dropped = [], set(), 0, 0
    for it in (filled or {}).get("items") or []:
        if not isinstance(it, dict):
            dropped += 1
            continue
        sym = plain(str(it.get("symbol") or "").upper().lstrip("$"))
        same = [c for c in cands if c["symbol"] == sym and c["direction"] == str(it.get("direction", "")).lower()
                and c["id"] not in used]
        tf = [c for c in same if c["timeframe"] == str(it.get("timeframe", "")).lower()] or same

        def near(c: dict[str, Any]) -> float:
            try:
                return abs(float(it.get("trigger")) - c["trigger"])
            except (TypeError, ValueError):
                return float("inf")

        if not tf:
            dropped += 1
            continue
        c = min(tf, key=near)
        used.add(c["id"])
        try:
            exact = (abs(float(it.get("trigger")) - c["trigger"]) < 1e-6 and abs(float(it.get("stop")) - c["stop"]) < 1e-6
                     and [float(x) for x in it.get("targets") or []][:1] == c["targets"])
        except (TypeError, ValueError):
            exact = False
        corrected += not exact
        items.append(_item(c, it.get("why"), it.get("invalidation")))
    order = {id(x): n for n, x in enumerate(items)}  # keep the AI's order, but inside bars stay first
    items.sort(key=lambda x: (not is_inside(x["pattern"]), order[id(x)]))
    return items[:MAX_SETUPS], corrected, dropped


def setups_prompt(analysis: str, cands: list[dict[str, Any]], rates: dict[str, Any]) -> str:
    rows = [{k: c[k] for k in ("id", "symbol", "timeframe", "pattern", "direction", "trigger", "stop", "targets",
                               "rr", "with_continuity", "continuity", "confidence")} for c in cands[:20]]
    return ("Fill the trade_setup form for the next session from AMO's Strat analysis below.\n"
            f"Pick at most {MAX_SETUPS} setups, best first. Rank setups built on inside bars (1) highest, then those "
            "with timeframe continuity. Use ONLY setups from the list, and copy symbol, timeframe, pattern, direction, "
            "trigger, stop, targets, continuity and confidence exactly as given. Write `why` (one plain sentence) and "
            "`invalidation` yourself. If nothing is worth trading, return no items and say why in the note.\n\n"
            f"Setups the engine computed (exact numbers):\n{json.dumps(rows, indent=1)}\n\n"
            f"Hit rates so far by timeframe|pattern (favor what works): {json.dumps(rates) if rates else 'none yet'}\n\n"
            f"AMO's analysis:\n{analysis[:4000]}")


def setups_note(cands: list[dict[str, Any]], problems: list[str], ai_note: str = "") -> str:
    parts = [ai_note.strip()] if ai_note.strip() else []
    if any(c["market_symbol"].endswith("-USD") and c["timeframe"] == "daily" for c in cands):
        from .market import crypto_day_open_text

        parts.append(f"Crypto daily candles roll over at {crypto_day_open_text()}; re-check crypto levels after.")
    if problems:
        parts.append("Couldn't read: " + "; ".join(problems)[:300])
    return " ".join(parts)


def symbols_from(out: dict[str, Any]) -> list[str]:
    """The tickers AMO's own analysis looked at (from its tool calls)."""
    seen: list[str] = []
    for c in out.get("tool_calls") or []:
        r = c.get("result") if isinstance(c.get("result"), dict) else {}
        for s in ([r.get("symbol")] if r.get("symbol") else []) + list(r.get("symbols") or []):
            if isinstance(s, str) and s not in seen:
                seen.append(s)
    return seen


def save_plan(db: Database, task_id: Any, form: dict[str, Any], cands: list[dict[str, Any]]) -> None:
    syms = {c["symbol"]: c["market_symbol"] for c in cands}
    db.insert("trade_plans", {"task_id": task_id, "posted_at": now_iso(), "form": json.dumps(form),
                              "symbols": json.dumps(syms)})


# ------------------------------------------------------------------ 16:15: the scorecard
def outcome(setup: dict[str, Any], bars: list[Any], since: float) -> tuple[str, float | None, str]:
    """What the market did with one setup since the plan was posted: (outcome, R, comment).
    If target and stop fall in the same hour, it counts as the stop (to be safe)."""
    long = setup["direction"] == "long"
    trig, stop, target = setup["trigger"], setup["stop"], (setup.get("targets") or [None])[0]
    risk = abs(trig - stop) or None
    triggered_at = None
    for b in bars:
        if b.t < since:
            continue
        if triggered_at is None:
            if (b.h >= trig) if long else (b.l <= trig):
                triggered_at = b.t
            else:
                continue
        if (b.l <= stop) if long else (b.h >= stop):
            return "hit stop", -1.0, f"triggered {_when(triggered_at)}, stopped {_when(b.t)}"
        if target is not None and ((b.h >= target) if long else (b.l <= target)):
            r = round(abs(target - trig) / risk, 2) if risk else None
            return "hit target", r, f"triggered {_when(triggered_at)}, target {_when(b.t)}"
    if triggered_at is None:
        return "not triggered", None, "price never reached the trigger"
    last = bars[-1].c if bars else trig
    r = round(((last - trig) if long else (trig - last)) / risk, 2) if risk else None
    return "still open", r, f"triggered {_when(triggered_at)}, open at {last:g}"


def _when(t: float | None) -> str:
    if t is None:
        return "—"
    from zoneinfo import ZoneInfo

    from .config import settings

    return datetime.fromtimestamp(t, ZoneInfo(settings.timezone)).strftime("%a %-I:%M %p")


def last_plan(db: Database) -> dict[str, Any] | None:
    return db.one("SELECT * FROM trade_plans WHERE graded_at IS NULL ORDER BY id DESC LIMIT 1")


def grade_rows(plan: dict[str, Any]) -> list[dict[str, Any]]:
    from .market import candles

    form = json.loads(plan["form"])
    syms = json.loads(plan["symbols"])
    since = datetime.fromisoformat(plan["posted_at"]).timestamp()
    rows = []
    for it in form.get("items") or []:
        try:
            bars, _ = candles(syms.get(it["symbol"], it["symbol"]), "60")
            out, r, comment = outcome(it, bars, since)
        except Exception as e:  # noqa: BLE001
            out, r, comment = "still open", None, f"couldn't read prices ({type(e).__name__})"
        rows.append({"symbol": it["symbol"], "timeframe": it.get("timeframe"), "pattern": it["pattern"],
                     "direction": it["direction"], "outcome": out, "r_multiple": r, "comment": comment})
    return rows


def score_line(rows: list[dict[str, Any]], db: Database) -> str:
    done = [r for r in rows if r["outcome"] in ("hit target", "hit stop")]
    won = sum(r["outcome"] == "hit target" for r in done)
    today = (f"{won} of {len(done)} finished setups worked" if done else
             f"none of {len(rows)} setups finished yet" if rows else "no setups to grade")
    allr = db.all("SELECT outcome, graded_at FROM setup_grades WHERE outcome IN ('hit target','hit stop')")
    tw = sum(r["outcome"] == "hit target" for r in allr) + won
    tt = len(allr) + len(done)
    days = len({r["graded_at"][:10] for r in allr} | ({now_iso()[:10]} if rows else set()))
    running = (f"Running ({days} day{'s' if days != 1 else ''}): {tw} of {tt} = {round(100 * tw / tt)}%" if tt
               else "Running: no finished setups yet")
    return f"{today[:1].upper() + today[1:]}. {running}."


def check_scorecard(filled: dict[str, Any] | None, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One item per setup, outcomes and R exactly as computed; the AI only adds the comment."""
    comments: dict[tuple, str] = {}
    for it in (filled or {}).get("items") or []:
        if isinstance(it, dict) and it.get("comment"):
            comments[(plain(str(it.get("symbol", "")).upper()), str(it.get("direction", "")).lower())] = str(it["comment"])
    return [{"symbol": r["symbol"], "pattern": r["pattern"], "direction": r["direction"], "outcome": r["outcome"],
             "r_multiple": r["r_multiple"],
             "comment": (comments.get((r["symbol"], r["direction"])) or r["comment"])[:300]} for r in rows]


def save_grades(db: Database, plan: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    ts = now_iso()
    for r in rows:
        db.insert("setup_grades", {"plan_id": plan["id"], "symbol": r["symbol"], "timeframe": r.get("timeframe"),
                                   "pattern": r["pattern"], "direction": r["direction"], "outcome": r["outcome"],
                                   "r_multiple": r["r_multiple"], "graded_at": ts})
    db.execute("UPDATE trade_plans SET graded_at = ? WHERE id = ?", (ts, plan["id"]))


def scorecard_prompt(rows: list[dict[str, Any]], line: str) -> str:
    return ("Fill the setup_scorecard form: one item per setup below, copying symbol, pattern, direction, outcome "
            "and r_multiple exactly. Write a short `comment` for each (keep the times and prices). In the note, write "
            "ONLY one lesson for tomorrow, one sentence.\n\n"
            f"Results computed from the price data:\n{json.dumps(rows, indent=1)}\n\nScore: {line}")

