"""SQLite storage: long-term memory plus the business modules.

One file (``~/.modd/modd.db``) holds everything, so moving to the Pi in
Phase 2 is just copying that file. Only the standard library is used.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL DEFAULT 'note',   -- note | fact | preference | conversation
    area        TEXT NOT NULL DEFAULT 'general',-- general | studio | cravvr | reselling | trading
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content, area, content='memories', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, area) VALUES (new.id, new.content, new.area);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, area) VALUES ('delete', old.id, old.content, old.area);
END;

CREATE TABLE IF NOT EXISTS clients (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phone       TEXT,
    email       TEXT,
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS studio_sessions (
    id          INTEGER PRIMARY KEY,
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    starts_at   TEXT NOT NULL,                  -- ISO datetime
    hours       REAL NOT NULL DEFAULT 1,
    rate        REAL NOT NULL DEFAULT 0,        -- per hour
    status      TEXT NOT NULL DEFAULT 'booked', -- booked | done | cancelled
    paid        REAL NOT NULL DEFAULT 0,        -- amount received
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    platform    TEXT,                           -- ebay | stockx | depop | ...
    cost        REAL NOT NULL DEFAULT 0,
    list_price  REAL,
    sold_price  REAL,
    fees        REAL NOT NULL DEFAULT 0,
    shipping    REAL NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'inventory', -- inventory | listed | sold
    bought_at   TEXT NOT NULL DEFAULT (date('now','localtime')),
    sold_at     TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL DEFAULT 'long',   -- long | short
    qty             REAL NOT NULL,
    entry           REAL NOT NULL,
    exit            REAL,
    fees            REAL NOT NULL DEFAULT 0,
    opened_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    closed_at       TEXT,
    setup           TEXT,
    followed_rules  INTEGER,                        -- 1 / 0 / NULL (unknown)
    emotion         TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS trading_rules (
    id      INTEGER PRIMARY KEY,
    rule    TEXT NOT NULL,
    active  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS goals (
    id      INTEGER PRIMARY KEY,
    day     TEXT NOT NULL DEFAULT (date('now','localtime')),
    area    TEXT NOT NULL DEFAULT 'general',
    text    TEXT NOT NULL,
    done    INTEGER NOT NULL DEFAULT 0
);
"""

AREAS = ("general", "studio", "cravvr", "reselling", "trading")

_TRADE_PNL = """
CASE WHEN exit IS NULL THEN NULL
     WHEN side = 'short' THEN (entry - exit) * qty - fees
     ELSE (exit - entry) * qty - fees END
"""
_ITEM_PROFIT = "COALESCE(sold_price, 0) - cost - fees - shipping"


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def _fts_query(text: str) -> str:
    """Turn free text into a forgiving FTS5 query (any word, prefix match)."""
    words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 2]
    return " OR ".join(f'"{w}"*' for w in words)


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def _insert(self, sql: str, params: tuple) -> int:
        with self.conn:
            return self.conn.execute(sql, params).lastrowid

    # ------------------------------------------------------------------ memory
    def remember(self, content: str, area: str = "general", kind: str = "note") -> int:
        return self._insert(
            "INSERT INTO memories(content, area, kind) VALUES (?, ?, ?)", (content, area, kind)
        )

    def forget(self, memory_id: int) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount > 0

    def recall(self, query: str = "", limit: int = 8, area: str | None = None) -> list[dict]:
        q = _fts_query(query)
        area_sql, params = ("AND m.area = ?", [area]) if area else ("", [])
        if q:
            sql = f"""SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid
                      WHERE memories_fts MATCH ? {area_sql}
                      ORDER BY bm25(memories_fts) LIMIT ?"""
            return _rows(self.conn.execute(sql, [q, *params, limit]))
        sql = f"SELECT m.* FROM memories m WHERE 1=1 {area_sql} ORDER BY m.id DESC LIMIT ?"
        return _rows(self.conn.execute(sql, [*params, limit]))

    # -------------------------------------------------------------- studio CRM
    def add_client(self, name: str, phone: str = "", email: str = "", notes: str = "") -> int:
        with self.conn:
            self.conn.execute(
                """INSERT INTO clients(name, phone, email, notes) VALUES (?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     phone = COALESCE(NULLIF(excluded.phone, ''), phone),
                     email = COALESCE(NULLIF(excluded.email, ''), email),
                     notes = TRIM(COALESCE(notes, '') || ' ' || excluded.notes)""",
                (name, phone, email, notes),
            )
        return self.get_client(name)["id"]

    def get_client(self, name: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM clients WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def list_clients(self) -> list[dict]:
        return _rows(self.conn.execute(
            """SELECT c.*, COUNT(s.id) AS sessions,
                      COALESCE(SUM(s.paid), 0) AS total_paid,
                      MAX(s.starts_at) AS last_session
               FROM clients c LEFT JOIN studio_sessions s
                 ON s.client_id = c.id AND s.status != 'cancelled'
               GROUP BY c.id ORDER BY c.name"""
        ))

    def book_session(self, client: str, starts_at: str, hours: float = 1, rate: float = 0,
                     notes: str = "") -> int:
        c = self.get_client(client) or {"id": self.add_client(client)}
        return self._insert(
            "INSERT INTO studio_sessions(client_id, starts_at, hours, rate, notes) VALUES (?, ?, ?, ?, ?)",
            (c["id"], starts_at, hours, rate, notes),
        )

    def update_session(self, session_id: int, status: str | None = None,
                       paid: float | None = None) -> bool:
        with self.conn:
            cur = self.conn.execute(
                """UPDATE studio_sessions SET status = COALESCE(?, status), paid = COALESCE(?, paid)
                   WHERE id = ?""",
                (status, paid, session_id),
            )
        return cur.rowcount > 0

    def sessions(self, start: str | None = None, end: str | None = None,
                 status: str | None = None) -> list[dict]:
        sql = """SELECT s.*, c.name AS client, s.hours * s.rate AS amount_due
                 FROM studio_sessions s JOIN clients c ON c.id = s.client_id WHERE 1=1"""
        params: list[Any] = []
        if start:
            sql += " AND s.starts_at >= ?"
            params.append(start)
        if end:
            sql += " AND s.starts_at < ?"
            params.append(end)
        if status:
            sql += " AND s.status = ?"
            params.append(status)
        return _rows(self.conn.execute(sql + " ORDER BY s.starts_at", params))

    def unpaid_sessions(self) -> list[dict]:
        return [s for s in self.sessions(status="done") if s["paid"] < s["amount_due"]]

    # --------------------------------------------------------------- reselling
    def add_item(self, name: str, cost: float, platform: str = "", list_price: float | None = None,
                 notes: str = "") -> int:
        status = "listed" if list_price else "inventory"
        return self._insert(
            "INSERT INTO items(name, cost, platform, list_price, status, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (name, cost, platform, list_price, status, notes),
        )

    def sell_item(self, item_id: int, sold_price: float, fees: float = 0, shipping: float = 0,
                  platform: str | None = None, sold_at: str | None = None) -> dict | None:
        with self.conn:
            self.conn.execute(
                """UPDATE items SET status = 'sold', sold_price = ?, fees = ?, shipping = ?,
                     platform = COALESCE(?, platform), sold_at = COALESCE(?, date('now','localtime'))
                   WHERE id = ?""",
                (sold_price, fees, shipping, platform, sold_at, item_id),
            )
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> dict | None:
        row = self.conn.execute(
            f"SELECT *, {_ITEM_PROFIT} AS profit FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        return dict(row) if row else None

    def items(self, status: str | None = None) -> list[dict]:
        sql = f"SELECT *, CASE WHEN status = 'sold' THEN {_ITEM_PROFIT} END AS profit FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        return _rows(self.conn.execute(sql + " ORDER BY id DESC", params))

    # ----------------------------------------------------------------- trading
    def log_trade(self, symbol: str, qty: float, entry: float, side: str = "long",
                  exit: float | None = None, fees: float = 0, setup: str = "",
                  followed_rules: bool | None = None, emotion: str = "", notes: str = "",
                  opened_at: str | None = None) -> int:
        side = side.lower()
        if side not in ("long", "short"):
            raise ValueError("side must be 'long' or 'short'")
        closed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if exit is not None else None
        return self._insert(
            """INSERT INTO trades(symbol, side, qty, entry, exit, fees, setup, followed_rules,
                                  emotion, notes, opened_at, closed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now','localtime')), ?)""",
            (symbol.upper(), side, qty, entry, exit, fees, setup,
             None if followed_rules is None else int(followed_rules), emotion, notes,
             opened_at, closed_at),
        )

    def close_trade(self, trade_id: int, exit: float, fees: float | None = None) -> dict | None:
        with self.conn:
            self.conn.execute(
                """UPDATE trades SET exit = ?, fees = COALESCE(?, fees), closed_at = datetime('now','localtime')
                   WHERE id = ?""",
                (exit, fees, trade_id),
            )
        return self.get_trade(trade_id)

    def get_trade(self, trade_id: int) -> dict | None:
        row = self.conn.execute(
            f"SELECT *, {_TRADE_PNL} AS pnl FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        return dict(row) if row else None

    def trades(self, limit: int = 50, open_only: bool = False) -> list[dict]:
        where = "WHERE exit IS NULL" if open_only else ""
        return _rows(self.conn.execute(
            f"SELECT *, {_TRADE_PNL} AS pnl FROM trades {where} ORDER BY opened_at DESC LIMIT ?",
            (limit,),
        ))

    def trade_stats(self, since: str | None = None) -> dict:
        where = "WHERE exit IS NOT NULL" + (" AND closed_at >= ?" if since else "")
        params = (since,) if since else ()
        r = self.conn.execute(
            f"""SELECT COUNT(*) AS n,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       COALESCE(SUM(pnl), 0) AS total,
                       AVG(CASE WHEN pnl > 0 THEN pnl END) AS avg_win,
                       AVG(CASE WHEN pnl <= 0 THEN pnl END) AS avg_loss,
                       SUM(CASE WHEN followed_rules = 0 THEN 1 ELSE 0 END) AS rule_breaks,
                       COALESCE(SUM(CASE WHEN followed_rules = 0 THEN pnl END), 0) AS pnl_rule_breaks
                FROM (SELECT *, {_TRADE_PNL} AS pnl FROM trades) {where}""",
            params,
        ).fetchone()
        stats = dict(r)
        stats["win_rate"] = round(stats["wins"] / stats["n"], 3) if stats["n"] else None
        return stats

    def add_rule(self, rule: str) -> int:
        return self._insert("INSERT INTO trading_rules(rule) VALUES (?)", (rule,))

    def rules(self) -> list[dict]:
        return _rows(self.conn.execute("SELECT * FROM trading_rules WHERE active = 1 ORDER BY id"))

    # ------------------------------------------------------------------- goals
    def add_goal(self, text: str, area: str = "general", day: str | None = None) -> int:
        return self._insert(
            "INSERT INTO goals(text, area, day) VALUES (?, ?, COALESCE(?, date('now','localtime')))",
            (text, area, day),
        )

    def complete_goal(self, goal_id: int, done: bool = True) -> bool:
        with self.conn:
            return self.conn.execute(
                "UPDATE goals SET done = ? WHERE id = ?", (int(done), goal_id)
            ).rowcount > 0

    def goals(self, day: str | None = None) -> list[dict]:
        day = day or date.today().isoformat()
        return _rows(self.conn.execute("SELECT * FROM goals WHERE day = ? ORDER BY id", (day,)))

    # ----------------------------------------------------------------- revenue
    def revenue(self, start: str, end: str) -> dict:
        """Money in across all areas for [start, end) (ISO dates)."""
        studio = self.conn.execute(
            "SELECT COALESCE(SUM(paid), 0) FROM studio_sessions WHERE starts_at >= ? AND starts_at < ?",
            (start, end),
        ).fetchone()[0]
        reselling = self.conn.execute(
            f"""SELECT COALESCE(SUM({_ITEM_PROFIT}), 0) FROM items
                WHERE status = 'sold' AND sold_at >= ? AND sold_at < ?""",
            (start, end),
        ).fetchone()[0]
        trading = self.conn.execute(
            f"""SELECT COALESCE(SUM({_TRADE_PNL}), 0) FROM trades
                WHERE exit IS NOT NULL AND closed_at >= ? AND closed_at < ?""",
            (start, end),
        ).fetchone()[0]
        out = {"studio": studio, "reselling": reselling, "trading": trading}
        out = {k: round(v, 2) for k, v in out.items()}
        out["total"] = round(sum(out.values()), 2)
        return out

    def revenue_for(self, period: str = "month", today: date | None = None) -> dict:
        today = today or date.today()
        if period == "day":
            start = today
        elif period == "week":
            start = today - timedelta(days=today.weekday())
        elif period == "year":
            start = today.replace(month=1, day=1)
        else:
            start = today.replace(day=1)
        end = today + timedelta(days=1)
        return {"period": period, "start": start.isoformat(), **self.revenue(start.isoformat(), end.isoformat())}

    # --------------------------------------------------------------- dashboard
    def dashboard(self, today: date | None = None) -> dict:
        """Compact snapshot for the CLI, the assistant prompt and the ESP32 screen."""
        today = today or date.today()
        tomorrow = (today + timedelta(days=1)).isoformat()
        return {
            "date": today.isoformat(),
            "goals": self.goals(today.isoformat()),
            "sessions_today": self.sessions(today.isoformat(), tomorrow),
            "unpaid_sessions": self.unpaid_sessions(),
            "open_trades": self.trades(open_only=True),
            "inventory_count": len(self.items("inventory")) + len(self.items("listed")),
            "revenue_month": self.revenue_for("month", today),
            "revenue_today": self.revenue_for("day", today),
        }
