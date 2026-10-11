"""Speed report: every request is timed, so tuning is based on numbers, not guesses.

    amo stats            where the time goes (last 7 days), slowest requests, what to do about it

Recorded per request: how long until the first word (text) or first sound (voice), the total,
and the parts: speech-to-text, the AI model (and its first word), tools (database, market data,
web), and making the first sentence's audio. Also which path answered: instant (no AI), fastpath
(a direct action, no AI), model, or coder.
"""

from __future__ import annotations

import statistics
import time
from datetime import timedelta
from typing import Any

from .db import Database, get_db, local_now, now_iso


class Turn:
    """Timing for one request. Marks are milliseconds since the request started."""

    def __init__(self, channel: str = "webui", request: str = "", started: float | None = None):
        self.t0 = started or time.monotonic()
        self.channel, self.request = channel, (request or "")[:200]
        self.marks: dict[str, int] = {}
        self.sums: dict[str, int] = {}
        self.info: dict[str, Any] = {"model_calls": 0, "tools": [], "ok": True}
        self.saved = False

    def ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def mark(self, name: str) -> None:
        """First time only (e.g. 'first_text', 'first_audio', 'first_token')."""
        self.marks.setdefault(name, self.ms())

    def add(self, name: str, ms: float) -> None:
        self.sums[name] = self.sums.get(name, 0) + int(ms)

    def timer(self, name: str) -> "_Span":
        return _Span(self, name)

    def save(self, db: Database | None = None, path: str | None = None, model: str | None = None) -> None:
        if self.saved:
            return
        self.saved = True
        total = self.ms()
        first = self.marks.get("first_audio") if self.channel == "voice" else None
        first = first if first is not None else self.marks.get("first_text", total)
        try:
            (db or get_db()).insert("timings", {
                "at": now_iso(), "channel": self.channel, "request": self.request,
                "path": path or self.info.get("path") or "model", "model": model or self.info.get("model"),
                "first_ms": first, "total_ms": total, "stt_ms": self.sums.get("stt"),
                "model_ms": self.sums.get("model"), "first_token_ms": self.marks.get("first_token"),
                "tool_ms": self.sums.get("tool"), "tts_ms": self.sums.get("tts"),
                "model_calls": self.info["model_calls"], "tools": ",".join(self.info["tools"])[:300],
                "ok": 1 if self.info.get("ok", True) else 0})
        except Exception:  # noqa: BLE001 — timing must never break a reply
            pass


class _Span:
    def __init__(self, turn: Turn, name: str):
        self.turn, self.name = turn, name

    def __enter__(self) -> "_Span":
        self.t = time.monotonic()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.turn.add(self.name, (time.monotonic() - self.t) * 1000)


# ------------------------------------------------------------------ the report
def _pct(values: list[int], p: float) -> int | None:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


def _s(ms: int | None) -> str:
    return "—" if ms is None else (f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms")


def report(db: Database | None = None, days: int = 7) -> dict[str, Any]:
    db = db or get_db()
    since = (local_now() - timedelta(days=days)).isoformat()
    rows = db.all("SELECT * FROM timings WHERE at >= ? ORDER BY id", (since,))
    out: dict[str, Any] = {"count": len(rows), "days": days, "by_path": {}, "slowest": [], "advice": []}
    if not rows:
        return out
    for path in ("instant", "fastpath", "model", "coder"):
        rs = [r for r in rows if r["path"] == path]
        if rs:
            out["by_path"][path] = {"count": len(rs), "first_median": _pct([r["first_ms"] for r in rs], 0.5),
                                    "first_p90": _pct([r["first_ms"] for r in rs], 0.9),
                                    "total_median": _pct([r["total_ms"] for r in rs], 0.5)}
    model_rows = [r for r in rows if r["path"] in ("model", "coder")]
    voice = [r for r in rows if r["channel"] == "voice"]
    out["first_median"] = _pct([r["first_ms"] for r in rows], 0.5)
    out["parts"] = {
        "stt_median": _pct([r["stt_ms"] for r in voice], 0.5),
        "first_token_median": _pct([r["first_token_ms"] for r in model_rows], 0.5),
        "model_median": _pct([r["model_ms"] for r in model_rows], 0.5),
        "tool_median": _pct([r["tool_ms"] for r in rows if r["tool_ms"]], 0.5),
        "tts_median": _pct([r["tts_ms"] for r in voice if r["tts_ms"]], 0.5),
    }
    out["share_instant"] = round(sum(1 for r in rows if r["path"] in ("instant", "fastpath")) / len(rows), 2)
    out["slowest"] = [{"request": r["request"], "path": r["path"], "first": r["first_ms"], "total": r["total_ms"],
                       "model": r["model_ms"], "tool": r["tool_ms"], "stt": r["stt_ms"], "tools": r["tools"]}
                      for r in sorted(rows, key=lambda r: -(r["total_ms"] or 0))[:5]]
    out["advice"] = advice(out, model_rows)
    return out


def advice(rep: dict[str, Any], model_rows: list[dict[str, Any]]) -> list[str]:
    """What to do about it, in plain words."""
    tips = []
    p = rep["parts"]
    if p["first_token_median"] and p["first_token_median"] > 4000:
        tips.append(f"The AI model takes {_s(p['first_token_median'])} to start answering. Try a smaller/faster "
                    "model (`amo eval --models …` compares them on your own requests), or keep fewer tools in play.")
    calls = [r["model_calls"] for r in model_rows if r["model_calls"]]
    if calls and statistics.mean(calls) > 1.6:
        tips.append("Most AI answers need 2+ model rounds (a tool, then the answer). More instant shortcuts for your "
                    "common requests would skip the second round — see `amo mistakes` / the slowest list.")
    if p["stt_median"] and p["stt_median"] > 1500:
        tips.append(f"Speech-to-text takes {_s(p['stt_median'])}. A smaller Whisper model "
                    "(AMO_WHISPER_MODEL=base.en → tiny.en) or transcribing while you talk would cut it.")
    if p["tool_median"] and p["tool_median"] > 2500:
        tips.append(f"Tools take {_s(p['tool_median'])} (usually market data or web search) — those are network-bound.")
    if p["tts_median"] and p["tts_median"] > 1200:
        tips.append(f"Making the first sentence's audio takes {_s(p['tts_median'])}; shorter first sentences help.")
    if rep.get("share_instant", 0) < 0.3 and rep["count"] >= 20:
        tips.append("Under a third of your requests are instant. Your most common phrasings could get shortcuts.")
    return tips or ["Nothing stands out — AMO is about as fast as this hardware allows."]


def report_text(db: Database | None = None, days: int = 7) -> str:
    r = report(db, days)
    if not r["count"]:
        return "No requests timed yet — talk to AMO a bit, then run this again."
    lines = [f"Speed report — last {days} days, {r['count']} requests "
             f"({round(r['share_instant'] * 100)}% instant, no AI needed)",
             f"Median time to first word/sound: {_s(r['first_median'])}", ""]
    names = {"instant": "Instant answers", "fastpath": "Direct actions", "model": "AI answers", "coder": "Coding"}
    for path, s in r["by_path"].items():
        lines.append(f"  {names[path]:<16} {s['count']:>4}×   first {_s(s['first_median'])} "
                     f"(slow ones {_s(s['first_p90'])})   total {_s(s['total_median'])}")
    p = r["parts"]
    lines += ["", "Where the time goes (medians):",
              f"  speech→text {_s(p['stt_median'])} · AI first word {_s(p['first_token_median'])} · "
              f"AI total {_s(p['model_median'])} · tools {_s(p['tool_median'])} · first audio {_s(p['tts_median'])}",
              "", "Slowest:"]
    for s in r["slowest"]:
        lines.append(f"  {_s(s['total']):>6}  “{s['request'][:60]}”  ({s['path']}"
                     + (f", AI {_s(s['model'])}" if s["model"] else "") + (f", tools {_s(s['tool'])}" if s["tool"] else "")
                     + (f": {s['tools']}" if s["tools"] else "") + ")")
    lines += ["", "What would help:"] + [f"  • {t}" for t in r["advice"]]
    return "\n".join(lines)
