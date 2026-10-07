"""Trading journal: trades, R-multiples, rule adherence and your personal trading rules."""

from __future__ import annotations

from typing import Any

from .db import Database, get_db, now_iso, parse_when, today

TRADE_FIELDS = {
    "symbol", "side", "setup", "entry", "exit", "stop", "target", "quantity", "fees",
    "opened_at", "closed_at", "followed_rules", "emotion", "notes",
}


def _pnl(t: dict[str, Any]) -> float | None:
    if t.get("exit") is None:
        return None
    direction = 1 if t["side"] == "long" else -1
    return round((t["exit"] - t["entry"]) * direction * t["quantity"] - (t["fees"] or 0), 2)


def _r_multiple(t: dict[str, Any]) -> float | None:
    if t.get("exit") is None or t.get("stop") is None:
        return None
    risk = abs(t["entry"] - t["stop"])
    if not risk:
        return None
    direction = 1 if t["side"] == "long" else -1
    return round((t["exit"] - t["entry"]) * direction / risk, 2)


class TradingJournal:
    def __init__(self, db: Database | None = None):
        self.db = db or get_db()

    def _enrich(self, t: dict[str, Any] | None) -> dict[str, Any] | None:
        if t:
            t["pnl"] = _pnl(t)
            t["r_multiple"] = _r_multiple(t)
        return t

    def open_trade(
        self, symbol: str, side: str, entry: float, quantity: float = 1, **fields: Any
    ) -> dict[str, Any]:
        side = side.lower()
        if side in ("buy", "call"):
            side = "long"
        elif side in ("sell", "put"):
            side = "short"
        if side not in ("long", "short"):
            raise ValueError("side must be long or short")
        data = {k: v for k, v in fields.items() if k in TRADE_FIELDS and v is not None}
        data["opened_at"] = parse_when(data.get("opened_at"))
        if "closed_at" in data:
            data["closed_at"] = parse_when(data["closed_at"])
        if isinstance(data.get("followed_rules"), bool):
            data["followed_rules"] = int(data["followed_rules"])
        tid = self.db.insert(
            "trades",
            {"symbol": symbol.upper().strip(), "side": side, "entry": float(entry),
             "quantity": float(quantity), **data},
        )
        return self.get(tid)

    def close_trade(
        self,
        trade_id: int,
        exit: float,
        followed_rules: bool | None = None,
        emotion: str | None = None,
        notes: str | None = None,
        fees: float | None = None,
        closed_at: str | None = None,
    ) -> dict[str, Any]:
        t = self.get(trade_id)
        if not t:
            raise ValueError(f"no trade with id {trade_id}")
        data: dict[str, Any] = {"exit": float(exit), "closed_at": parse_when(closed_at)}
        if followed_rules is not None:
            data["followed_rules"] = int(bool(followed_rules))
        if emotion:
            data["emotion"] = emotion
        if notes:
            data["notes"] = ((t["notes"] + "\n") if t["notes"] else "") + notes
        if fees is not None:
            data["fees"] = float(fees)
        self.db.update("trades", trade_id, data)
        return self.get(trade_id)

    def get(self, trade_id: int) -> dict[str, Any] | None:
        return self._enrich(self.db.one("SELECT * FROM trades WHERE id = ?", (trade_id,)))

    def open_positions(self) -> list[dict[str, Any]]:
        return [self._enrich(t) for t in self.db.all(
            "SELECT * FROM trades WHERE exit IS NULL ORDER BY opened_at")]

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return [self._enrich(t) for t in self.db.all(
            "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,))]

    def closed_since(self, since: str) -> list[dict[str, Any]]:
        return [self._enrich(t) for t in self.db.all(
            "SELECT * FROM trades WHERE exit IS NOT NULL AND closed_at >= ? ORDER BY closed_at",
            (since,))]

    def stats(self, since: str | None = None) -> dict[str, Any]:
        trades = self.closed_since(since or "0000")
        pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
        rule_known = [t for t in trades if t["followed_rules"] is not None]
        broke = [t for t in rule_known if not t["followed_rules"]]

        by_setup: dict[str, dict[str, float]] = {}
        for t in trades:
            key = t["setup"] or "untagged"
            s = by_setup.setdefault(key, {"trades": 0, "pnl": 0.0, "wins": 0})
            s["trades"] += 1
            s["pnl"] = round(s["pnl"] + (t["pnl"] or 0), 2)
            s["wins"] += 1 if (t["pnl"] or 0) > 0 else 0

        return {
            "trades": len(pnls),
            "net_pnl": round(sum(pnls), 2),
            "win_rate": round(len(wins) / len(pnls), 3) if pnls else None,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) else None,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
            "rule_adherence": round(1 - len(broke) / len(rule_known), 3) if rule_known else None,
            "pnl_when_rules_broken": round(sum(t["pnl"] or 0 for t in broke), 2),
            "by_setup": by_setup,
        }

    def todays_pnl(self) -> float:
        return round(sum(t["pnl"] or 0 for t in self.closed_since(today().isoformat())), 2)

    # --- rules -------------------------------------------------------------
    def add_rule(self, rule: str) -> dict[str, Any]:
        rid = self.db.insert("trading_rules", {"rule": rule.strip(), "created_at": now_iso()})
        return self.db.one("SELECT * FROM trading_rules WHERE id = ?", (rid,))

    def rules(self) -> list[dict[str, Any]]:
        return self.db.all("SELECT * FROM trading_rules WHERE active = 1 ORDER BY id")

    def remove_rule(self, rule_id: int) -> bool:
        return self.db.update("trading_rules", rule_id, {"active": 0})
