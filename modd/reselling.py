"""Reselling tracker: inventory, listings, sales and profit."""

from __future__ import annotations

from typing import Any

from .db import Database, days_ago, get_db, parse_when

ITEM_FIELDS = {
    "name", "sku", "category", "source", "cost", "list_price", "sold_price", "fees", "shipping",
    "platform", "status", "bought_at", "listed_at", "sold_at", "notes",
}

PROFIT_SQL = "(COALESCE(sold_price, 0) - cost - fees - shipping)"


class Reselling:
    def __init__(self, db: Database | None = None):
        self.db = db or get_db()

    def add_item(self, name: str, cost: float = 0, **fields: Any) -> dict[str, Any]:
        data = {k: v for k, v in fields.items() if k in ITEM_FIELDS and v is not None}
        data["bought_at"] = parse_when(data.get("bought_at"))
        if data.get("list_price") and "status" not in data:
            data["status"] = "listed"
            data.setdefault("listed_at", data["bought_at"])
        iid = self.db.insert("resale_items", {"name": name.strip(), "cost": float(cost), **data})
        return self.get(iid)

    def get(self, item_id: int) -> dict[str, Any] | None:
        return self.db.one(
            f"SELECT *, CASE WHEN status = 'sold' THEN {PROFIT_SQL} END AS profit "
            "FROM resale_items WHERE id = ?",
            (item_id,),
        )

    def find(self, query: str) -> list[dict[str, Any]]:
        like = f"%{query.strip()}%"
        return self.db.all(
            "SELECT * FROM resale_items WHERE name LIKE ? OR sku LIKE ? ORDER BY bought_at DESC",
            (like, like),
        )

    def resolve(self, item: int | str) -> dict[str, Any]:
        if isinstance(item, int) or str(item).isdigit():
            row = self.get(int(item))
            if not row:
                raise ValueError(f"no item with id {item}")
            return row
        matches = [m for m in self.find(str(item)) if m["status"] != "sold"] or self.find(str(item))
        if len(matches) == 1:
            return self.get(matches[0]["id"])
        if not matches:
            raise ValueError(f"no item matching {item!r}")
        names = ", ".join(f"{m['name']} (#{m['id']}, {m['status']})" for m in matches[:5])
        raise ValueError(f"{item!r} is ambiguous: {names}")

    def list_item(self, item: int | str, price: float, platform: str | None = None):
        it = self.resolve(item)
        self.db.update(
            "resale_items",
            it["id"],
            {"list_price": float(price), "platform": platform or it["platform"], "status": "listed",
             "listed_at": it["listed_at"] or parse_when(None)},
        )
        return self.get(it["id"])

    def mark_sold(
        self,
        item: int | str,
        sold_price: float,
        platform: str | None = None,
        fees: float | None = None,
        shipping: float | None = None,
        sold_at: str | None = None,
    ) -> dict[str, Any]:
        it = self.resolve(item)
        data: dict[str, Any] = {
            "sold_price": float(sold_price),
            "status": "sold",
            "sold_at": parse_when(sold_at),
        }
        if platform:
            data["platform"] = platform
        if fees is not None:
            data["fees"] = float(fees)
        if shipping is not None:
            data["shipping"] = float(shipping)
        self.db.update("resale_items", it["id"], data)
        return self.get(it["id"])

    def update(self, item_id: int, **fields: Any) -> dict[str, Any] | None:
        data = {k: v for k, v in fields.items() if k in ITEM_FIELDS and v is not None}
        self.db.update("resale_items", item_id, data)
        return self.get(item_id)

    def inventory(self, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            return self.db.all(
                "SELECT * FROM resale_items WHERE status = ? ORDER BY bought_at", (status,)
            )
        return self.db.all(
            "SELECT * FROM resale_items WHERE status IN ('inventory', 'listed') ORDER BY bought_at"
        )

    def stale(self, days: int = 30) -> list[dict[str, Any]]:
        """Unsold items sitting longer than `days` — candidates for a price drop."""
        return self.db.all(
            "SELECT * FROM resale_items WHERE status IN ('inventory', 'listed') AND bought_at < ? "
            "ORDER BY bought_at",
            (days_ago(days),),
        )

    def summary(self, since: str | None = None) -> dict[str, Any]:
        since = since or "0000"
        sold = self.db.one(
            f"SELECT COUNT(*) AS sold, COALESCE(SUM(sold_price), 0) AS revenue, "
            f"COALESCE(SUM({PROFIT_SQL}), 0) AS profit, "
            f"AVG(julianday(sold_at) - julianday(bought_at)) AS avg_days_to_sell "
            f"FROM resale_items WHERE status = 'sold' AND sold_at >= ?",
            (since,),
        )
        inv = self.db.one(
            "SELECT COUNT(*) AS items, COALESCE(SUM(cost), 0) AS capital_tied_up, "
            "COALESCE(SUM(list_price), 0) AS listed_value "
            "FROM resale_items WHERE status IN ('inventory', 'listed')"
        )
        by_platform = self.db.all(
            f"SELECT COALESCE(platform, 'unknown') AS platform, COUNT(*) AS sold, "
            f"ROUND(SUM({PROFIT_SQL}), 2) AS profit FROM resale_items "
            f"WHERE status = 'sold' AND sold_at >= ? GROUP BY platform ORDER BY profit DESC",
            (since,),
        )
        roi = (sold["profit"] / (sold["revenue"] - sold["profit"])) if sold["revenue"] - sold["profit"] else None
        return {
            **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in sold.items()},
            "roi": round(roi, 3) if roi is not None else None,
            "inventory": {k: round(v, 2) if isinstance(v, float) else v for k, v in inv.items()},
            "by_platform": by_platform,
        }
