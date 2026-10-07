"""Goals and daily check-ins (streaks, progress)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .db import Database, get_db, now_iso, parse_when, today, week_start

GOAL_FIELDS = {"title", "area", "cadence", "target", "due_date", "active"}


class Goals:
    def __init__(self, db: Database | None = None):
        self.db = db or get_db()

    def add(self, title: str, area: str = "general", cadence: str = "daily",
            target: float | None = None, due_date: str | None = None) -> dict[str, Any]:
        gid = self.db.insert(
            "goals",
            {"title": title.strip(), "area": area, "cadence": cadence, "target": target,
             "due_date": parse_when(due_date)[:10] if due_date else None, "created_at": now_iso()},
        )
        return self.get(gid)

    def get(self, goal_id: int) -> dict[str, Any] | None:
        g = self.db.one("SELECT * FROM goals WHERE id = ?", (goal_id,))
        if g:
            g["streak"] = self.streak(goal_id)
            g["done_today"] = bool(self.db.scalar(
                "SELECT done FROM goal_checkins WHERE goal_id = ? AND day = ?",
                (goal_id, today().isoformat())))
            g["progress"] = self.progress(g)
        return g

    def update(self, goal_id: int, **fields: Any) -> dict[str, Any] | None:
        self.db.update("goals", goal_id, {k: v for k, v in fields.items() if k in GOAL_FIELDS})
        return self.get(goal_id)

    def resolve(self, goal: int | str) -> dict[str, Any]:
        if isinstance(goal, int) or str(goal).isdigit():
            g = self.get(int(goal))
            if not g:
                raise ValueError(f"no goal with id {goal}")
            return g
        rows = self.db.all("SELECT id, title FROM goals WHERE active = 1 AND title LIKE ?",
                           (f"%{goal}%",))
        if len(rows) == 1:
            return self.get(rows[0]["id"])
        if not rows:
            raise ValueError(f"no active goal matching {goal!r}")
        raise ValueError(f"{goal!r} is ambiguous: " + ", ".join(f"{r['title']} (#{r['id']})" for r in rows))

    def check_in(self, goal: int | str, done: bool = True, value: float | None = None,
                 note: str | None = None, day: str | None = None) -> dict[str, Any]:
        g = self.resolve(goal)
        d = parse_when(day or "today")[:10]
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO goal_checkins (goal_id, day, done, value, note) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(goal_id, day) DO UPDATE SET done = excluded.done, "
                "value = COALESCE(excluded.value, value), note = COALESCE(excluded.note, note)",
                (g["id"], d, int(done), value, note),
            )
        return self.get(g["id"])

    def streak(self, goal_id: int) -> int:
        days = {r["day"] for r in self.db.all(
            "SELECT day FROM goal_checkins WHERE goal_id = ? AND done = 1", (goal_id,))}
        d = today()
        if d.isoformat() not in days:  # today not done yet doesn't break the streak
            d -= timedelta(days=1)
        n = 0
        while d.isoformat() in days:
            n += 1
            d -= timedelta(days=1)
        return n

    def progress(self, g: dict[str, Any]) -> dict[str, Any] | None:
        """For goals with a numeric target, sum check-in values over the current period."""
        if g.get("target") is None:
            return None
        start: date
        t = today()
        if g["cadence"] == "weekly":
            start = week_start(t)
        elif g["cadence"] == "monthly":
            start = t.replace(day=1)
        elif g["cadence"] == "daily":
            start = t
        else:
            start = date(1970, 1, 1)
        total = self.db.scalar(
            "SELECT COALESCE(SUM(value), 0) FROM goal_checkins WHERE goal_id = ? AND day >= ?",
            (g["id"], start.isoformat()),
        )
        return {"value": total, "target": g["target"],
                "pct": round(min(1.0, total / g["target"]), 3) if g["target"] else None}

    def active(self, area: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id FROM goals WHERE active = 1"
        params: tuple = ()
        if area:
            sql += " AND area = ?"
            params = (area,)
        return [self.get(r["id"]) for r in self.db.all(sql + " ORDER BY area, id", params)]

    def today_list(self) -> list[dict[str, Any]]:
        return [g for g in self.active() if g["cadence"] == "daily"]
