"""Studio CRM: clients, bookings (studio sessions) and payments."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .db import Database, get_db, now_iso, parse_when, today

CLIENT_FIELDS = {"name", "artist_name", "phone", "email", "instagram", "notes", "status"}
SESSION_FIELDS = {"starts_at", "hours", "service", "rate", "status", "notes"}


class StudioCRM:
    def __init__(self, db: Database | None = None):
        self.db = db or get_db()

    # --- clients -----------------------------------------------------------
    def add_client(self, name: str, **fields: Any) -> dict[str, Any]:
        data = {k: v for k, v in fields.items() if k in CLIENT_FIELDS and v is not None}
        cid = self.db.insert("clients", {"name": name.strip(), **data, "created_at": now_iso()})
        return self.get_client(cid)

    def update_client(self, client_id: int, **fields: Any) -> dict[str, Any] | None:
        data = {k: v for k, v in fields.items() if k in CLIENT_FIELDS and v is not None}
        self.db.update("clients", client_id, data)
        return self.get_client(client_id)

    def get_client(self, client_id: int) -> dict[str, Any] | None:
        c = self.db.one("SELECT * FROM clients WHERE id = ?", (client_id,))
        if not c:
            return None
        c["total_paid"] = self.db.scalar(
            "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE client_id = ?", (client_id,)
        )
        c["session_count"] = self.db.scalar(
            "SELECT COUNT(*) FROM studio_sessions WHERE client_id = ? AND status = 'completed'",
            (client_id,),
        )
        c["last_session"] = self.db.scalar(
            "SELECT MAX(starts_at) FROM studio_sessions WHERE client_id = ? AND status = 'completed'",
            (client_id,),
        )
        c["balance_due"] = round(self._billed(client_id) - c["total_paid"], 2)
        return c

    def _billed(self, client_id: int) -> float:
        return float(
            self.db.scalar(
                "SELECT COALESCE(SUM(hours * rate), 0) FROM studio_sessions "
                "WHERE client_id = ? AND status = 'completed'",
                (client_id,),
            )
        )

    def find_client(self, query: str) -> list[dict[str, Any]]:
        like = f"%{query.strip()}%"
        return self.db.all(
            "SELECT * FROM clients WHERE name LIKE ? OR artist_name LIKE ? OR instagram LIKE ? "
            "OR phone LIKE ? OR email LIKE ? ORDER BY name",
            (like, like, like, like, like),
        )

    def resolve_client(self, client: int | str) -> dict[str, Any]:
        """Accept an id or a (partial) name; raise if ambiguous or missing."""
        if isinstance(client, int) or (isinstance(client, str) and client.isdigit()):
            c = self.get_client(int(client))
            if not c:
                raise ValueError(f"no client with id {client}")
            return c
        matches = self.find_client(str(client))
        exact = [m for m in matches if m["name"].lower() == str(client).lower()
                 or (m["artist_name"] or "").lower() == str(client).lower()]
        if len(exact) == 1:
            return self.get_client(exact[0]["id"])
        if len(matches) == 1:
            return self.get_client(matches[0]["id"])
        if not matches:
            raise ValueError(f"no client matching {client!r}")
        names = ", ".join(f"{m['name']} (#{m['id']})" for m in matches[:5])
        raise ValueError(f"{client!r} is ambiguous: {names}")

    def list_clients(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self.db.all("SELECT * FROM clients WHERE status = ? ORDER BY name", (status,))
        return self.db.all("SELECT * FROM clients ORDER BY name")

    def inactive_clients(self, days: int = 45) -> list[dict[str, Any]]:
        """Active clients whose last completed session is older than `days` — follow-up targets."""
        cutoff = (today() - timedelta(days=days)).isoformat()
        return self.db.all(
            "SELECT c.id, c.name, c.artist_name, c.phone, c.instagram, MAX(s.starts_at) AS last_session "
            "FROM clients c JOIN studio_sessions s ON s.client_id = c.id AND s.status = 'completed' "
            "WHERE c.status = 'active' GROUP BY c.id HAVING last_session < ? ORDER BY last_session",
            (cutoff,),
        )

    # --- sessions ----------------------------------------------------------
    def book_session(
        self,
        client: int | str,
        starts_at: str,
        hours: float = 2,
        service: str = "recording",
        rate: float = 0,
        notes: str | None = None,
    ) -> dict[str, Any]:
        c = self.resolve_client(client)
        when = parse_when(starts_at)
        sid = self.db.insert(
            "studio_sessions",
            {
                "client_id": c["id"],
                "starts_at": when,
                "hours": float(hours),
                "service": service,
                "rate": float(rate),
                "notes": notes,
                "created_at": now_iso(),
            },
        )
        return self.get_session(sid)

    def get_session(self, session_id: int) -> dict[str, Any] | None:
        return self.db.one(
            "SELECT s.*, c.name AS client_name, c.artist_name, c.phone, (s.hours * s.rate) AS total "
            "FROM studio_sessions s JOIN clients c ON c.id = s.client_id WHERE s.id = ?",
            (session_id,),
        )

    def update_session(self, session_id: int, **fields: Any) -> dict[str, Any] | None:
        data = {k: v for k, v in fields.items() if k in SESSION_FIELDS and v is not None}
        if "starts_at" in data:
            data["starts_at"] = parse_when(data["starts_at"])
        self.db.update("studio_sessions", session_id, data)
        return self.get_session(session_id)

    def complete_session(self, session_id: int, paid: float | None = None, method: str | None = None):
        s = self.update_session(session_id, status="completed")
        if s and paid:
            self.record_payment(paid, client=s["client_id"], session_id=session_id, method=method)
        return self.get_session(session_id)

    def sessions_between(self, start: str, end: str, include_cancelled: bool = False):
        sql = (
            "SELECT s.*, c.name AS client_name, c.artist_name, (s.hours * s.rate) AS total "
            "FROM studio_sessions s JOIN clients c ON c.id = s.client_id "
            "WHERE s.starts_at >= ? AND s.starts_at < ?"
        )
        if not include_cancelled:
            sql += " AND s.status NOT IN ('cancelled')"
        return self.db.all(sql + " ORDER BY s.starts_at", (start, end))

    def upcoming(self, days: int = 7) -> list[dict[str, Any]]:
        start = today()
        return [
            s
            for s in self.sessions_between(start.isoformat(), (start + timedelta(days=days + 1)).isoformat())
            if s["status"] == "booked"
        ]

    def schedule_for(self, day: str | None = None) -> list[dict[str, Any]]:
        d = parse_when(day or "today")[:10]
        from datetime import date

        nxt = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
        return self.sessions_between(d, nxt)

    # --- payments ----------------------------------------------------------
    def record_payment(
        self,
        amount: float,
        client: int | str | None = None,
        session_id: int | None = None,
        method: str | None = None,
        note: str | None = None,
        business: str = "studio",
        paid_at: str | None = None,
    ) -> dict[str, Any]:
        client_id = self.resolve_client(client)["id"] if client not in (None, "") else None
        pid = self.db.insert(
            "payments",
            {
                "client_id": client_id,
                "session_id": session_id,
                "amount": float(amount),
                "method": method,
                "business": business,
                "note": note,
                "paid_at": parse_when(paid_at),
            },
        )
        return self.db.one("SELECT * FROM payments WHERE id = ?", (pid,))

    def unpaid_sessions(self) -> list[dict[str, Any]]:
        return self.db.all(
            "SELECT s.id, s.starts_at, c.name AS client_name, (s.hours * s.rate) AS total, "
            "COALESCE((SELECT SUM(amount) FROM payments p WHERE p.session_id = s.id), 0) AS paid "
            "FROM studio_sessions s JOIN clients c ON c.id = s.client_id "
            "WHERE s.status = 'completed' AND s.rate > 0 "
            "AND (s.hours * s.rate) > COALESCE((SELECT SUM(amount) FROM payments p WHERE p.session_id = s.id), 0) "
            "ORDER BY s.starts_at"
        )
