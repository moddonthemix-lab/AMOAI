"""SQLite storage: one file holds AMO's entire memory and business data.

Migrations are a simple ordered list; each runs once and is recorded in
`schema_version`. Add new migrations to the end, never edit old ones.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .config import settings

MIGRATIONS: list[str] = [
    # 1 — memory
    """
    CREATE TABLE memories (
        id INTEGER PRIMARY KEY,
        content TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT 'general',   -- general|preference|studio|cravvr|reselling|trading|goal|person
        importance INTEGER NOT NULL DEFAULT 3,       -- 1..5
        source TEXT NOT NULL DEFAULT 'manual',       -- manual|chat|reflection|import
        embedding TEXT,                              -- JSON float array (optional)
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        use_count INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0
    );
    CREATE VIRTUAL TABLE memories_fts USING fts5(content, category, content='memories', content_rowid='id');
    CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, content, category) VALUES (new.id, new.content, new.category);
    END;
    CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, category) VALUES ('delete', old.id, old.content, old.category);
    END;
    CREATE TRIGGER memories_au AFTER UPDATE OF content, category ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, content, category) VALUES ('delete', old.id, old.content, old.category);
        INSERT INTO memories_fts(rowid, content, category) VALUES (new.id, new.content, new.category);
    END;

    CREATE TABLE conversations (
        id INTEGER PRIMARY KEY,
        channel TEXT NOT NULL DEFAULT 'webui',      -- webui|voice|cli|device
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        learned INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE reflections (
        id INTEGER PRIMARY KEY,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        summary TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    # 2 — studio CRM
    """
    CREATE TABLE clients (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        artist_name TEXT,
        phone TEXT,
        email TEXT,
        instagram TEXT,
        notes TEXT,
        status TEXT NOT NULL DEFAULT 'active',      -- lead|active|inactive
        created_at TEXT NOT NULL
    );
    CREATE TABLE studio_sessions (
        id INTEGER PRIMARY KEY,
        client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
        starts_at TEXT NOT NULL,
        hours REAL NOT NULL DEFAULT 2,
        service TEXT NOT NULL DEFAULT 'recording',   -- recording|mixing|mastering|production|other
        rate REAL NOT NULL DEFAULT 0,                -- price per hour
        status TEXT NOT NULL DEFAULT 'booked',       -- booked|completed|cancelled|no_show
        notes TEXT,
        reminded INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE payments (
        id INTEGER PRIMARY KEY,
        client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
        session_id INTEGER REFERENCES studio_sessions(id) ON DELETE SET NULL,
        amount REAL NOT NULL,
        method TEXT,
        business TEXT NOT NULL DEFAULT 'studio',     -- studio|cravvr|reselling|trading|other
        note TEXT,
        paid_at TEXT NOT NULL
    );
    """,
    # 3 — reselling
    """
    CREATE TABLE resale_items (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        sku TEXT,
        category TEXT,
        source TEXT,                                 -- where you bought it
        cost REAL NOT NULL DEFAULT 0,
        list_price REAL,
        sold_price REAL,
        fees REAL NOT NULL DEFAULT 0,
        shipping REAL NOT NULL DEFAULT 0,
        platform TEXT,                               -- ebay|stockx|goat|mercari|depop|fb|other
        status TEXT NOT NULL DEFAULT 'inventory',    -- inventory|listed|sold|returned
        bought_at TEXT NOT NULL,
        listed_at TEXT,
        sold_at TEXT,
        notes TEXT
    );
    """,
    # 4 — trading journal
    """
    CREATE TABLE trades (
        id INTEGER PRIMARY KEY,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,                          -- long|short
        setup TEXT,
        entry REAL NOT NULL,
        exit REAL,
        stop REAL,
        target REAL,
        quantity REAL NOT NULL DEFAULT 1,
        fees REAL NOT NULL DEFAULT 0,
        opened_at TEXT NOT NULL,
        closed_at TEXT,
        followed_rules INTEGER,                      -- 1 yes, 0 no, NULL unknown
        emotion TEXT,
        notes TEXT
    );
    CREATE TABLE trading_rules (
        id INTEGER PRIMARY KEY,
        rule TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    """,
    # 5 — goals & notifications
    """
    CREATE TABLE goals (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        area TEXT NOT NULL DEFAULT 'general',        -- studio|cravvr|reselling|trading|health|general
        cadence TEXT NOT NULL DEFAULT 'daily',       -- daily|weekly|monthly|once
        target REAL,
        due_date TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE goal_checkins (
        id INTEGER PRIMARY KEY,
        goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
        day TEXT NOT NULL,
        done INTEGER NOT NULL DEFAULT 1,
        value REAL,
        note TEXT,
        UNIQUE(goal_id, day)
    );
    CREATE TABLE notifications (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        body TEXT,
        level TEXT NOT NULL DEFAULT 'info',          -- info|warn|urgent
        created_at TEXT NOT NULL,
        read INTEGER NOT NULL DEFAULT 0
    );
    """,
    # 6 — Cravvr (food-truck / business project) notes & metrics
    """
    CREATE TABLE cravvr_tasks (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'todo',         -- todo|doing|done
        priority INTEGER NOT NULL DEFAULT 3,
        due_date TEXT,
        notes TEXT,
        created_at TEXT NOT NULL,
        done_at TEXT
    );
    """,
    # 7 — small key/value store (scheduler state, settings)
    """
    CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    # 8 — a counter bumped on every data change (any process), so the dashboard can live-update
    """
    CREATE TABLE data_version (v INTEGER NOT NULL);
    INSERT INTO data_version (v) VALUES (0);
    CREATE TRIGGER dv_memories_I AFTER INSERT ON memories BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_memories_U AFTER UPDATE ON memories BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_memories_D AFTER DELETE ON memories BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_clients_I AFTER INSERT ON clients BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_clients_U AFTER UPDATE ON clients BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_clients_D AFTER DELETE ON clients BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_studio_sessions_I AFTER INSERT ON studio_sessions BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_studio_sessions_U AFTER UPDATE ON studio_sessions BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_studio_sessions_D AFTER DELETE ON studio_sessions BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_payments_I AFTER INSERT ON payments BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_payments_U AFTER UPDATE ON payments BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_payments_D AFTER DELETE ON payments BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_resale_items_I AFTER INSERT ON resale_items BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_resale_items_U AFTER UPDATE ON resale_items BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_resale_items_D AFTER DELETE ON resale_items BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_trades_I AFTER INSERT ON trades BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_trades_U AFTER UPDATE ON trades BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_trades_D AFTER DELETE ON trades BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_trading_rules_I AFTER INSERT ON trading_rules BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_trading_rules_U AFTER UPDATE ON trading_rules BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_trading_rules_D AFTER DELETE ON trading_rules BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_goals_I AFTER INSERT ON goals BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_goals_U AFTER UPDATE ON goals BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_goals_D AFTER DELETE ON goals BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_goal_checkins_I AFTER INSERT ON goal_checkins BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_goal_checkins_U AFTER UPDATE ON goal_checkins BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_goal_checkins_D AFTER DELETE ON goal_checkins BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_notifications_I AFTER INSERT ON notifications BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_notifications_U AFTER UPDATE ON notifications BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_notifications_D AFTER DELETE ON notifications BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_cravvr_tasks_I AFTER INSERT ON cravvr_tasks BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_cravvr_tasks_U AFTER UPDATE ON cravvr_tasks BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_cravvr_tasks_D AFTER DELETE ON cravvr_tasks BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_reflections_I AFTER INSERT ON reflections BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_reflections_U AFTER UPDATE ON reflections BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_reflections_D AFTER DELETE ON reflections BEGIN UPDATE data_version SET v = v + 1; END;
    """,
    # 9 — watchlist (Strat alerts) and things AMO wants to say out loud on its own
    """
    CREATE TABLE watchlist (
        id INTEGER PRIMARY KEY,
        symbol TEXT NOT NULL,
        note TEXT,
        alerts INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE announcements (
        id INTEGER PRIMARY KEY,
        text TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'info',          -- alert|reminder|brief|checkin|info
        key TEXT UNIQUE,                            -- de-duplication (same alert never twice)
        created_at TEXT NOT NULL,
        expires_at TEXT,
        spoken_at TEXT
    );
    CREATE TRIGGER dv_watchlist_i AFTER INSERT ON watchlist BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_watchlist_u AFTER UPDATE ON watchlist BEGIN UPDATE data_version SET v = v + 1; END;
    CREATE TRIGGER dv_watchlist_d AFTER DELETE ON watchlist BEGIN UPDATE data_version SET v = v + 1; END;
    """,
    # 10 — what AMO's face should show (state changes and words being spoken, with timing)
    """
    CREATE TABLE face_events (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,          -- state | say
        data TEXT NOT NULL,          -- JSON
        created_at TEXT NOT NULL
    );
    """,
]


def local_now() -> datetime:
    """Current wall-clock time in the owner's timezone (naive).

    AMO is single-user, so every timestamp is stored as local naive ISO-8601
    ("2026-10-07T14:30:00"). That keeps "today" / "this week" queries trivial.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(settings.timezone)).replace(tzinfo=None, microsecond=0)
    except Exception:  # unknown tz / missing tzdata
        return datetime.now().replace(microsecond=0)


def now_iso() -> str:
    return local_now().isoformat()


def today() -> date:
    return local_now().date()


def days_ago(n: int) -> str:
    return (local_now() - timedelta(days=n)).isoformat()


def week_start(d: date | None = None) -> date:
    d = d or today()
    return d - timedelta(days=d.weekday())


def parse_when(value: str | None) -> str:
    """Normalize a user/LLM-supplied date or datetime to ISO. Defaults to now."""
    if not value:
        return now_iso()
    v = value.strip().replace(" ", "T", 1)
    low = v.lower()
    if low in ("now",):
        return now_iso()
    if low == "today":
        return today().isoformat()
    if low == "tomorrow":
        return (today() + timedelta(days=1)).isoformat()
    if low == "yesterday":
        return (today() - timedelta(days=1)).isoformat()
    try:
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt.isoformat() if "T" in v else dt.date().isoformat()
    except ValueError:
        raise ValueError(f"could not parse date/time {value!r}; use YYYY-MM-DD or YYYY-MM-DD HH:MM")


class Database:
    """Thread-safe-enough wrapper: one connection per thread, WAL mode."""

    def __init__(self, path: str | None = None):
        self.path = path or settings.db_path
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        # Keep a shared connection for :memory: so all threads see the same DB.
        self._shared: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self.migrate()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self.path == ":memory:":
            if self._shared is None:
                self._shared = self._connect()
            return self._shared
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self.conn
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def migrate(self) -> None:
        with self.tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = c.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = row["v"] or 0
            for i, sql in enumerate(MIGRATIONS[current:], start=current + 1):
                c.executescript(sql)
                c.execute("INSERT INTO schema_version(version) VALUES (?)", (i,))

    # --- helpers -----------------------------------------------------------
    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.tx() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid if cur.lastrowid else cur.rowcount

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
            return row[0] if row else None

    def insert(self, table: str, data: dict[str, Any]) -> int:
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        with self.tx() as c:
            cur = c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))
            return int(cur.lastrowid)

    def update(self, table: str, row_id: int, data: dict[str, Any]) -> bool:
        if not data:
            return False
        sets = ", ".join(f"{k} = ?" for k in data)
        with self.tx() as c:
            cur = c.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*data.values(), row_id))
            return cur.rowcount > 0

    def data_version(self) -> int:
        """Increases whenever any business data changes (from any process)."""
        return int(self.scalar("SELECT v FROM data_version"))

    def get_kv(self, key: str, default: str | None = None) -> str | None:
        v = self.scalar("SELECT value FROM kv WHERE key = ?", (key,))
        return default if v is None else v

    def set_kv(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


def set_db(db: Database) -> None:
    """Swap the global database (used by tests)."""
    global _db
    _db = db
