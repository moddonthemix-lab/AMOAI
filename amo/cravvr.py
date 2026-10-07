"""Cravvr: lightweight task board for the Cravvr project.

Revenue for Cravvr goes through `payments` with business='cravvr', so it shows
up in revenue tracking alongside the studio.
"""

from __future__ import annotations

from typing import Any

from .db import Database, get_db, now_iso, parse_when


class Cravvr:
    def __init__(self, db: Database | None = None):
        self.db = db or get_db()

    def add_task(self, title: str, priority: int = 3, due_date: str | None = None,
                 notes: str | None = None) -> dict[str, Any]:
        tid = self.db.insert("cravvr_tasks", {
            "title": title.strip(), "priority": max(1, min(5, int(priority))),
            "due_date": parse_when(due_date)[:10] if due_date else None, "notes": notes,
            "created_at": now_iso(),
        })
        return self.db.one("SELECT * FROM cravvr_tasks WHERE id = ?", (tid,))

    def set_status(self, task_id: int, status: str) -> dict[str, Any] | None:
        if status not in ("todo", "doing", "done"):
            raise ValueError("status must be todo, doing or done")
        self.db.update("cravvr_tasks", task_id, {
            "status": status, "done_at": now_iso() if status == "done" else None})
        return self.db.one("SELECT * FROM cravvr_tasks WHERE id = ?", (task_id,))

    def open_tasks(self) -> list[dict[str, Any]]:
        return self.db.all(
            "SELECT * FROM cravvr_tasks WHERE status != 'done' "
            "ORDER BY priority DESC, COALESCE(due_date, '9999'), id")
