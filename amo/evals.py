"""The test set: AMO is scored on real requests, so every change (or a new AI model) is measured.

    amo eval                          built-in cases + yours, with the current model
    amo eval --models gemma4:e2b,qwen3:4b,llama3.2:3b     compare models on the same requests
    amo eval --mine                   also replay your own past requests that worked (regression check)
    amo eval --add "put calling mom on my goals" --tool add_goal      add your own case
    amo eval --offline                skip cases that need the internet (markets, web)

Every case runs in a throwaway database (seeded with sample clients/goals/trades, or a copy of
your real data for --mine) — nothing touches your data, no texts are sent, no phone pushes.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import statistics
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, settings
from .db import Database, get_db, local_now, set_db

BUILTIN = PROJECT_ROOT / "evals" / "cases.json"
CUSTOM = PROJECT_ROOT / "data" / "evals" / "my_cases.json"
RESULTS = PROJECT_ROOT / "data" / "evals"
NETWORK_TOOLS = {"strat_analysis", "strat_compare", "show_chart", "quote", "watchlist_add", "watchlist_scan",
                 "check_trade", "web_search", "read_webpage"}
UNSAFE_TOOLS = {"send_text", "code_apply", "code_undo", "self_repair", "self_rollback", "hardware_control",
                "look", "code_fix", "set_pronunciation"}


@dataclass
class Case:
    say: str
    before: list[str] = field(default_factory=list)   # earlier turns in the same conversation
    tool: list[str] = field(default_factory=list)     # one of these must be called successfully
    no_tool: bool = False                             # must answer without any tool
    args: dict[str, str] = field(default_factory=dict)  # tool argument → text it must contain
    reply_has: list[str] = field(default_factory=list)  # all must appear in the reply (any case)
    reply_any: list[str] = field(default_factory=list)  # at least one must appear
    reply_not: list[str] = field(default_factory=list)
    path: str = ""                                    # instant | fastpath | model | coder
    sql: str = ""                                     # a check on the database afterwards…
    sql_equals: Any = None                            # …must equal this
    tags: list[str] = field(default_factory=list)
    network: bool = False
    source: str = "builtin"

    @classmethod
    def from_dict(cls, d: dict[str, Any], source: str) -> "Case":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        for k in ("tool", "reply_has", "reply_any", "reply_not", "tags", "before"):
            if isinstance(known.get(k), str):
                known[k] = [known[k]]
        c = cls(**known)
        c.source = source
        c.network = c.network or bool(set(c.tool) & NETWORK_TOOLS)
        return c


# ------------------------------------------------------------------ cases
def builtin_cases() -> list[Case]:
    data = json.loads(BUILTIN.read_text()) if BUILTIN.is_file() else []
    return [Case.from_dict(d, "builtin") for d in data]


def custom_cases() -> list[Case]:
    data = json.loads(CUSTOM.read_text()) if CUSTOM.is_file() else []
    return [Case.from_dict(d, "custom") for d in data]


def add_custom(say: str, tool: str = "", reply_has: str = "") -> Case:
    data = json.loads(CUSTOM.read_text()) if CUSTOM.is_file() else []
    d: dict[str, Any] = {"say": say, "tags": ["mine"]}
    if tool:
        d["tool"] = [t.strip() for t in tool.split(",")]
    if reply_has:
        d["reply_has"] = [reply_has]
    if not tool and not reply_has:
        d["no_tool"] = True
    data.append(d)
    CUSTOM.parent.mkdir(parents=True, exist_ok=True)
    CUSTOM.write_text(json.dumps(data, indent=1))
    return Case.from_dict(d, "custom")


def mine_cases(db: Database | None = None, limit: int = 60) -> list[Case]:
    """Your own past requests that worked, replayed on a copy of your data: they must keep working."""
    db = db or get_db()
    since = (local_now() - timedelta(days=90)).isoformat()
    rows = db.all("SELECT request, tools FROM timings WHERE ok = 1 AND tools != '' AND request != '' AND at >= ? "
                  "AND channel != 'eval' ORDER BY id DESC", (since,))
    seen, out = set(), []
    for r in rows:
        key = r["request"].strip().lower()
        first = r["tools"].split(",")[0]
        if key in seen or first in UNSAFE_TOOLS or len(key) < 4:
            continue
        seen.add(key)
        out.append(Case(say=r["request"], tool=[first], tags=["mine"], source="mine",
                        network=first in NETWORK_TOOLS))
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ a safe place to run them
SEED = """
INSERT INTO clients (name, artist_name, phone, status, created_at) VALUES
  ('Jay Carter', 'Lil Jay', '555-0101', 'active', '2026-01-01T00:00:00'),
  ('Tyrese Brown', 'Ty', '555-0102', 'active', '2026-01-01T00:00:00');
INSERT INTO goals (title, area, cadence, created_at) VALUES
  ('Make one beat', 'studio', 'daily', '2026-01-01T00:00:00'),
  ('Gym', 'health', 'daily', '2026-01-01T00:00:00');
INSERT INTO trading_rules (rule, created_at) VALUES ('No trades after 11am', '2026-01-01T00:00:00');
INSERT INTO watchlist (symbol, created_at) VALUES ('AMZN', '2026-01-01T00:00:00');
INSERT INTO resale_items (name, cost, status, bought_at) VALUES ('Jordan 4 Bred size 10', 210, 'inventory', '2026-01-01');
INSERT INTO cravvr_tasks (title, priority, status, created_at) VALUES ('Finalize menu pricing', 4, 'todo', '2026-01-01T00:00:00');
INSERT INTO trades (symbol, side, entry, quantity, opened_at) VALUES ('NQ=F', 'long', 18000, 1, '2026-01-01T09:45:00');
INSERT INTO notes (text, tags, created_at) VALUES ('Beat idea, 140 BPM, dark piano', 'music, idea', '2026-01-01T00:00:00');
"""


def _seeded() -> Database:
    db = Database(":memory:")
    with db.tx() as c:
        c.executescript(SEED)
    tomorrow = (local_now() + timedelta(days=1)).strftime("%Y-%m-%dT19:00:00")
    db.execute("INSERT INTO studio_sessions (client_id, starts_at, hours, service, rate, status, created_at) "
               "VALUES (1, ?, 3, 'recording', 50, 'booked', ?)", (tomorrow, local_now().isoformat()))
    from .memory import Memory

    Memory(db, use_embeddings=False).add("Marcus is my engineer and works Tuesdays", "person", 4)
    return db


def _copy_of(live: Database) -> tuple[Database, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="amo-eval-")) / "amo.db"
    dst = sqlite3.connect(tmp)
    with live._lock:
        live.conn.backup(dst)
    dst.close()
    return Database(str(tmp)), tmp


@contextmanager
def _sandbox(model: str | None):
    """No phone pushes, no learning in the background, the requested model, and the real DB restored after."""
    saved = {k: getattr(settings, k) for k in ("ntfy_url", "auto_learn", "chat_model", "self_repair", "print_dry_run")}
    real_db = get_db()
    settings.ntfy_url, settings.auto_learn, settings.self_repair = "", False, False
    settings.print_dry_run = True  # a test run never uses real paper
    if model:
        settings.chat_model = model
    try:
        yield real_db
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)
        set_db(real_db)


# ------------------------------------------------------------------ running and scoring
def check(case: Case, out: dict[str, Any], path: str, db: Database) -> list[str]:
    """Why this case failed ([] = passed)."""
    why = []
    reply = (out.get("content") or "").lower()
    calls = [t for t in out.get("tool_calls", []) if not (isinstance(t.get("result"), dict) and "error" in t["result"])]
    names = [t["name"] for t in calls]
    if case.tool and not set(case.tool) & set(names):
        errors = [t["result"]["error"] for t in out.get("tool_calls", []) if isinstance(t.get("result"), dict)
                  and "error" in t["result"]]
        why.append(f"expected {' or '.join(case.tool)}, got {', '.join(names) or 'no tool'}"
                   + (f" (error: {errors[0][:80]})" if errors else ""))
    if case.no_tool and names:
        why.append(f"expected no tool, got {', '.join(names)}")
    for k, want in case.args.items():
        got = " ".join(str(t["arguments"].get(k, "")) for t in calls if isinstance(t.get("arguments"), dict))
        if want.lower() not in got.lower():
            why.append(f"{k} should contain “{want}” (got “{got[:60]}”)")
    for s in case.reply_has:
        if s.lower() not in reply:
            why.append(f"reply should mention “{s}”")
    if case.reply_any and not any(s.lower() in reply for s in case.reply_any):
        why.append(f"reply should mention one of {case.reply_any}")
    for s in case.reply_not:
        if s.lower() in reply:
            why.append(f"reply shouldn't say “{s}”")
    if case.path and path != case.path:
        why.append(f"should be {case.path}, was {path}")
    if case.sql:
        got = db.scalar(case.sql)
        if got != case.sql_equals:
            why.append(f"database check: {got!r} ≠ {case.sql_equals!r}")
    if not reply.strip():
        why.append("empty reply")
    return why


def run_case(case: Case, live: Database, model: str | None = None) -> dict[str, Any]:
    from .agent import Agent
    from .perf import Turn

    tmp = None
    db, tmp = _copy_of(live) if case.source == "mine" else (_seeded(), None)
    set_db(db)
    history: list[dict[str, Any]] = []
    try:
        agent = Agent(db)
        for line in case.before:
            history.append({"role": "user", "content": line})
            history.append({"role": "assistant", "content": agent.chat(list(history), channel="eval")["content"]})
        history.append({"role": "user", "content": case.say})
        turn = Turn("eval", case.say)
        started = time.monotonic()
        try:
            out = agent.chat(list(history), channel="eval", model=model, turn=turn)
            error = None
        except Exception as e:  # noqa: BLE001 — a crash is a failure, not the end of the run
            out, error = {"content": "", "tool_calls": []}, f"{type(e).__name__}: {e}"
        total = int((time.monotonic() - started) * 1000)
        path = turn.info.get("path", "")
        why = [f"crashed: {error}"] if error else check(case, out, path, db)
        from .brain import kind

        return {"say": case.say, "source": case.source, "tags": case.tags, "ok": not why, "why": why,
                "kind": kind(case.say, case.before[-1] if case.before else ""),
                "reply": (out.get("content") or "")[:300], "tools": [t["name"] for t in out.get("tool_calls", [])],
                "path": path, "first_ms": turn.marks.get("first_text", total), "total_ms": total,
                "model_calls": turn.info.get("model_calls", 0)}
    finally:
        if tmp:
            shutil.rmtree(tmp.parent, ignore_errors=True)


def run(model: str | None = None, mine: bool = False, offline: bool = False, only: str = "",
        cases: list[Case] | None = None, progress=None) -> dict[str, Any]:
    cases = cases if cases is not None else builtin_cases() + custom_cases()
    with _sandbox(model) as live:
        if mine and not only:
            cases = cases + mine_cases(live)
        if only:
            cases = [c for c in cases if only in c.tags or only == c.source]
        if offline:
            cases = [c for c in cases if not c.network]
        results = []
        for i, c in enumerate(cases):
            r = run_case(c, live, model)
            results.append(r)
            if progress:
                progress(i + 1, len(cases), r)
    return summarize(results, model or settings.chat_model)


def summarize(results: list[dict[str, Any]], model: str) -> dict[str, Any]:
    ai = [r for r in results if r["path"] in ("model", "coder") or "ai" in r["tags"]]
    med = lambda xs: int(statistics.median(xs)) if xs else None  # noqa: E731
    out = {
        "model": model, "at": local_now().isoformat(timespec="seconds"), "cases": len(results),
        "passed": sum(r["ok"] for r in results),
        "score": round(100 * sum(r["ok"] for r in results) / len(results), 1) if results else 0.0,
        "ai_score": round(100 * sum(r["ok"] for r in ai) / len(ai), 1) if ai else None,
        "first_median_ms": med([r["first_ms"] for r in results]),
        "ai_first_median_ms": med([r["first_ms"] for r in ai]),
        "total_median_ms": med([r["total_ms"] for r in results]),
        # reply time only for requests the AI answered (shortcuts are instant and would hide it)
        "ai_total_median_ms": med([r["total_ms"] for r in results if r["path"] in ("model", "coder")]),
        "failures": [r for r in results if not r["ok"]],
        "results": results,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    name = f"results-{out['at'].replace(':', '')}-{model.replace(':', '_').replace('/', '_')}.json"
    (RESULTS / name).write_text(json.dumps(out, indent=1))
    return out


def text(summary: dict[str, Any], show: int = 15) -> str:
    s = summary
    sec = lambda ms: "—" if ms is None else f"{ms / 1000:.1f}s"  # noqa: E731
    lines = [f"{s['model']}: {s['passed']}/{s['cases']} passed ({s['score']}%)"
             + (f" · AI-answered cases {s['ai_score']}%" if s["ai_score"] is not None else "")
             + f" · first word {sec(s['first_median_ms'])} (AI {sec(s['ai_first_median_ms'])})"]
    for f in s["failures"][:show]:
        lines.append(f"  ✗ “{f['say'][:70]}” — {'; '.join(f['why'])[:160]}")
        if f["reply"]:
            lines.append(f"      AMO: {f['reply'][:120]}")
    if len(s["failures"]) > show:
        lines.append(f"  … and {len(s['failures']) - show} more (data/evals/results-*.json)")
    return "\n".join(lines)


def compare(summaries: list[dict[str, Any]]) -> str:
    from .brain import KINDS, broken, recommend

    sec = lambda ms: "—" if ms is None else f"{ms / 1000:.1f}s"  # noqa: E731
    bad = {s["model"]: broken(s) for s in summaries if broken(s)}
    rows = sorted((s for s in summaries if s["model"] not in bad),
                  key=lambda s: (-s["score"], s["ai_first_median_ms"] or 10**9))
    lines = ["model                      score   AI cases  AI reply time"]
    for s in rows:
        lines.append(f"{s['model']:<26} {s['score']:>5}%   {str(s['ai_score'] or '—'):>6}%  "
                     f"{sec(s.get('ai_total_median_ms', s['ai_first_median_ms'])):>13}")
    for m, err in bad.items():
        lines.append(f"{m:<26} didn't run — {err[:110]}")
    if not rows:
        return "\n".join(lines + ["", "None of the models ran. Check they're downloaded: ollama list"])

    rec = recommend(summaries)
    lines += ["", "By kind of request (accuracy · reply time · cases):"]
    for k in KINDS:
        cells = "   ".join(f"{r['model']} {r['acc']:.0f}% {sec(r['ms'])}" for r in rec["table"].get(k, []))
        n = min((r["n"] for r in rec["table"].get(k, [])), default=0)
        lines.append(f"  {k:<7} {cells or '—'}" + (f"   ({n} cases)" if cells else "")
                     + ("  — too few to judge, keeping " + rec["split"][k] if k in rec["kept"] else ""))
    best = rows[0]
    fastest_good = min((s for s in rows if s["score"] >= best["score"] - 3),
                       key=lambda s: s["ai_first_median_ms"] or 10**9)
    pick = fastest_good if fastest_good is not best else best
    lines.append(f"\nOne model: {pick['model']} — "
                 + ("best score" if pick is best else "within 3 points of the best and faster") +
                 f".  Switch with:  amo use {pick['model']}")
    split = rec["split"]
    if len(set(split.values())) > 1:
        lines.append("Two-tier (each kind to the fastest model that's as good there): "
                     + ", ".join(f"{k} → {split[k]}" for k in KINDS if k in split)
                     + ".  Switch with:  amo brain --auto")
    return "\n".join(lines)
