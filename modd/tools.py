"""Tools the model can call to read and write the database.

Each tool is a plain function taking the Store plus keyword arguments; the
JSON schema list is what gets sent to Ollama's ``/api/chat`` ``tools`` field.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .db import AREAS, Store


def _schema(name: str, description: str, props: dict[str, dict], required: list[str] = ()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": list(required)},
        },
    }


S = {"type": "string"}
N = {"type": "number"}
I = {"type": "integer"}
B = {"type": "boolean"}
AREA = {"type": "string", "enum": list(AREAS)}

TOOLS: dict[str, tuple[dict, Callable[..., Any]]] = {}


def tool(name: str, description: str, props: dict, required: list[str] = ()):
    def register(fn: Callable[..., Any]):
        TOOLS[name] = (_schema(name, description, props, required), fn)
        return fn
    return register


# ---------------------------------------------------------------- memory
@tool("remember", "Save a durable fact, preference, decision or note about the user's life or business.",
      {"content": S, "area": AREA, "kind": {"type": "string", "enum": ["note", "fact", "preference"]}},
      ["content"])
def _remember(db: Store, content: str, area: str = "general", kind: str = "note"):
    return {"saved_id": db.remember(content, area, kind)}


@tool("recall", "Search long-term memory for anything relevant to a query.",
      {"query": S, "area": AREA}, ["query"])
def _recall(db: Store, query: str, area: str | None = None):
    return db.recall(query, area=area)


# ------------------------------------------------------------ studio CRM
@tool("add_client", "Add or update a studio client.",
      {"name": S, "phone": S, "email": S, "notes": S}, ["name"])
def _add_client(db: Store, name: str, phone: str = "", email: str = "", notes: str = ""):
    return {"client_id": db.add_client(name, phone, email, notes)}


@tool("list_clients", "List studio clients with session counts and total paid.", {})
def _list_clients(db: Store):
    return db.list_clients()


@tool("book_session", "Book a studio session. starts_at is ISO datetime like 2026-10-07T18:00.",
      {"client": S, "starts_at": S, "hours": N, "rate": {**N, "description": "hourly rate"}, "notes": S},
      ["client", "starts_at"])
def _book_session(db: Store, client: str, starts_at: str, hours: float = 1, rate: float = 0, notes: str = ""):
    return {"session_id": db.book_session(client, starts_at, hours, rate, notes)}


@tool("update_session", "Mark a studio session done/cancelled and/or record payment received.",
      {"session_id": I, "status": {"type": "string", "enum": ["booked", "done", "cancelled"]}, "paid": N},
      ["session_id"])
def _update_session(db: Store, session_id: int, status: str | None = None, paid: float | None = None):
    return {"updated": db.update_session(session_id, status, paid)}


@tool("list_sessions", "List studio sessions in a date range (ISO dates, end exclusive).",
      {"start": S, "end": S, "status": S})
def _list_sessions(db: Store, start: str | None = None, end: str | None = None, status: str | None = None):
    return db.sessions(start, end, status)


# ------------------------------------------------------------- reselling
@tool("add_item", "Add a reselling inventory item. Include list_price if it is already listed.",
      {"name": S, "cost": N, "platform": S, "list_price": N, "notes": S}, ["name", "cost"])
def _add_item(db: Store, name: str, cost: float, platform: str = "", list_price: float | None = None,
              notes: str = ""):
    return {"item_id": db.add_item(name, cost, platform, list_price, notes)}


@tool("sell_item", "Mark a reselling item sold and compute profit.",
      {"item_id": I, "sold_price": N, "fees": N, "shipping": N, "platform": S}, ["item_id", "sold_price"])
def _sell_item(db: Store, item_id: int, sold_price: float, fees: float = 0, shipping: float = 0,
               platform: str | None = None):
    return db.sell_item(item_id, sold_price, fees, shipping, platform)


@tool("list_items", "List reselling items, optionally by status.",
      {"status": {"type": "string", "enum": ["inventory", "listed", "sold"]}})
def _list_items(db: Store, status: str | None = None):
    return db.items(status)


# --------------------------------------------------------------- trading
@tool("log_trade", "Log a trade in the journal. Leave exit empty if still open.",
      {"symbol": S, "qty": N, "entry": N, "side": {"type": "string", "enum": ["long", "short"]},
       "exit": N, "fees": N, "setup": S, "followed_rules": B, "emotion": S, "notes": S},
      ["symbol", "qty", "entry"])
def _log_trade(db: Store, **kw):
    return db.get_trade(db.log_trade(**kw))


@tool("close_trade", "Close an open trade at an exit price.",
      {"trade_id": I, "exit": N, "fees": N}, ["trade_id", "exit"])
def _close_trade(db: Store, trade_id: int, exit: float, fees: float | None = None):
    return db.close_trade(trade_id, exit, fees)


@tool("trade_stats", "Win rate, P&L and rule-break stats for closed trades, optionally since a date.",
      {"since": S})
def _trade_stats(db: Store, since: str | None = None):
    return db.trade_stats(since)


@tool("list_trades", "Recent trades, or only open positions.", {"open_only": B, "limit": I})
def _list_trades(db: Store, open_only: bool = False, limit: int = 20):
    return db.trades(limit, open_only)


@tool("add_trading_rule", "Add a personal trading rule to follow.", {"rule": S}, ["rule"])
def _add_rule(db: Store, rule: str):
    return {"rule_id": db.add_rule(rule)}


# ------------------------------------------------------- goals / revenue
@tool("add_goal", "Add a goal for today (or a given ISO date).", {"text": S, "area": AREA, "day": S}, ["text"])
def _add_goal(db: Store, text: str, area: str = "general", day: str | None = None):
    return {"goal_id": db.add_goal(text, area, day)}


@tool("complete_goal", "Mark a goal done.", {"goal_id": I}, ["goal_id"])
def _complete_goal(db: Store, goal_id: int):
    return {"updated": db.complete_goal(goal_id)}


@tool("revenue", "Revenue across studio, reselling profit and trading P&L for a period.",
      {"period": {"type": "string", "enum": ["day", "week", "month", "year"]}})
def _revenue(db: Store, period: str = "month"):
    return db.revenue_for(period)


@tool("dashboard", "Today's snapshot: goals, sessions, unpaid invoices, open trades, revenue.", {})
def _dashboard(db: Store):
    return db.dashboard()


def schemas() -> list[dict]:
    return [schema for schema, _ in TOOLS.values()]


def call(db: Store, name: str, arguments: dict | str | None) -> str:
    """Run a tool and return a JSON string result (errors are returned, not raised,
    so the model can see and recover from them)."""
    if name not in TOOLS:
        return json.dumps({"error": f"unknown tool {name}"})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return json.dumps({"error": "arguments were not valid JSON"})
    _, fn = TOOLS[name]
    try:
        result = fn(db, **(arguments or {}))
    except Exception as e:  # noqa: BLE001 — surface to the model
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
    return json.dumps(result, default=str)
