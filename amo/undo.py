"""Undo: take back what AMO just saved — for when it misheard you.

After every turn the new things it created (a goal, a booking, a payment, a trade, a memory…)
are remembered in the kv table, so "undo that", "no, I said Thursday" or cutting AMO off with
"you misheard me" can remove them again — from any window or device.
Changes to existing items (rescheduling, marking sold) are not undone automatically.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from .db import Database, get_db, local_now

KEY = "last_turn"

# tool that created something → the kind of record it made
CREATES = {
    "add_goal": "goals", "add_client": "clients", "book_session": "sessions", "record_payment": "payments",
    "add_resale_item": "resale", "log_trade": "trades", "add_cravvr_task": "cravvr",
    "add_trading_rule": "rules", "remember": "memories",
}
CHANGES = {"update_client", "update_session", "complete_session", "list_resale_item", "mark_item_sold",
           "close_trade", "check_in_goal", "update_cravvr_task", "update_record", "delete_record",
           "delete_all_records", "forget", "watchlist_remove"}

UNDO_RE = re.compile(r"^(?:(?:hey )?amo[ ,]*)?(?:please )?(?:undo(?: that| it| the last (?:one|thing))?|"
                     r"take (?:that|it) back|scratch that|revert that)[ .!]*(?:please)?[ .!]*$", re.I)


def record_turn(trace: list[dict[str, Any]], db: Database | None = None) -> None:
    """Remember what this turn created. Turns that saved nothing leave the last record alone."""
    made, changed = [], []
    for t in trace:
        r = t.get("result")
        if not isinstance(r, dict) or "error" in r:
            continue
        if t["name"] in CREATES and r.get("id") and not r.get("duplicate"):
            made.append({"kind": CREATES[t["name"]], "id": r["id"]})
        elif t["name"] == "watchlist_add" and r.get("added"):
            made.append({"watch": r["added"]})
        elif t["name"] == "text_client" and r.get("drafted"):
            made.append({"text": r.get("client")})
        elif t["name"] in CHANGES:
            changed.append(t["name"])
    if made or changed:
        (db or get_db()).set_kv(KEY, json.dumps({"made": made, "changed": changed,
                                                 "at": local_now().isoformat()}))


def undo_recent(db: Database | None = None, within_minutes: float = 3) -> str | None:
    """Undo the last turn's saves if they happened in the last few minutes.
    Returns what was undone (to say out loud), or None if there was nothing."""
    db = db or get_db()
    raw = db.get_kv(KEY)
    if not raw:
        return None
    last = json.loads(raw)
    if datetime.fromisoformat(last["at"]) < local_now() - timedelta(minutes=within_minutes):
        return None
    from . import records
    from .integrations import cancel_text
    from .proactive import remove_from_watchlist

    done = []
    for m in last.get("made", []):
        try:
            if "watch" in m:
                remove_from_watchlist(m["watch"], db)
                done.append(f"took {', '.join(m['watch'])} off the watchlist")
            elif "text" in m:
                if cancel_text(db)["cancelled"]:
                    done.append(f"cancelled the text to {m['text']}")
            else:
                gone = records.delete(m["kind"], m["id"], db)
                done.append(f"removed the {gone['label']} {gone['name']}")
        except ValueError:
            continue  # already gone (deleted by hand)
    db.execute("DELETE FROM kv WHERE key = ?", (KEY,))
    if done:
        return "Undid that: " + "; ".join(done) + "."
    if last.get("changed"):
        return "I can only undo new things I added — that was a change, so fix it by telling me what it should be."
    return None


def undo_last(db: Database | None = None) -> str:
    """'Undo that' typed or said — a longer window than a correction."""
    return undo_recent(db, within_minutes=30) or "There's nothing recent to undo."
