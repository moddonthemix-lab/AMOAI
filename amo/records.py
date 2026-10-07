"""One place to list, create, update and delete everything AMO tracks.

The dashboard's management tabs and the AI's edit/delete tools both go through here, so
whatever you can do in one you can do in the other, with the same validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .db import Database, get_db, now_iso, parse_when


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    type: str = "text"          # text | longtext | number | int | date | datetime | select | bool | client
    options: tuple[str, ...] = ()
    required: bool = False
    show: bool = True            # shown as a column in the dashboard table


@dataclass(frozen=True)
class Entity:
    kind: str
    label: str                   # singular, for messages ("goal")
    title: str                   # plural, for the dashboard tab ("Goals")
    table: str
    fields: tuple[Field, ...]
    name_fields: tuple[str, ...]  # searched when you refer to an item by name
    order: str = "id DESC"
    where: str = ""               # default filter for lists
    soft_delete: str | None = None  # column to set instead of deleting the row
    enrich: Callable[[Database, list[dict]], None] | None = field(default=None, compare=False)


# ------------------------------------------------------------------ enrichers
def _client_names(db: Database, rows: list[dict]) -> None:
    ids = {r["client_id"] for r in rows if r.get("client_id")}
    names = {}
    if ids:
        marks = ",".join("?" * len(ids))
        names = {c["id"]: c["artist_name"] or c["name"]
                 for c in db.all(f"SELECT id, name, artist_name FROM clients WHERE id IN ({marks})", tuple(ids))}
    for r in rows:
        r["client_name"] = names.get(r.get("client_id"))
        if "hours" in r and "rate" in r:
            r["total"] = round((r["hours"] or 0) * (r["rate"] or 0), 2)


def _resale(db: Database, rows: list[dict]) -> None:
    for r in rows:
        r["profit"] = (round((r["sold_price"] or 0) - (r["cost"] or 0) - (r["fees"] or 0) - (r["shipping"] or 0), 2)
                       if r["status"] == "sold" else None)


def _trades(db: Database, rows: list[dict]) -> None:
    from .trading import _pnl, _r_multiple

    for r in rows:
        r["pnl"], r["r_multiple"] = _pnl(r), _r_multiple(r)


def _goals(db: Database, rows: list[dict]) -> None:
    from .goals import Goals

    g = Goals(db)
    for r in rows:
        full = g.get(r["id"])
        r["streak"], r["done_today"] = full["streak"], full["done_today"]


AREAS = ("studio", "cravvr", "reselling", "trading", "health", "general")

ENTITIES: dict[str, Entity] = {e.kind: e for e in [
    Entity("goals", "goal", "Goals", "goals", (
        Field("title", "Goal", required=True),
        Field("cadence", "How often", "select", ("daily", "weekly", "monthly", "once")),
        Field("area", "Area", "select", AREAS),
        Field("target", "Target", "number", show=False),
        Field("due_date", "Due", "date", show=False),
    ), ("title",), order="cadence = 'daily' DESC, id", where="active = 1", enrich=_goals),
    Entity("clients", "client", "Clients", "clients", (
        Field("name", "Name", required=True),
        Field("artist_name", "Artist name"),
        Field("phone", "Phone"),
        Field("email", "Email", show=False),
        Field("instagram", "Instagram"),
        Field("status", "Status", "select", ("lead", "active", "inactive")),
        Field("notes", "Notes", "longtext", show=False),
    ), ("name", "artist_name", "instagram", "phone"), order="name COLLATE NOCASE"),
    Entity("sessions", "session", "Sessions", "studio_sessions", (
        Field("client_id", "Client", "client", required=True),
        Field("starts_at", "When", "datetime", required=True),
        Field("hours", "Hours", "number"),
        Field("service", "Service", "select", ("recording", "mixing", "mastering", "production", "other")),
        Field("rate", "Rate $/h", "number"),
        Field("status", "Status", "select", ("booked", "completed", "cancelled", "no_show")),
        Field("notes", "Notes", "longtext", show=False),
    ), (), order="starts_at DESC", enrich=_client_names),
    Entity("payments", "payment", "Payments", "payments", (
        Field("amount", "Amount", "number", required=True),
        Field("client_id", "Client", "client"),
        Field("business", "Business", "select", ("studio", "cravvr", "reselling", "trading", "other")),
        Field("method", "Method"),
        Field("paid_at", "Date", "date"),
        Field("note", "Note"),
    ), ("note", "method"), order="paid_at DESC, id DESC", enrich=_client_names),
    Entity("resale", "item", "Inventory", "resale_items", (
        Field("name", "Item", required=True),
        Field("cost", "Cost", "number"),
        Field("list_price", "Listed at", "number"),
        Field("sold_price", "Sold for", "number"),
        Field("fees", "Fees", "number", show=False),
        Field("shipping", "Shipping", "number", show=False),
        Field("platform", "Platform"),
        Field("status", "Status", "select", ("inventory", "listed", "sold", "returned")),
        Field("category", "Category", show=False),
        Field("sku", "SKU", show=False),
        Field("source", "Bought from", show=False),
        Field("bought_at", "Bought", "date", show=False),
        Field("sold_at", "Sold", "date", show=False),
        Field("notes", "Notes", "longtext", show=False),
    ), ("name", "sku"), order="status = 'sold', bought_at DESC", enrich=_resale),
    Entity("trades", "trade", "Trades", "trades", (
        Field("symbol", "Symbol", required=True),
        Field("side", "Side", "select", ("long", "short"), required=True),
        Field("entry", "Entry", "number", required=True),
        Field("exit", "Exit", "number"),
        Field("stop", "Stop", "number", show=False),
        Field("target", "Target", "number", show=False),
        Field("quantity", "Qty", "number"),
        Field("fees", "Fees", "number", show=False),
        Field("setup", "Setup"),
        Field("followed_rules", "Followed rules", "bool", show=False),
        Field("emotion", "Emotion", show=False),
        Field("opened_at", "Opened", "datetime", show=False),
        Field("closed_at", "Closed", "datetime", show=False),
        Field("notes", "Notes", "longtext", show=False),
    ), ("symbol", "setup"), order="opened_at DESC", enrich=_trades),
    Entity("rules", "trading rule", "Trading rules", "trading_rules", (
        Field("rule", "Rule", required=True),
    ), ("rule",), order="id", where="active = 1"),
    Entity("watchlist", "ticker", "Watchlist", "watchlist", (
        Field("symbol", "Ticker", required=True),
        Field("note", "Note"),
        Field("alerts", "Spoken alerts", "bool"),
    ), ("symbol", "note"), order="id"),
    Entity("cravvr", "Cravvr task", "Cravvr", "cravvr_tasks", (
        Field("title", "Task", required=True),
        Field("status", "Status", "select", ("todo", "doing", "done")),
        Field("priority", "Priority (1-5)", "int"),
        Field("due_date", "Due", "date"),
        Field("notes", "Notes", "longtext", show=False),
    ), ("title",), order="status = 'done', priority DESC, id"),
    Entity("memories", "memory", "Memory", "memories", (
        Field("content", "What AMO knows", "longtext", required=True),
        Field("category", "Category", "select",
              ("general", "preference", "person", "studio", "cravvr", "reselling", "trading", "goal", "health")),
        Field("importance", "Importance (1-5)", "int"),
    ), ("content",), order="importance DESC, id DESC", where="archived = 0", soft_delete="archived"),
]}

KIND_ALIASES = {
    "goal": "goals", "client": "clients", "session": "sessions", "booking": "sessions",
    "payment": "payments", "item": "resale", "resale item": "resale", "inventory": "resale",
    "product": "resale", "trade": "trades", "rule": "rules", "trading rule": "rules",
    "task": "cravvr", "cravvr task": "cravvr", "ticker": "watchlist", "watch list": "watchlist", "memory": "memories", "note": "memories", "fact": "memories",
}


def entity(kind: str) -> Entity:
    k = (kind or "").strip().lower()
    k = KIND_ALIASES.get(k, KIND_ALIASES.get(k.rstrip("s"), k))
    if k not in ENTITIES:
        raise ValueError(f"unknown kind {kind!r} (one of: {', '.join(ENTITIES)})")
    return ENTITIES[k]


def schema() -> dict[str, Any]:
    return {e.kind: {"label": e.label, "title": e.title, "fields": [
        {"key": f.key, "label": f.label, "type": f.type, "options": list(f.options),
         "required": f.required, "show": f.show} for f in e.fields]} for e in ENTITIES.values()}


# ------------------------------------------------------------------ validation
def _coerce(db: Database, f: Field, value: Any) -> Any:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        if f.required:
            raise ValueError(f"{f.label} is required")
        return None
    try:
        if f.type == "number":
            return float(str(value).replace("$", "").replace(",", "").strip())
        if f.type == "int":
            return int(float(str(value).strip()))
    except ValueError as e:
        raise ValueError(f"{f.label} must be a number") from e
    if f.type == "bool":
        return 1 if (value is True or str(value).lower() in ("1", "true", "yes", "y", "on")) else 0
    if f.type == "date":
        return parse_when(str(value))[:10]
    if f.type == "datetime":
        return parse_when(str(value))
    if f.type == "select":
        v = str(value).strip().lower().replace("-", "_")
        if v not in f.options and v.replace(" ", "_") in f.options:  # "no show" → "no_show"
            v = v.replace(" ", "_")
        if v not in f.options:
            raise ValueError(f"{f.label} must be one of: {', '.join(f.options)}")
        return v
    if f.type == "client":
        from .crm import StudioCRM

        return StudioCRM(db).resolve_client(value)["id"]
    return str(value).strip()


def _clean(db: Database, ent: Entity, data: dict[str, Any], partial: bool) -> dict[str, Any]:
    fields = {f.key: f for f in ent.fields}
    if "client" in data and "client_id" in fields and "client_id" not in data:
        data = dict(data)
        data["client_id"] = data.pop("client")
    unknown = [k for k in data if k not in fields]
    if unknown:
        raise ValueError(f"a {ent.label} has no field {', '.join(unknown)} "
                         f"(fields: {', '.join(fields)})")
    out = {k: _coerce(db, fields[k], v) for k, v in data.items()}
    if ent.kind == "watchlist" and out.get("symbol"):
        from .market import normalize

        out["symbol"] = normalize(out["symbol"])
    if not partial:
        missing = [f.label for f in ent.fields if f.required and out.get(f.key) is None]
        if missing:
            raise ValueError(f"missing: {', '.join(missing)}")
    return out


# ------------------------------------------------------------------ operations
def _columns(db: Database, table: str) -> set[str]:
    return {r["name"] for r in db.all(f"PRAGMA table_info({table})")}


def get(kind: str, item_id: int, db: Database | None = None) -> dict[str, Any] | None:
    db = db or get_db()
    ent = entity(kind)
    row = db.one(f"SELECT * FROM {ent.table} WHERE id = ?", (item_id,))
    if row and ent.enrich:
        ent.enrich(db, [row])
    return row


def list_records(kind: str, search: str | None = None, db: Database | None = None,
                 limit: int = 500) -> list[dict[str, Any]]:
    db = db or get_db()
    ent = entity(kind)
    if search and ent.kind in ("sessions", "payments"):  # these are found by client name
        rows = list_records(ent.kind, None, db, limit)
        s = search.strip().lower()
        return [r for r in rows if s in (r.get("client_name") or "").lower()
                or s in str(r.get("note") or "").lower()]
    where, params = [], []
    if ent.where:
        where.append(ent.where)
    if search and ent.name_fields:
        where.append("(" + " OR ".join(f"{c} LIKE ?" for c in ent.name_fields) + ")")
        params += [f"%{search.strip()}%"] * len(ent.name_fields)
    sql = f"SELECT * FROM {ent.table}" + (f" WHERE {' AND '.join(where)}" if where else "")
    rows = db.all(f"{sql} ORDER BY {ent.order} LIMIT ?", (*params, limit))
    if ent.enrich:
        ent.enrich(db, rows)
    return rows


def _describe(ent: Entity, row: dict[str, Any]) -> str:
    for key in ent.name_fields + ("client_name", "symbol"):
        if row.get(key):
            extra = f" on {row['starts_at'][:16].replace('T', ' ')}" if ent.kind == "sessions" else ""
            return f"{row[key]}{extra}"
    return f"#{row['id']}"


_NUMBER_WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                 "seven": "7", "eight": "8", "nine": "9", "ten": "10", "a": "1", "an": "1"}
_RECENT = {"i just added", "just added", "the one i just added", "the one i added", "last one", "the last one",
           "latest", "the latest", "latest one", "newest", "the newest", "most recent", "the most recent",
           "last", "new one", "the new one"}


def _norm(text: str) -> str:
    """Lowercase, no punctuation, number words as digits — for forgiving name matching."""
    import re

    words = re.sub(r"[^a-z0-9 ]+", " ", str(text).lower()).split()
    return " ".join(_NUMBER_WORDS.get(w, w) for w in words if w not in ("the", "my"))


def _name_of(ent: Entity, row: dict[str, Any]) -> str:
    return " ".join(str(row.get(k) or "") for k in ent.name_fields + ("client_name",))


def resolve(kind: str, item: int | str | None, db: Database | None = None) -> dict[str, Any]:
    """Find one item by id or (part of) its name. Forgiving about punctuation, "one" vs "1",
    and close wording. Empty → the only item if there's just one; "the one I just added" → newest.
    Raises if missing or ambiguous (listing the choices)."""
    from difflib import SequenceMatcher

    db = db or get_db()
    ent = entity(kind)
    ref = str(item or "").strip().strip(" .,!?;:\"'“”").lstrip("#")
    if ref.isdigit():
        row = get(ent.kind, int(ref), db)
        if not row:
            raise ValueError(f"no {ent.label} #{ref}")
        return row

    everything = list_records(ent.kind, None, db)
    if not everything:
        raise ValueError(f"you don't have any {ent.title.lower()} yet")
    choices = "; ".join(_describe(ent, m) for m in everything[:6])
    if not ref:
        if len(everything) == 1:
            return everything[0]
        raise ValueError(f"which {ent.label}? You have: {choices}")
    if ref.lower() in _RECENT:
        return max(everything, key=lambda r: r["id"])

    matches = list_records(ent.kind, ref, db)
    if not matches:  # forgiving pass: punctuation, number words, word overlap
        target = _norm(ref)
        matches = [m for m in everything if target and target in _norm(_name_of(ent, m))]
    exact = [m for m in matches if any(_norm(m.get(k) or "") == _norm(ref) for k in ent.name_fields + ("client_name",))]
    if len(exact) == 1:
        return exact[0]
    if ent.kind == "sessions" and len(matches) > 1:  # prefer the next booked session
        upcoming = sorted((m for m in matches if m["status"] == "booked" and m["starts_at"] >= now_iso()[:10]),
                          key=lambda m: m["starts_at"])
        if upcoming:
            return upcoming[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:  # last resort: closest wording, if it's clearly the best
        scored = sorted(((SequenceMatcher(None, _norm(ref), _norm(_name_of(ent, m))).ratio(), m) for m in everything),
                        key=lambda x: x[0], reverse=True)
        best = scored[0]
        runner = scored[1][0] if len(scored) > 1 else 0
        if best[0] >= 0.6 and best[0] - runner >= 0.15:
            return best[1]
        raise ValueError(f"I couldn't find a {ent.label} matching “{ref}”. You have: {choices}")
    options = "; ".join(f"#{m['id']} {_describe(ent, m)}" for m in matches[:6])
    raise ValueError(f"more than one {ent.label} matches “{ref}”: {options}. Which one?")


def delete_many(kind: str, db: Database | None = None, cadence: str | None = None) -> dict[str, Any]:
    """Delete every item of a kind (goals can be limited to daily/weekly/monthly)."""
    db = db or get_db()
    ent = entity(kind)
    rows = list_records(ent.kind, None, db)
    if cadence and ent.kind == "goals":
        rows = [r for r in rows if r.get("cadence") == cadence]
    names = [_describe(ent, r) for r in rows]
    for r in rows:
        if ent.soft_delete:
            db.update(ent.table, r["id"], {ent.soft_delete: 1})
        else:
            with db.tx() as c:
                c.execute(f"DELETE FROM {ent.table} WHERE id = ?", (r["id"],))
    what = f"{cadence + ' ' if cadence else ''}{ent.title.lower()}"
    return {"deleted": len(rows), "kind": ent.kind, "what": what, "names": names}


def create(kind: str, data: dict[str, Any], db: Database | None = None) -> dict[str, Any]:
    db = db or get_db()
    ent = entity(kind)
    row = _clean(db, ent, dict(data), partial=False)
    cols = _columns(db, ent.table)
    stamp = now_iso()
    for col, default in (("created_at", stamp), ("bought_at", stamp), ("opened_at", stamp), ("paid_at", stamp)):
        if col in cols and not row.get(col):
            row[col] = default
    row = {k: v for k, v in row.items() if v is not None}
    new_id = db.insert(ent.table, row)
    return get(ent.kind, new_id, db)


def update(kind: str, item: int | str, changes: dict[str, Any], db: Database | None = None) -> dict[str, Any]:
    db = db or get_db()
    ent = entity(kind)
    target = resolve(ent.kind, item, db)
    clean = _clean(db, ent, dict(changes), partial=True)
    if not clean:
        raise ValueError("nothing to change")
    if ent.kind == "resale" and clean.get("sold_price") is not None and "status" not in clean:
        clean["status"] = "sold"
        clean.setdefault("sold_at", target.get("sold_at") or now_iso()[:10])
    if ent.kind == "cravvr" and "status" in clean:
        clean["done_at"] = now_iso() if clean["status"] == "done" else None
    if ent.kind == "trades" and clean.get("exit") is not None and not target.get("closed_at"):
        clean.setdefault("closed_at", now_iso())
    db.update(ent.table, target["id"], clean)
    return get(ent.kind, target["id"], db)


def delete(kind: str, item: int | str, db: Database | None = None) -> dict[str, Any]:
    """Delete one item. Returns what was deleted (so it can be confirmed or undone by hand)."""
    db = db or get_db()
    ent = entity(kind)
    target = resolve(ent.kind, item, db)
    if ent.soft_delete:
        db.update(ent.table, target["id"], {ent.soft_delete: 1})
    else:
        with db.tx() as c:
            c.execute(f"DELETE FROM {ent.table} WHERE id = ?", (target["id"],))
    return {"deleted": True, "kind": ent.kind, "label": ent.label, "id": target["id"],
            "name": _describe(ent, target)}
