"""Instant confirmations for actions that only save data.

After a write tool succeeds, the model would normally need a second full pass just to
say "Booked Jay for Friday at 7". On a CPU that doubles the wait, and AMO already knows
exactly what happened — so it writes the confirmation itself.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any, Callable


# Dry one-liners for the "computer" personality's instant confirmations.
COMPUTER_QUIPS = (
    "Try to contain your excitement.",
    "Filed with all the enthusiasm it deserves.",
    "Another triumph of data entry.",
    "Noted. Obviously.",
    "I live to serve. Apparently.",
    "Do try to keep up.",
    "Riveting stuff.",
)


def _money(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"${f:,.0f}" if f == int(f) else f"${f:,.2f}"


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    day = f"{dt:%a %b} {dt.day}"
    if len(iso) <= 10:
        return day
    hour = dt.hour % 12 or 12
    return f"{day} at {hour}{f':{dt.minute:02d}' if dt.minute else ''} {'AM' if dt.hour < 12 else 'PM'}"


def _who(r: dict) -> str:
    name = r.get("client_name") or r.get("name") or "client"
    artist = r.get("artist_name")
    return f"{artist} ({name})" if artist and artist != name else name


def _session(r: dict, a: dict) -> str:
    line = f"Booked {_who(r)} for {_when(r.get('starts_at'))} — {r.get('hours', 0):g}h {r.get('service', 'session')}"
    if r.get("rate"):
        line += f" at {_money(r['rate'])}/h ({_money(r.get('total'))})"
    return line + "."


def _update_session(r: dict, a: dict) -> str:
    status = r.get("status", "updated")
    if status == "cancelled":
        return f"Cancelled {_who(r)}'s session on {_when(r.get('starts_at'))}."
    if status == "no_show":
        return f"Marked {_who(r)} as a no-show for {_when(r.get('starts_at'))}."
    return f"Updated {_who(r)}'s session: {_when(r.get('starts_at'))}, {r.get('hours', 0):g}h, {status}."


def _complete(r: dict, a: dict) -> str:
    line = f"Marked {_who(r)}'s session complete"
    if a.get("paid"):
        line += f" and logged {_money(a['paid'])}" + (f" via {a['method']}" if a.get("method") else "")
    return line + "."


def _payment(r: dict, a: dict) -> str:
    who = f" from {a['client']}" if a.get("client") else ""
    via = f" via {r['method']}" if r.get("method") else ""
    biz = f" ({r['business']})" if r.get("business") and r["business"] != "studio" else ""
    return f"Logged {_money(r.get('amount'))}{who}{via}{biz}."


def _item(r: dict, a: dict) -> str:
    line = f"Added {r.get('name')} to inventory (cost {_money(r.get('cost'))})"
    if r.get("list_price"):
        line += f", listed at {_money(r['list_price'])}"
    return line + "."


def _sold(r: dict, a: dict) -> str:
    profit = r.get("profit")
    tail = f" — profit {_money(profit)}" if profit is not None else ""
    on = f" on {r['platform']}" if r.get("platform") else ""
    return f"Sold {r.get('name')} for {_money(r.get('sold_price'))}{on}{tail}."


def _trade(r: dict, a: dict) -> str:
    coach = f" {r['coach']}" if r.get("coach") else ""
    if r.get("exit") is not None:
        return _closed(r, a) + coach
    stop = f", stop {r['stop']:g}" if r.get("stop") is not None else ""
    return f"Logged {r.get('side')} {r.get('symbol')} at {r.get('entry'):g}{stop} (trade #{r.get('id')}).{coach}"


def _closed(r: dict, a: dict) -> str:
    rm = f", {r['r_multiple']:+g}R" if r.get("r_multiple") is not None else ""
    return f"Closed {r.get('side')} {r.get('symbol')} at {r.get('exit'):g}: P&L {_money(r.get('pnl'))}{rm}."


def _goal_checkin(r: dict, a: dict) -> str:
    streak = r.get("streak") or 0
    return f"Checked in on “{r.get('title')}”" + (f" — {streak}-day streak." if streak > 1 else ".")


CONFIRM: dict[str, Callable[[dict, dict], str]] = {
    "remember": lambda r, a: "Got it — I'll remember that.",
    "forget": lambda r, a: "Done, I've forgotten that.",
    "add_client": lambda r, a: f"Added {_who(r)} to your clients.",
    "update_client": lambda r, a: f"Updated {_who(r)}.",
    "book_session": _session,
    "update_session": _update_session,
    "complete_session": _complete,
    "record_payment": _payment,
    "add_resale_item": _item,
    "list_resale_item": lambda r, a: f"Listed {r.get('name')} at {_money(r.get('list_price'))}"
                                     + (f" on {r['platform']}" if r.get("platform") else "") + ".",
    "mark_item_sold": _sold,
    "log_trade": _trade,
    "close_trade": _closed,
    "add_trading_rule": lambda r, a: f"New trading rule saved: {r.get('rule')}",
    "add_goal": lambda r, a: f"New {r.get('cadence', '')} goal: {r.get('title')}.".replace("  ", " "),
    "check_in_goal": _goal_checkin,
    "add_cravvr_task": lambda r, a: f"Added Cravvr task: {r.get('title')}.",
    "update_cravvr_task": lambda r, a: f"Cravvr task “{r.get('title')}” is now {r.get('status')}.",
    "send_notification": lambda r, a: "Sent.",
    "watchlist_add": lambda r, a: (
        (f"Watching {', '.join(r['added'])}. " if r["added"] else "")
        + (f"Already watching {', '.join(r['already'])}. " if r["already"] else "")
        + (f"Couldn't find {', '.join(r['not_found'])}. " if r["not_found"] else "")
        + "I'll speak up when a setup triggers with continuity.").strip(),
    "watchlist_remove": lambda r, a: (f"Stopped watching {', '.join(r['removed'])}." if r["removed"]
                                      else "That wasn't on your watchlist."),
    "text_client": lambda r, a: (f"Here's the text to {r['client']} ({r['phone']}): “{r['message']}” "
                                 "Say “send it” to send, or “cancel”."),
    "send_text": lambda r, a: f"Sent to {r['client']}.",
    "cancel_text": lambda r, a: "Okay, I won't send it." if r["cancelled"] else "There was no text waiting.",
    "delete_record": lambda r, a: f"Deleted {r['label']} “{r['name']}”.",
    "delete_all_records": lambda r, a: (f"Deleted {r['deleted']} {r['what']}: " + ", ".join(r["names"][:8]) + "."
                                        if r["deleted"] else f"You don't have any {r['what']} to delete."),
    "update_record": lambda r, a: f"Updated {r['label']} “{r['name']}”: "
                                  + ", ".join(f"{k.replace('_', ' ')} → {v}" for k, v in r["changes"].items()) + ".",
}


def instant_reply(calls: list[dict[str, Any]]) -> str | None:
    """Confirmation text if every call in this round was a successful save, else None."""
    if not calls:
        return None
    lines = []
    for c in calls:
        fn = CONFIRM.get(c["name"])
        result = c["result"]
        if fn is None or not isinstance(result, dict) or "error" in result:
            return None
        try:
            lines.append(fn(result, c["arguments"] if isinstance(c["arguments"], dict) else {}))
        except (KeyError, TypeError, ValueError):
            return None
    reply = " ".join(lines)
    from .voice.tts import active_personality

    if active_personality() == "computer":
        reply += " " + random.choice(COMPUTER_QUIPS)
    return reply
