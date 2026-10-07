"""Revenue tracking across every business, plus the compact dashboard snapshot
used by the ESP32 touchscreen and the daily brief."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .config import settings
from .crm import StudioCRM
from .cravvr import Cravvr
from .db import Database, get_db, today, week_start
from .goals import Goals
from .reselling import PROFIT_SQL, Reselling
from .trading import TradingJournal


def period_start(period: str, ref: date | None = None) -> date:
    ref = ref or today()
    if period == "today":
        return ref
    if period == "week":
        return week_start(ref)
    if period == "month":
        return ref.replace(day=1)
    if period == "year":
        return ref.replace(month=1, day=1)
    if period == "all":
        return date(1970, 1, 1)
    raise ValueError("period must be today, week, month, year or all")


class Finance:
    def __init__(self, db: Database | None = None):
        self.db = db or get_db()

    def revenue(self, period: str = "month") -> dict[str, Any]:
        """Money in, by business. Reselling counts gross profit; trading counts net P&L."""
        start = period_start(period).isoformat()
        by_business = {
            r["business"]: round(r["total"], 2)
            for r in self.db.all(
                "SELECT business, SUM(amount) AS total FROM payments WHERE paid_at >= ? GROUP BY business",
                (start,),
            )
        }
        resale_profit = self.db.scalar(
            f"SELECT COALESCE(SUM({PROFIT_SQL}), 0) FROM resale_items WHERE status = 'sold' AND sold_at >= ?",
            (start,),
        )
        trading = TradingJournal(self.db).stats(since=start)["net_pnl"]
        by_business["reselling"] = round(by_business.get("reselling", 0) + resale_profit, 2)
        by_business["trading"] = round(by_business.get("trading", 0) + trading, 2)
        by_business.setdefault("studio", 0.0)
        by_business.setdefault("cravvr", 0.0)
        total = round(sum(by_business.values()), 2)
        out: dict[str, Any] = {"period": period, "since": start, "total": total, "by_business": by_business}
        if period == "month" and settings.monthly_revenue_target:
            out["target"] = settings.monthly_revenue_target
            out["target_pct"] = round(total / settings.monthly_revenue_target, 3)
        return out

    def daily_series(self, days: int = 14) -> list[dict[str, Any]]:
        """Per-day totals for a sparkline."""
        start = today() - timedelta(days=days - 1)
        series = {(start + timedelta(days=i)).isoformat(): 0.0 for i in range(days)}
        for r in self.db.all(
            "SELECT substr(paid_at, 1, 10) AS d, SUM(amount) AS t FROM payments WHERE paid_at >= ? GROUP BY d",
            (start.isoformat(),),
        ):
            if r["d"] in series:
                series[r["d"]] += r["t"]
        for r in self.db.all(
            f"SELECT substr(sold_at, 1, 10) AS d, SUM({PROFIT_SQL}) AS t FROM resale_items "
            f"WHERE status = 'sold' AND sold_at >= ? GROUP BY d",
            (start.isoformat(),),
        ):
            if r["d"] in series:
                series[r["d"]] += r["t"]
        return [{"day": d, "total": round(v, 2)} for d, v in series.items()]


def dashboard(db: Database | None = None) -> dict[str, Any]:
    """Everything the studio dashboard / ESP32 screen needs in one call. Keep it small:
    the ESP32 parses it with ArduinoJson on a few KB of RAM."""
    db = db or get_db()
    crm, goals, fin = StudioCRM(db), Goals(db), Finance(db)
    trading, resale = TradingJournal(db), Reselling(db)
    t = today()
    sessions_today = crm.schedule_for(t.isoformat())
    month = fin.revenue("month")
    week = fin.revenue("week")
    unread = db.all(
        "SELECT id, title, body, level, created_at FROM notifications WHERE read = 0 "
        "ORDER BY id DESC LIMIT 5"
    )
    return {
        "date": t.isoformat(),
        "weekday": t.strftime("%A"),
        "studio": {
            "today": [
                {"id": s["id"], "time": s["starts_at"][11:16] or "--:--", "client": s["client_name"],
                 "service": s["service"], "hours": s["hours"], "status": s["status"]}
                for s in sessions_today
            ],
            "upcoming_7d": len(crm.upcoming(7)),
            "unpaid": len(crm.unpaid_sessions()),
        },
        "goals": [
            {"id": g["id"], "title": g["title"], "done": g["done_today"], "streak": g["streak"],
             "cadence": g["cadence"], "area": g["area"]}
            for g in goals.active()
        ],
        "revenue": {
            "today": fin.revenue("today")["total"],
            "week": week["total"],
            "month": month["total"],
            "month_target": month.get("target"),
            "month_pct": month.get("target_pct"),
            "by_business": month["by_business"],
            "spark": [d["total"] for d in fin.daily_series(14)],
        },
        "trading": {
            "today_pnl": trading.todays_pnl(),
            "open_positions": len(trading.open_positions()),
            "week": trading.stats(since=week_start(t).isoformat())["net_pnl"],
        },
        "reselling": {
            "inventory": resale.summary()["inventory"]["items"],
            "stale": len(resale.stale(30)),
        },
        "cravvr": {"open_tasks": len(Cravvr(db).open_tasks())},
        "notifications": unread,
    }
