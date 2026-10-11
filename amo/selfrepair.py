"""Self-repair: AMO notices its own crashes and fixes simple ones — safely.

1. Every crash inside AMO's code is recorded (type, file, line, traceback), grouped by where it happened.
2. When you've gone quiet, AMO asks its local code model for the smallest fix (≤ 30 changed lines,
   only in amo/*.py — never the tests, the database layer or the repair system itself).
3. The fix is tested on a COPY of AMO: all of its tests must pass, before and after.
4. Only then is it applied — the old files are kept as a snapshot, the server restarts, and you get a
   notification. "Roll back your last fix" / `amo rollback` puts the snapshot back.
A fix that fails the tests is thrown away; after two failed tries AMO tells you and stops trying.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .config import PROJECT_ROOT, settings
from .db import Database, get_db, local_now, now_iso

log = logging.getLogger(__name__)

MAX_CHANGED_LINES = 30
MAX_ATTEMPTS = 2
MAX_REPAIRS_PER_DAY = 3
PROTECTED = {"amo/db.py", "amo/config.py", "amo/selfrepair.py", "amo/coding.py", "amo/__init__.py"}
SNAPSHOTS = PROJECT_ROOT / "data" / "snapshots"
_lock = threading.Lock()


# ------------------------------------------------------------------ 1. noticing crashes
def _where(tb, project: Path) -> tuple[str, int, str] | None:
    """The deepest frame inside AMO's own code (not tests, not libraries)."""
    found = None
    for frame in traceback.extract_tb(tb):
        try:
            rel = Path(frame.filename).resolve().relative_to(project.resolve()).as_posix()
        except ValueError:
            continue
        if rel.startswith("amo/"):
            found = (rel, frame.lineno or 0, frame.name)
    return found


def record(exc: BaseException, db: Database | None = None, project: Path = PROJECT_ROOT,
           repro: dict[str, Any] | None = None) -> bool:
    """Remember a crash. Returns True if it was in AMO's own code."""
    where = _where(exc.__traceback__, project)
    if where is None:
        return False
    rel, line, func = where
    sig = f"{type(exc).__name__}:{rel}:{func}"
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-6000:]
    db = db or get_db()
    now = now_iso()
    if db.one("SELECT id FROM errors WHERE signature = ?", (sig,)):
        db.execute("UPDATE errors SET count = count + 1, last_seen = ?, traceback = ?, line = ?, message = ?, "
                   "repro = COALESCE(?, repro), "
                   "status = CASE WHEN status = 'fixed' THEN 'open' ELSE status END WHERE signature = ?",
                   (now, tb, line, str(exc)[:500], json.dumps(repro, default=str) if repro else None, sig))
    else:
        db.insert("errors", {"signature": sig, "kind": type(exc).__name__, "message": str(exc)[:500],
                             "traceback": tb, "file": rel, "line": line, "first_seen": now, "last_seen": now,
                             "repro": json.dumps(repro, default=str) if repro else None})
    return True


class ErrorRecorder(logging.Handler):
    """Logging handler: every logged exception (log.exception / exc_info=True) is recorded."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)

    def emit(self, rec: logging.LogRecord) -> None:
        if rec.exc_info and rec.exc_info[1] is not None:
            try:
                record(rec.exc_info[1], repro=getattr(rec, "amo_repro", None))
            except Exception:  # noqa: BLE001 — never let error recording cause errors
                pass


def install() -> None:
    """Start recording crashes in this process (server, listener)."""
    for name in ("", "uvicorn.error"):
        lg = logging.getLogger(name)
        if not any(isinstance(h, ErrorRecorder) for h in lg.handlers):
            lg.addHandler(ErrorRecorder())


# ------------------------------------------------------------------ 2-3. fixing, tested on a copy
def run_tests(project: Path, timeout: int = 900) -> tuple[bool, str]:
    """Run AMO's test suite in `project`. (passed, last lines of output)"""
    env = {**os.environ, "PYTHONPATH": str(project), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"], cwd=project,
                           env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "tests took too long"
    except FileNotFoundError as e:
        return False, str(e)
    out = (r.stdout + r.stderr).strip().splitlines()
    return r.returncode == 0, "\n".join(out[-15:])


def _sandbox(project: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="amo-repair-"))
    for part in ("amo", "tests"):
        if (project / part).is_dir():
            shutil.copytree(project / part, tmp / part, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in ("pyproject.toml", "conftest.py"):
        if (project / f).is_file():
            shutil.copy2(project / f, tmp / f)
    return tmp


REPRO = """import json, sys
from amo import tools
r = json.loads(tools.call(sys.argv[1], json.loads(sys.argv[2])))
bad = isinstance(r, dict) and str(r.get("error", "")).startswith("something broke inside me")
print(r.get("error") if bad else "ok")
sys.exit(1 if bad else 0)
"""


def reproduce(box: Path, repro: dict[str, Any], db_path: str | None = None) -> tuple[bool, str]:
    """Replay the request that crashed, in the sandbox, on a copy of your data. (fixed?, output)"""
    data = box / "data"
    data.mkdir(exist_ok=True)
    live = Path(db_path or settings.db_path)
    copy = data / "amo.db"
    if live.is_file():  # a consistent copy, including changes still in the write-ahead log
        import sqlite3

        src, dst = sqlite3.connect(live), sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
    env = {**os.environ, "PYTHONPATH": str(box), "AMO_DB_PATH": str(copy), "AMO_ENV_FILE": "/nonexistent",
           "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        r = subprocess.run([sys.executable, "-c", REPRO, repro["tool"], json.dumps(repro.get("args") or {})],
                           cwd=box, env=env, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, "the replay took too long"
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-800:]


def _context(path: Path, line: int, around: int = 40) -> str:
    lines = path.read_text().split("\n")
    lo, hi = max(0, line - around), min(len(lines), line + around)
    return "\n".join(lines[lo:hi])


def repair(error: dict[str, Any], db: Database | None = None, project: Path = PROJECT_ROOT,
           coder: Callable[[str], str] | None = None, tests: Callable[[Path], tuple[bool, str]] = run_tests,
           restart: bool = True) -> dict[str, Any]:
    """Try to fix one recorded error. Returns {"result": applied|tests_failed|no_fix|ignored|paused, ...}."""
    from .coding import EDIT_RULES, ask_coder, changed_lines, diff, parse_edits, preview

    db = db or get_db()
    project = project.resolve()
    rel = error["file"]

    def done(result: str, detail: str = "", d: str = "", snapshot: str = "") -> dict[str, Any]:
        db.insert("repairs", {"error_id": error["id"], "files": rel, "diff": d, "result": result,
                              "detail": detail[-2000:], "snapshot": snapshot, "created_at": now_iso()})
        if result == "applied":
            db.execute("UPDATE errors SET status = 'fixed', attempts = attempts + 1 WHERE id = ?", (error["id"],))
        elif result == "ignored":  # not something AMO edits on its own
            db.execute("UPDATE errors SET status = 'ignored' WHERE id = ?", (error["id"],))
        elif result == "paused":  # the tests fail even without a fix: wait (e.g. after an update)
            db.set_kv("repair_paused_until", (local_now() + timedelta(hours=6)).isoformat())
        elif result in ("tests_failed", "no_fix"):
            db.execute("UPDATE errors SET attempts = attempts + 1, status = CASE WHEN attempts + 1 >= ? "
                       "THEN 'failed' ELSE status END WHERE id = ?", (MAX_ATTEMPTS, error["id"]))
        return {"result": result, "detail": detail, "diff": d, "file": rel, "snapshot": snapshot}

    if rel in PROTECTED or not rel.startswith("amo/") or not (project / rel).is_file():
        return done("ignored", f"{rel} is not something I repair on my own")
    path = project / rel
    prompt = (f"AMO (a Python app) crashed. Fix the cause with the smallest safe change.\n\n"
              f"Traceback:\n{error['traceback'][-3000:]}\n\n"
              f"The crash is at line {error['line']} of {rel}. The code around it:\n\nFILE: {rel}\n```python\n"
              f"{_context(path, error['line'])}\n```")
    answer = (coder or (lambda p: ask_coder(p, system_extra="\n\n" + EDIT_RULES)))(prompt)
    edits = [e for e in parse_edits(answer, rel) if e.file == rel]
    if not edits:
        return done("no_fix", answer[:500])
    try:
        new = preview(project, edits)
    except ValueError as e:
        return done("no_fix", str(e))
    d = diff(project, new)
    if changed_lines(d) > MAX_CHANGED_LINES:
        return done("no_fix", f"the fix was too big ({changed_lines(d)} lines)", d)

    box = _sandbox(project)
    try:
        ok, out = tests(box)
        if not ok:  # already failing without the fix — can't judge a fix, don't touch anything
            return done("paused", "the tests already fail without the fix:\n" + out, d)
        for f, text in new.items():
            (box / f).write_text(text)
        ok, out = tests(box)
        if ok and error.get("repro"):  # and the request that crashed now works (on a copy of your data)
            fixed, rout = reproduce(box, json.loads(error["repro"]))
            if not fixed:
                ok, out = False, "the tests pass but the original problem still happens:\n" + rout
    finally:
        shutil.rmtree(box, ignore_errors=True)
    if not ok:
        return done("tests_failed", out, d)

    # 4. apply, keeping a snapshot to roll back to
    snap = SNAPSHOTS / local_now().strftime("%Y%m%d-%H%M%S")
    for f, text in new.items():
        (snap / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(project / f, snap / f)
        (project / f).write_text(text)
    (snap / "manifest.json").write_text(json.dumps({"project": str(project), "files": list(new)}))
    result = done("applied", out, d, str(snap))
    from .notify import notify

    notify("AMO fixed itself", f"{error['kind']} in {rel}: fixed ({changed_lines(d)} lines, all tests pass). "
           "Say “roll back your last fix” to undo.", db=db)
    if restart:
        restart_soon()
    return result


def restart_soon() -> None:
    """Restart the background AMO server so the fix takes effect (macOS launchd)."""
    script = PROJECT_ROOT / "scripts" / "mac-autostart.sh"
    if sys.platform == "darwin" and script.is_file():
        subprocess.Popen(["/bin/bash", "-c", f"sleep 3; '{script}' restart"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def rollback(db: Database | None = None, project: Path | None = None, restart: bool = True) -> dict[str, Any]:
    """Put back the files from the last applied fix."""
    db = db or get_db()
    row = db.one("SELECT * FROM repairs WHERE result = 'applied' ORDER BY id DESC LIMIT 1")
    if not row or not row["snapshot"] or not Path(row["snapshot"]).is_dir():
        raise ValueError("I haven't fixed myself recently, so there's nothing to roll back")
    snap = Path(row["snapshot"])
    manifest = json.loads((snap / "manifest.json").read_text())
    root = project or Path(manifest["project"])
    for f in manifest["files"]:
        shutil.copy2(snap / f, root / f)
    db.update("repairs", row["id"], {"result": "rolled_back"})
    db.execute("UPDATE errors SET status = 'ignored' WHERE id = ?", (row["error_id"],))
    if restart:
        restart_soon()
    return {"rolled_back": True, "files": manifest["files"],
            "text": f"Rolled back my last fix ({', '.join(manifest['files'])}). I won't try that one again."}


# ------------------------------------------------------------------ when it runs
def due(db: Database, now: datetime) -> list[dict[str, Any]]:
    today = now.date().isoformat()
    paused = db.get_kv("repair_paused_until")
    if paused and paused > now.isoformat():
        return []
    made = db.scalar("SELECT COUNT(*) FROM repairs WHERE substr(created_at, 1, 10) = ? "
                     "AND result NOT IN ('ignored', 'paused')", (today,))
    if made >= MAX_REPAIRS_PER_DAY:
        return []
    return db.all("SELECT * FROM errors WHERE status = 'open' AND attempts < ? ORDER BY count DESC, last_seen DESC "
                  "LIMIT ?", (MAX_ATTEMPTS, MAX_REPAIRS_PER_DAY - made))


def run(db: Database | None = None, now: datetime | None = None, **kw: Any) -> list[dict[str, Any]]:
    """Fix what's due (one at a time; never two repairs at once)."""
    db = db or get_db()
    now = now or local_now()
    if not _lock.acquire(blocking=False):
        return []
    try:
        out = []
        for err in due(db, now):
            r = repair(err, db, **kw)
            out.append(r)
            if db.scalar("SELECT status FROM errors WHERE id = ?", (err["id"],)) == "failed":
                from .notify import notify

                notify("A bug I couldn't fix", f"{err['kind']} in {err['file']}: {(err['message'] or '')[:200]}",
                       db=db)
            if r["result"] in ("applied", "paused"):
                break  # applied: the server restarts to load the fix · paused: try again later
        return out
    finally:
        _lock.release()


def maybe_run(db: Database, now: datetime, idle_for: timedelta = timedelta(minutes=10)) -> bool:
    """Scheduler job: when self-repair is on and you've been quiet, fix due errors in the background."""
    if not settings.self_repair or not due(db, now):
        return False
    last = db.scalar("SELECT MAX(created_at) FROM conversations")
    if last and last > (now - idle_for).isoformat():
        return False
    threading.Thread(target=run, args=(get_db(), now), daemon=True).start()
    return True


def status_text(db: Database | None = None) -> str:
    db = db or get_db()
    open_ = db.all("SELECT kind, file, count FROM errors WHERE status = 'open' ORDER BY count DESC LIMIT 5")
    fixed = db.all("SELECT r.created_at, e.kind, e.file FROM repairs r JOIN errors e ON e.id = r.error_id "
                   "WHERE r.result = 'applied' ORDER BY r.id DESC LIMIT 3")
    parts = []
    if fixed:
        parts.append("Fixed myself: " + "; ".join(f"{f['kind']} in {f['file']} ({f['created_at'][:10]})" for f in fixed) + ".")
    if open_:
        parts.append("Open bugs: " + "; ".join(f"{e['kind']} in {e['file']} ×{e['count']}" for e in open_) + ".")
    return " ".join(parts) or "No bugs on record — I'm running clean."
