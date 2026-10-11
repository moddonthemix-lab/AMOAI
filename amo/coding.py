"""AMO's coding brain: a local code model (qwen2.5-coder via Ollama) that explains code and errors,
and proposes exact fixes — applied only when you say so, with a backup so "undo the fix" works.

    "Why does this crash? ```…```"            "Explain ~/Projects/beatstore/app.py"
    "Fix ~/Projects/beatstore/app.py — the cart total is wrong"
    "apply the fix"        "undo the fix"

AMO only reads and writes code inside AMO_CODE_DIRS (and its own folder).
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT, settings
from .db import Database, get_db, local_now

MAX_FILE_CHARS = 14_000
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".md", ".sh", ".swift", ".java",
            ".c", ".cpp", ".h", ".ino", ".go", ".rs", ".rb", ".php", ".sql", ".yml", ".yaml", ".toml", ".kt"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "data"}

CODER_SYSTEM = (
    "You are the coding brain of {assistant}, {owner}'s personal AI. You're an expert programmer. "
    "Explain plainly (they're not a professional developer), get to the cause fast, and show the fix as "
    "code. Keep answers short. Never invent files or APIs you can't see — say what you'd need to look at."
)
VOICE_NOTE = " This is a voice conversation: answer in 2-3 spoken sentences, no code blocks."
EDIT_RULES = """Reply ONLY with edits in this exact format (one block per change, no other text):

FILE: relative/path/to/file
<<<<<<< SEARCH
exact lines copied from the file
=======
the replacement lines
>>>>>>> REPLACE

Rules: SEARCH must match the file exactly (copy it, including indentation) and be unique — include a
couple of surrounding lines if needed. Change as little as possible. If you can't find a safe fix,
reply with just: NO FIX"""


# ------------------------------------------------------------------ where AMO may look
def roots() -> list[Path]:
    out = [PROJECT_ROOT.resolve()]
    for d in settings.code_dirs.split(","):
        p = Path(d.strip()).expanduser()
        if d.strip() and p.is_dir():
            out.append(p.resolve())
    return out


SECRET_NAMES = {".env", ".env.local", "id_rsa", "id_ed25519", ".netrc", ".npmrc", ".pypirc"}


def allowed(path: Path) -> bool:
    """Inside your code folders (after following symlinks and '..'), and never secrets or AMO's own data."""
    p = path.expanduser().resolve()
    if p.name in SECRET_NAMES or p.suffix in (".pem", ".key") or ".git" in p.parts:
        return False
    amo = PROJECT_ROOT.resolve()
    if p == amo / "data" or (amo / "data") in p.parents or ".venv" in p.parts:
        return False
    return any(p == r or r in p.parents for r in roots())


def find_path(ref: str) -> Path | None:
    """'~/Projects/app/main.py', '/Users/me/x.py', or just 'main.py' / 'beatstore' (searched in your code folders)."""
    ref = ref.strip().strip("`'\"“”").rstrip(".,?!")
    if not ref:
        return None
    p = Path(ref).expanduser()
    if p.is_absolute() or ref.startswith(("~", "./")):
        return p.resolve() if p.exists() and allowed(p) else None
    if ".." in Path(ref).parts:
        return None
    for root in roots():
        cand = root / ref
        if cand.exists() and allowed(cand):
            return cand.resolve()
        for depth_glob in (f"*/{ref}", f"*/*/{ref}", f"*/*/*/{ref}"):
            for cand in root.glob(depth_glob):
                if not (set(cand.parts) & SKIP_DIRS) and allowed(cand):
                    return cand.resolve()
    return None


def read(path: Path, limit: int = MAX_FILE_CHARS) -> str:
    """A file's text, or a folder's layout plus its main files."""
    if path.is_file():
        text = path.read_text(errors="replace")
        return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more characters)"
    files = [f for f in sorted(path.rglob("*")) if f.is_file() and f.suffix in CODE_EXT
             and not (set(f.relative_to(path).parts) & SKIP_DIRS)][:200]
    out = ["Folder layout:"] + [f"  {f.relative_to(path)}" for f in files[:80]]
    budget = limit - sum(len(x) for x in out)
    for f in sorted(files, key=lambda f: (f.name not in ("main.py", "app.py", "index.js", "package.json",
                                                          "README.md"), f.stat().st_size))[:6]:
        body = f.read_text(errors="replace")[: max(0, budget // 3)]
        if body:
            out.append(f"\n--- {f.relative_to(path)} ---\n{body}")
            budget -= len(body)
        if budget <= 500:
            break
    return "\n".join(out)


# ------------------------------------------------------------------ the code model
def ask_coder(prompt: str, voice: bool = False, model: str | None = None, system_extra: str = "",
              history: list[dict[str, Any]] | None = None) -> str:
    from .llm import get_llm

    system = CODER_SYSTEM.format(assistant=settings.assistant_name, owner=settings.owner_name)
    system += (VOICE_NOTE if voice else "") + system_extra
    earlier = [{"role": m["role"], "content": m["content"]} for m in (history or [])
               if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)][-6:]
    msg = get_llm().chat([{"role": "system", "content": system}, *earlier, {"role": "user", "content": prompt}],
                         model=model or settings.coder_model, options={"temperature": 0.2})
    return (msg.get("content") or "").strip()


# ------------------------------------------------------------------ edits
@dataclass
class Edit:
    file: str
    search: str
    replace: str


_EDIT_RE = re.compile(r"(?:FILE:\s*(?P<file>[^\n]+)\n)?\s*<{5,9} ?SEARCH\n(?P<search>.*?)\n?={5,9}\n(?P<replace>.*?)\n?>{5,9} ?REPLACE",
                      re.S)


def parse_edits(text: str, default_file: str = "") -> list[Edit]:
    edits, current = [], default_file
    for m in _EDIT_RE.finditer(text or ""):
        if m.group("file"):
            current = m.group("file").strip().strip("`*")
        edits.append(Edit(current, m.group("search"), m.group("replace")))
    return edits


def _locate(text: str, search: str) -> tuple[int, int] | None:
    """Where `search` is in `text` (exactly once), forgiving trailing spaces. None if missing/ambiguous."""
    if search and text.count(search) == 1:
        i = text.index(search)
        return i, i + len(search)
    lines, want = text.split("\n"), [ln.rstrip() for ln in search.strip("\n").split("\n")]
    hits = [i for i in range(len(lines) - len(want) + 1)
            if [ln.rstrip() for ln in lines[i:i + len(want)]] == want]
    if len(hits) != 1:
        return None
    start = sum(len(ln) + 1 for ln in lines[:hits[0]])
    end = start + sum(len(ln) + 1 for ln in lines[hits[0]:hits[0] + len(want)]) - 1
    return start, end


def preview(root: Path, edits: list[Edit]) -> dict[str, str]:
    """{relative file: new text} after the edits, or raise ValueError saying what didn't fit."""
    new: dict[str, str] = {}
    root = root.resolve()
    for e in edits:
        path = (root / e.file).resolve()
        if not (path == root or root in path.parents) or not path.is_file():
            raise ValueError(f"the fix points at a file I can't use: {e.file}")
        key = path.relative_to(root).as_posix()  # "a.py" and "./a.py" are the same file
        text = new.get(key, path.read_text())
        spot = _locate(text, e.search)
        if spot is None:
            raise ValueError(f"the fix doesn't match {e.file} exactly")
        new[key] = text[:spot[0]] + e.replace + text[spot[1]:]
    return new


def fingerprint(root: Path, files) -> dict[str, str]:
    """Hashes of files as they are now — to notice if they change before a fix is applied."""
    import hashlib

    return {f: hashlib.sha1((root / f).read_bytes()).hexdigest() for f in files}


def diff(root: Path, new: dict[str, str]) -> str:
    out = []
    for f, text in new.items():
        old = (root / f).read_text()
        out += difflib.unified_diff(old.splitlines(True), text.splitlines(True), f"a/{f}", f"b/{f}", n=2)
    return "".join(out)


def changed_lines(d: str) -> int:
    return sum(1 for ln in d.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---")))


def check_syntax(path: Path) -> str | None:
    """None if fine, else the problem (Python and JSON are checked)."""
    if path.suffix == ".py":
        try:
            compile(path.read_text(), str(path), "exec")
        except SyntaxError as e:
            return f"syntax error line {e.lineno}: {e.msg}"
    if path.suffix == ".json":
        try:
            json.loads(path.read_text())
        except ValueError as e:
            return f"invalid JSON: {e}"
    return None


# ------------------------------------------------------------------ fixing your code (with your OK)
PENDING = "code_fix"


def _root_for(path: Path) -> Path:
    """The project folder: nearest parent with a git repo / package file, else the file's folder."""
    p = path if path.is_dir() else path.parent
    for cand in [p, *p.parents]:
        if any((cand / m).exists() for m in (".git", "pyproject.toml", "package.json", "setup.py")):
            return cand
        if cand in roots():
            break
    return p


def propose_fix(ref: str, problem: str = "", db: Database | None = None) -> dict[str, Any]:
    db = db or get_db()
    path = find_path(ref)
    if path is None:
        raise ValueError(f"I can't find {ref!r} in your code folders ({', '.join(str(r) for r in roots())}).")
    if not allowed(path):
        raise ValueError("that's outside the folders I'm allowed to touch")
    root = _root_for(path)
    target = path if path.is_file() else None
    code = read(path)
    rel = str(target.relative_to(root)) if target else ""
    prompt = (f"Problem: {problem or 'find and fix the bug'}\n\n"
              + (f"FILE: {rel}\n```\n{code}\n```" if target else code))
    answer = ask_coder(prompt, system_extra="\n\n" + EDIT_RULES)
    edits = parse_edits(answer, rel)
    if not edits:
        return {"proposed": False, "text": "I looked but couldn't find a safe fix. " + answer[:300]}
    new = preview(root, edits)
    d = diff(root, new)
    db.set_kv(PENDING, json.dumps({"root": str(root), "new": new, "diff": d, "at": local_now().isoformat(),
                                   "before": fingerprint(root, new)}))
    files = ", ".join(new)
    return {"proposed": True, "files": list(new), "diff": d, "lines": changed_lines(d),
            "text": f"Here's the fix for {files} ({changed_lines(d)} lines):\n```diff\n{d}```\nSay “apply the fix” to make the change."}


BACKUPS = PROJECT_ROOT / "data" / "code-backups"


def apply_pending(db: Database | None = None) -> dict[str, Any]:
    db = db or get_db()
    raw = db.get_kv(PENDING)
    if not raw:
        raise ValueError("there's no fix waiting — ask me to fix something first")
    p = json.loads(raw)
    root = Path(p["root"])
    if p.get("before") and fingerprint(root, p["new"]) != p["before"]:
        db.execute("DELETE FROM kv WHERE key = ?", (PENDING,))
        raise ValueError("the file changed since I proposed the fix, so I didn't apply it (your edits are safe) "
                         "— ask me to fix it again")
    stamp = local_now().strftime("%Y%m%d-%H%M%S")
    backup = BACKUPS / stamp
    for f, text in p["new"].items():
        src = root / f
        (backup / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, backup / f)
        src.write_text(text)
        problem = check_syntax(src)
        if problem:  # never leave your code broken
            for g in p["new"]:
                if (backup / g).exists():
                    shutil.copy2(backup / g, root / g)
            raise ValueError(f"the fix broke {f} ({problem}), so I put it back")
    (backup / ".amo-root").write_text(str(root))
    (backup / ".amo-after.json").write_text(json.dumps(fingerprint(root, p["new"])))
    db.execute("DELETE FROM kv WHERE key = ?", (PENDING,))
    db.set_kv("code_fix_last", str(backup))
    return {"applied": True, "files": list(p["new"]), "text": f"Done — changed {', '.join(p['new'])}. "
                                                              "Say “undo the fix” to put it back."}


def recent_fix(db: Database | None = None, minutes: float = 30) -> bool:
    """A code fix of mine was applied in the last half hour."""
    last = (db or get_db()).get_kv("code_fix_last")
    try:
        when = datetime.strptime(Path(last).name, "%Y%m%d-%H%M%S")
    except (TypeError, ValueError):
        return False
    return when > local_now().replace(tzinfo=None) - timedelta(minutes=minutes)


def undo_last_fix(db: Database | None = None) -> dict[str, Any]:
    db = db or get_db()
    last = db.get_kv("code_fix_last")
    if not last or not Path(last).is_dir():
        raise ValueError("there's no code fix of mine to undo")
    backup = Path(last)
    root = Path((backup / ".amo-root").read_text())
    after = backup / ".amo-after.json"
    if after.exists():
        was = json.loads(after.read_text())
        now = {f: h for f, h in fingerprint(root, [f for f in was if (root / f).exists()]).items()}
        edited = [f for f in was if now.get(f) != was[f]]
        if edited:
            raise ValueError(f"you've changed {', '.join(edited)} since my fix, so I didn't put the old version "
                             "back (that would wipe your edits)")
    restored = []
    for f in backup.rglob("*"):
        if f.is_file() and f.name not in (".amo-root", ".amo-after.json"):
            rel = f.relative_to(backup)
            shutil.copy2(f, root / rel)
            restored.append(str(rel))
    db.execute("DELETE FROM kv WHERE key = ?", ("code_fix_last",))
    return {"undone": True, "files": restored, "text": f"Put back {', '.join(restored)} the way it was."}


# ------------------------------------------------------------------ understanding requests
_PATH = re.compile(r"(?:~|/|\./)[\w./-]*[\w/]|\b[\w-]+\.(?:" +
                   "|".join(e.lstrip(".") for e in CODE_EXT) + r")\b")
_STRONG = re.compile(r"```|Traceback \(most recent call last\)|\b\w+(?:Error|Exception):|\bnpm ERR!|"
                     r"\bundefined is not\b|\bsegmentation fault\b|\bstack ?trace\b|\bnull pointer\b", re.I)
_LANG = re.compile(r"\b(?:python|javascript|typescript|react|html|css|node\.?js|npm|pip install|swift|swiftui|sql|"
                   r"arduino|c\+\+|java|kotlin|rust|golang|php|bash|regex|json|yaml|flutter|django|flask|fastapi)\b", re.I)
_WORDS = re.compile(r"\b(?:code|coding|function|script|bug|debug|compile|compiler|syntax|variable|programming|"
                    r"algorithm|api|endpoint|repo|github|commit|deploy|website|app)\b", re.I)


APPLY_RE = re.compile(r"^(?:(?:ok(?:ay)?|yes|yeah|yep)[, ]+)?(?:apply|make|do) (?:the |that |your )?(?:fix|change|edit)s?"
                      r"(?: please)?[.!]*$|^apply it[.!]*$", re.I)
UNDO_FIX_RE = re.compile(r"^(?:undo|revert|roll ?back) (?:the |that |your |my )?(?:code )?(?:fix|change|edit)s?[.!]*$", re.I)
FIX_RE = re.compile(r"^(?:(?:hey )?amo[ ,]*)?(?:can you |please |could you )?fix (?:the (?:bug|error|problem) in )?"
                    r"(?P<path>\S+)(?:\s*[-—:,]\s*|\s+(?:so|because|where|the|it)\b\s*)?(?P<problem>.*)$", re.I)
EXPLAIN_RE = re.compile(r"^(?:(?:hey )?amo[ ,]*)?(?:can you |please )?(?:explain|look at|review|read|check) "
                        r"(?P<path>\S+)(?P<question>.*)$", re.I)


SELF_FIX_RE = re.compile(r"^(?:(?:hey )?amo[ ,]*)?(?:can you |please )?(?:fix|check|heal|repair|debug) (?:yourself|your (?:own )?"
                         r"(?:code|bugs|errors))\b"
                         r"|^(?:(?:do|did) you (?:have|hit|get)|have you (?:had|hit|got)) any (?:bugs|errors)"
                         r"(?: lately| today| recently| in your (?:own )?code)?[?.!]*$"
                         r"|^(?:what|any) (?:bugs|errors) (?:have you (?:had|hit)|in your (?:own )?code)"
                         r"(?: lately| today| recently)?[?.!]*$", re.I)
ROLLBACK_RE = re.compile(r"^(?:(?:hey )?amo[ ,]*)?(?:roll ?back|undo|revert) (?:your )?(?:last |latest )?"
                         r"(?:self[- ]?)?(?:fix|repair) (?:to|of|on) yourself|^(?:(?:hey )?amo[ ,]*)?(?:roll ?back|undo|revert) "
                         r"your (?:last |latest )?(?:self[- ]?)?(?:fix|repair|update)", re.I)


def is_code_question(text: str) -> bool:
    """A pasted error / code, a programming language, or several coding words together."""
    t = text or ""
    return bool(_STRONG.search(t) or _LANG.search(t)
                or len({w.lower() for w in _WORDS.findall(t)}) >= 2)


def match(text: str) -> tuple[str, dict[str, Any]] | None:
    t = text.strip()
    if ROLLBACK_RE.match(t):
        return "self_rollback", {}
    if SELF_FIX_RE.match(t):
        return "self_repair", {}
    # "make the change" / "undo that edit" are about code only while a code fix is waiting / just happened
    loose = not re.search(r"\b(?:fix|code)\b", t, re.I)
    if APPLY_RE.match(t) and not (loose and not get_db().get_kv(PENDING)):
        return "code_apply", {}
    if UNDO_FIX_RE.match(t) and not (loose and not recent_fix()):
        return "code_undo", {}
    m = FIX_RE.match(t)
    if m and _PATH.fullmatch(m.group("path").rstrip(".,")):
        return "code_fix", {"path": m.group("path"), "problem": m.group("problem").strip()}
    m = EXPLAIN_RE.match(t)
    if m and _PATH.fullmatch(m.group("path").rstrip(".,?")):
        return "code_explain", {"path": m.group("path"), "question": m.group("question").strip(" ,?")}
    return None


def explain(ref: str, question: str = "", voice: bool = False) -> dict[str, Any]:
    path = find_path(ref)
    if path is None:
        raise ValueError(f"I can't find {ref!r} in your code folders.")
    code = read(path)
    answer = ask_coder(f"{question or 'Explain what this does and point out any bugs.'}\n\n{path.name}:\n```\n{code}\n```",
                       voice=voice)
    return {"text": answer, "path": str(path)}


def answer(question: str, voice: bool = False, history: list[dict[str, Any]] | None = None) -> str:
    """A coding question or pasted error: include any files it mentions that AMO can read."""
    extra = []
    for ref in dict.fromkeys(_PATH.findall(question or "")):
        p = find_path(ref)
        if p and p.is_file():
            extra.append(f"\n{p.name}:\n```\n{read(p, 6000)}\n```")
    return ask_coder(question + "".join(extra[:3]), voice=voice, history=history)
