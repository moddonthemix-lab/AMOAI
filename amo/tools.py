"""Tools the assistant can call. Each one is exposed to the local model through
Ollama's function-calling API, so you can say "book Jay for Friday at 7, 3 hours
at $50" and it lands in the CRM.

Add a tool: decorate a function with @tool, describe its params, done.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from .crm import StudioCRM
from .cravvr import Cravvr
from .db import get_db
from .finance import Finance, dashboard as _dashboard
from .goals import Goals
from .memory import Memory
from .notify import notify as _notify
from .reselling import Reselling
from .trading import TradingJournal

# param spec: name -> (json type, description, required)
ParamSpec = dict[str, tuple[str, str, bool]]


@dataclass
class Tool:
    name: str
    description: str
    params: ParamSpec
    fn: Callable[..., Any]

    def schema(self) -> dict[str, Any]:
        props = {}
        for pname, (ptype, desc, _req) in self.params.items():
            if ptype.startswith("enum:"):
                props[pname] = {"type": "string", "enum": ptype[5:].split("|"), "description": desc}
            else:
                props[pname] = {"type": ptype, "description": desc}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": [p for p, (_, _, req) in self.params.items() if req],
                },
            },
        }


REGISTRY: dict[str, Tool] = {}


def tool(description: str, **params: tuple[str, str, bool]):
    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        REGISTRY[fn.__name__] = Tool(fn.__name__, description, params, fn)
        return fn
    return deco


def schemas(names: list[str] | None = None) -> list[dict[str, Any]]:
    return [t.schema() for n, t in REGISTRY.items() if names is None or n in names]


def _coerce(value: Any, ptype: str) -> Any:
    """Small models often send numbers as strings; fix that up."""
    if value is None:
        return None
    try:
        if ptype == "number":
            return float(str(value).replace("$", "").replace(",", ""))
        if ptype == "integer":
            return int(float(str(value)))
        if ptype == "boolean":
            return value if isinstance(value, bool) else str(value).lower() in ("true", "yes", "1", "y")
    except ValueError:
        return value
    return value


def call(name: str, arguments: dict[str, Any] | str | None) -> str:
    """Run a tool and return a JSON string for the model. Errors are returned, not raised,
    so the model can recover (e.g. ask which 'Jay' you meant)."""
    t = REGISTRY.get(name)
    if not t:
        return json.dumps({"error": f"unknown tool {name}"})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return json.dumps({"error": "arguments were not valid JSON"})
    args = {k: _coerce(v, t.params[k][0]) for k, v in (arguments or {}).items()
            if k in t.params and v not in ("", None)}
    missing = [p for p, (_, _, req) in t.params.items() if req and p not in args]
    if missing:
        return json.dumps({"error": f"missing required argument(s): {', '.join(missing)}"})
    try:
        result = t.fn(**args)
    except (ValueError, TypeError) as e:
        return json.dumps({"error": str(e)})
    return json.dumps(result, default=str)


# ---------------------------------------------------------------- memory
@tool(
    "Save a durable fact about the user, their businesses, people, preferences or rules so you "
    "remember it in future conversations. Use whenever the user shares something worth remembering.",
    content=("string", "The fact, written as a standalone sentence.", True),
    category=("enum:general|preference|person|studio|cravvr|reselling|trading|goal|health",
              "Which area this belongs to.", False),
    importance=("integer", "1 (trivia) to 5 (core identity / hard rule). Default 3.", False),
)
def remember(content: str, category: str = "general", importance: int = 3):
    m = Memory(get_db()).add(content, category, importance, source="chat")
    return {"saved": True, "id": m["id"], "duplicate": m.get("duplicate", False)}


@tool("Search long-term memory for facts relevant to a question.",
      query=("string", "What to look for.", True),
      category=("string", "Optional category filter.", False))
def recall(query: str, category: str | None = None):
    return Memory(get_db()).search(query, limit=10, category=category)


@tool("Forget (archive) a memory that is wrong or outdated.",
      memory_id=("integer", "The memory id.", True))
def forget(memory_id: int):
    return {"forgotten": Memory(get_db()).forget(memory_id)}


# ---------------------------------------------------------------- studio CRM
@tool("Add a studio client to the CRM.",
      name=("string", "Client's real name.", True),
      artist_name=("string", "Stage / artist name.", False),
      phone=("string", "Phone number.", False),
      email=("string", "Email.", False),
      instagram=("string", "Instagram handle.", False),
      notes=("string", "Anything useful: genre, preferences, engineer notes.", False),
      status=("enum:lead|active|inactive", "Default active.", False))
def add_client(name: str, **kw):
    return StudioCRM(get_db()).add_client(name, **kw)


@tool("Look up studio clients by name, artist name, phone, email or instagram.",
      query=("string", "Search text.", True))
def find_client(query: str):
    crm = StudioCRM(get_db())
    return [crm.get_client(c["id"]) for c in crm.find_client(query)]


@tool("Update a studio client's details.",
      client_id=("integer", "Client id.", True),
      artist_name=("string", "", False), phone=("string", "", False), email=("string", "", False),
      instagram=("string", "", False), notes=("string", "Replaces existing notes.", False),
      status=("enum:lead|active|inactive", "", False))
def update_client(client_id: int, **kw):
    return StudioCRM(get_db()).update_client(client_id, **kw)


@tool("Book a studio session for a client.",
      client=("string", "Client name or id.", True),
      starts_at=("string", "Start as 'YYYY-MM-DD HH:MM' (24h, local time).", True),
      hours=("number", "Length in hours. Default 2.", False),
      service=("enum:recording|mixing|mastering|production|other", "Default recording.", False),
      rate=("number", "Hourly rate in dollars.", False),
      notes=("string", "Session notes.", False))
def book_session(client: str, starts_at: str, **kw):
    return StudioCRM(get_db()).book_session(client, starts_at, **kw)


@tool("Change a booked session: reschedule, cancel, mark no-show, change rate or hours.",
      session_id=("integer", "Session id.", True),
      starts_at=("string", "New start 'YYYY-MM-DD HH:MM'.", False),
      hours=("number", "", False), rate=("number", "", False),
      status=("enum:booked|completed|cancelled|no_show", "", False),
      notes=("string", "", False))
def update_session(session_id: int, **kw):
    return StudioCRM(get_db()).update_session(session_id, **kw)


@tool("Mark a studio session completed, optionally recording the payment received.",
      session_id=("integer", "Session id.", True),
      paid=("number", "Amount paid now, if any.", False),
      method=("string", "cash, cashapp, zelle, card…", False))
def complete_session(session_id: int, paid: float | None = None, method: str | None = None):
    return StudioCRM(get_db()).complete_session(session_id, paid, method)


@tool("Record money received (studio, Cravvr, or other business).",
      amount=("number", "Dollars.", True),
      client=("string", "Client name or id (studio).", False),
      session_id=("integer", "Session it pays for.", False),
      method=("string", "Payment method.", False),
      business=("enum:studio|cravvr|reselling|trading|other", "Default studio.", False),
      note=("string", "", False),
      paid_at=("string", "Date if not today.", False))
def record_payment(amount: float, **kw):
    return StudioCRM(get_db()).record_payment(amount, **kw)


@tool("Studio schedule for a given day.",
      day=("string", "YYYY-MM-DD, 'today' or 'tomorrow'. Default today.", False))
def studio_schedule(day: str | None = None):
    return StudioCRM(get_db()).schedule_for(day)


@tool("Booked studio sessions in the next N days.",
      days=("integer", "Default 7.", False))
def upcoming_sessions(days: int = 7):
    return StudioCRM(get_db()).upcoming(days)


@tool("Completed sessions that still have a balance owed.")
def unpaid_sessions():
    return StudioCRM(get_db()).unpaid_sessions()


@tool("Active clients who haven't booked in a while — people to follow up with.",
      days=("integer", "Days since last session. Default 45.", False))
def clients_to_follow_up(days: int = 45):
    return StudioCRM(get_db()).inactive_clients(days)


# ---------------------------------------------------------------- reselling
@tool("Add an item bought for resale.",
      name=("string", "Item name, e.g. 'Jordan 4 Bred size 10'.", True),
      cost=("number", "What you paid.", True),
      sku=("string", "", False), category=("string", "sneakers, electronics, clothing…", False),
      source=("string", "Where it was bought.", False),
      list_price=("number", "Asking price if already listed.", False),
      platform=("string", "ebay, stockx, goat, mercari, depop, fb…", False),
      bought_at=("string", "Purchase date if not today.", False),
      notes=("string", "", False))
def add_resale_item(name: str, cost: float, **kw):
    return Reselling(get_db()).add_item(name, cost, **kw)


@tool("Mark an inventory item as listed for sale.",
      item=("string", "Item name or id.", True),
      price=("number", "Listing price.", True),
      platform=("string", "", False))
def list_resale_item(item: str, price: float, platform: str | None = None):
    return Reselling(get_db()).list_item(item, price, platform)


@tool("Mark a resale item as sold and compute profit.",
      item=("string", "Item name or id.", True),
      sold_price=("number", "Sale price.", True),
      platform=("string", "", False),
      fees=("number", "Platform fees.", False),
      shipping=("number", "Shipping cost you paid.", False),
      sold_at=("string", "Date if not today.", False))
def mark_item_sold(item: str, sold_price: float, **kw):
    return Reselling(get_db()).mark_sold(item, sold_price, **kw)


@tool("Unsold reselling inventory.",
      status=("enum:inventory|listed", "Filter; omit for both.", False))
def resale_inventory(status: str | None = None):
    return Reselling(get_db()).inventory(status)


@tool("Reselling performance: revenue, profit, ROI, days-to-sell, by platform, plus stale inventory.",
      period=("enum:today|week|month|year|all", "Default month.", False))
def resale_summary(period: str = "month"):
    from .finance import period_start
    r = Reselling(get_db())
    return {**r.summary(period_start(period).isoformat()), "stale_items": r.stale(30)}


# ---------------------------------------------------------------- trading
@tool("Log a new trade in the trading journal.",
      symbol=("string", "Ticker, e.g. SPY, NQ, BTC.", True),
      side=("enum:long|short", "Direction.", True),
      entry=("number", "Entry price.", True),
      quantity=("number", "Shares / contracts. Default 1.", False),
      stop=("number", "Stop loss.", False), target=("number", "Profit target.", False),
      setup=("string", "Setup name, e.g. 'ORB', 'VWAP reclaim'.", False),
      exit=("number", "Exit price if already closed.", False),
      followed_rules=("boolean", "Did the trade follow your rules?", False),
      emotion=("string", "calm, FOMO, revenge, confident…", False),
      notes=("string", "", False))
def log_trade(symbol: str, side: str, entry: float, **kw):
    tj = TradingJournal(get_db())
    exit_price = kw.pop("exit", None)
    t = tj.open_trade(symbol, side, entry, **kw)
    if exit_price is not None:
        t = tj.close_trade(t["id"], exit_price)
    return t


@tool("Close an open trade.",
      trade_id=("integer", "Trade id.", True),
      exit=("number", "Exit price.", True),
      followed_rules=("boolean", "", False),
      emotion=("string", "", False),
      notes=("string", "Lesson / what happened.", False),
      fees=("number", "", False))
def close_trade(trade_id: int, exit: float, **kw):
    return TradingJournal(get_db()).close_trade(trade_id, exit, **kw)


@tool("Open trading positions.")
def open_positions():
    return TradingJournal(get_db()).open_positions()


@tool("Trading performance stats: P&L, win rate, profit factor, avg R, rule adherence, by setup.",
      period=("enum:today|week|month|year|all", "Default month.", False))
def trading_stats(period: str = "month"):
    from .finance import period_start
    return TradingJournal(get_db()).stats(period_start(period).isoformat())


@tool("List the user's personal trading rules.")
def trading_rules():
    return TradingJournal(get_db()).rules()


@tool("Add a personal trading rule.", rule=("string", "The rule.", True))
def add_trading_rule(rule: str):
    return TradingJournal(get_db()).add_rule(rule)


# ---------------------------------------------------------------- goals
@tool("Create a goal.",
      title=("string", "The goal.", True),
      area=("enum:studio|cravvr|reselling|trading|health|general", "", False),
      cadence=("enum:daily|weekly|monthly|once", "Default daily.", False),
      target=("number", "Numeric target per period, if measurable.", False),
      due_date=("string", "For one-off goals.", False))
def add_goal(title: str, **kw):
    return Goals(get_db()).add(title, **kw)


@tool("Check in on a goal (mark done today, or log progress toward a numeric target).",
      goal=("string", "Goal title or id.", True),
      done=("boolean", "Default true.", False),
      value=("number", "Progress amount for measurable goals.", False),
      note=("string", "", False))
def check_in_goal(goal: str, **kw):
    return Goals(get_db()).check_in(goal, **kw)


@tool("List active goals with streaks and progress.",
      area=("string", "Optional area filter.", False))
def list_goals(area: str | None = None):
    return Goals(get_db()).active(area)


# ---------------------------------------------------------------- cravvr
@tool("Add a Cravvr task.",
      title=("string", "", True),
      priority=("integer", "1-5, default 3.", False),
      due_date=("string", "", False), notes=("string", "", False))
def add_cravvr_task(title: str, **kw):
    return Cravvr(get_db()).add_task(title, **kw)


@tool("Update a Cravvr task's status.",
      task_id=("integer", "", True),
      status=("enum:todo|doing|done", "", True))
def update_cravvr_task(task_id: int, status: str):
    return Cravvr(get_db()).set_status(task_id, status)


@tool("Open Cravvr tasks by priority.")
def cravvr_tasks():
    return Cravvr(get_db()).open_tasks()


# ---------------------------------------------------------------- overview
@tool("Revenue across all businesses for a period.",
      period=("enum:today|week|month|year|all", "Default month.", False))
def revenue(period: str = "month"):
    return Finance(get_db()).revenue(period)


@tool("Full snapshot: today's sessions, goals, revenue, trading, reselling, Cravvr, notifications.")
def overview():
    return _dashboard(get_db())


@tool("Send the user a notification (dashboard + phone push).",
      title=("string", "", True), body=("string", "", False),
      level=("enum:info|warn|urgent", "", False))
def send_notification(title: str, body: str = "", level: str = "info"):
    return _notify(title, body, level)


# ---------------------------------------------------------------- routing
# Sending all ~34 tool definitions costs ~4k prompt tokens per message, which is slow on a
# CPU-only machine. Each message only gets the tool groups it plausibly needs.
CORE = ["remember", "recall"]
GROUPS: dict[str, tuple[list[str], str]] = {
    "studio": (
        ["add_client", "find_client", "update_client", "book_session", "update_session",
         "complete_session", "record_payment", "studio_schedule", "upcoming_sessions",
         "unpaid_sessions", "clients_to_follow_up"],
        r"client|artist|\bbook|session|studio|schedul|calendar|recording|\bmix|master|\bpaid|\bpay|\bowe|"
        r"balance|\brate\b|follow.?up|no.?show|cancel|resched|\bcash ?app|zelle|venmo",
    ),
    "reselling": (
        ["add_resale_item", "list_resale_item", "mark_item_sold", "resale_inventory", "resale_summary"],
        r"bought|\bbuy|\bsold|\bsell|flip|resell|resale|inventory|listing|\blist(ed)?\b|ebay|stockx|goat|"
        r"mercari|depop|poshmark|sneaker|shoe|jordan|\bdunk|yeezy|\bitem|margin|\broi\b",
    ),
    "trading": (
        ["log_trade", "close_trade", "open_positions", "trading_stats", "trading_rules", "add_trading_rule"],
        r"trad(e|es|ing)|\blong\b|\bshort\b|\bstop\b|entry|\bexit|p&l|\bpnl|position|\brules?\b|setup|"
        r"futures|options|\bcalls?\b|\bputs?\b|\bes\b|\bnq\b|\bmnq\b|\bspy\b|\bqqq\b|ticker|win ?rate|contracts?",
    ),
    "goals": (
        ["add_goal", "check_in_goal", "list_goals"],
        r"goal|streak|habit|check.?in|\bdone\b|finished|completed|daily",
    ),
    "cravvr": (
        ["add_cravvr_task", "update_cravvr_task", "cravvr_tasks"],
        r"cravvr|\btasks?\b|to.?do",
    ),
    "money": (
        ["revenue", "overview", "record_payment"],
        r"revenue|money|\bmade\b|\bmake\b|income|earn|profit|target|how am i doing|how.?s (my|business)|"
        r"overview|summary|brief|dashboard|this (week|month)|today|plan my day|what.?s (up|next|on)",
    ),
    "admin": (
        ["forget", "send_notification"],
        r"forget|wrong|outdated|remind|notify|notification|alert",
    ),
}
_GROUP_RE = {g: re.compile(rx, re.I) for g, (_, rx) in GROUPS.items()}


def route(text: str) -> list[str]:
    """Pick the tool names relevant to `text` (recent user messages)."""
    names = list(CORE)
    for g, (group_tools, _) in GROUPS.items():
        if _GROUP_RE[g].search(text):
            names += [t for t in group_tools if t not in names]
    return names


# Sanity check at import: every declared param exists on the function.
for _t in REGISTRY.values():
    _sig = inspect.signature(_t.fn)
    _has_kw = any(p.kind is p.VAR_KEYWORD for p in _sig.parameters.values())
    for _p in _t.params:
        assert _has_kw or _p in _sig.parameters, f"{_t.name}: param {_p} not in signature"
for _g, (_names, _) in GROUPS.items():
    for _n in _names:
        assert _n in REGISTRY, f"routing group {_g}: unknown tool {_n}"
