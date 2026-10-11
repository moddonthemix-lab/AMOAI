"""Your knowledge folder: drop documents in, AMO reads them and answers from them.

    ~/Documents/AMO Knowledge/   (AMO_KNOWLEDGE_DIR)
        price sheet.pdf   lyrics/   Cravvr business plan.docx   strat notes.md   clients.csv …

Files are split into pieces, embedded with the local embedding model and searched by meaning.
When something you ask matches your documents closely, the best piece is put in front of AMO
automatically; "what do my docs say about mixing prices?" searches on purpose.
New and changed files are picked up in the background (deleted ones are forgotten).
"""

from __future__ import annotations

import json
import re
import zipfile
from html import unescape
from pathlib import Path
from typing import Any

from .config import settings
from .db import Database, get_db
from .recall import VectorIndex

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".json", ".py", ".js", ".ts", ".html", ".htm", ".rtf", ".yaml",
            ".yml", ".ino", ".swift", ".sql", ".log"}
DOC_EXT = TEXT_EXT | {".pdf", ".docx"}
CHUNK, OVERLAP = 900, 120
MAX_FILE_BYTES = 20_000_000
INDEX = VectorIndex("knowledge_chunks")

ASK_RE = re.compile(
    r"^(?:(?:hey )?amo[ ,]*)?(?:what do|what does|check|search|look in|look through|find in) (?:my )?"
    r"(?:docs|documents|files|knowledge(?: folder)?|notes folder|papers)(?: say| have| for| about)*\s*(?:about|on|for)?\s+"
    r"(?P<q>.+?)[?.!]*$", re.I)


def folder() -> Path:
    return Path(settings.knowledge_dir).expanduser()


def ensure_folder() -> Path:
    f = folder()
    if not f.exists():
        f.mkdir(parents=True, exist_ok=True)
        (f / "READ ME.txt").write_text(
            f"Drop anything you want {settings.assistant_name} to know in this folder: PDFs, Word docs, text, "
            "notes, lyrics, price sheets, plans, CSVs. Subfolders are fine.\n"
            f"{settings.assistant_name} reads new and changed files automatically within a few minutes.\n"
            "Delete a file and it's forgotten.\n")
    return f


# ------------------------------------------------------------------ reading files
def read_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return ""
        try:
            return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages[:300])
        except Exception:  # noqa: BLE001 — a broken / encrypted PDF
            return ""
    if ext == ".docx":
        try:
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", "replace")
        except (zipfile.BadZipFile, KeyError, OSError):
            return ""
        xml = re.sub(r"</w:p>", "\n", xml)
        return unescape(re.sub(r"<[^>]+>", "", xml))
    raw = path.read_text(errors="replace")
    if ext in (".html", ".htm"):
        raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
        raw = unescape(re.sub(r"<[^>]+>", " ", raw))
    if ext == ".rtf":
        raw = re.sub(r"\\[a-z]+-?\d* ?|[{}]", "", raw)
    return raw


def chunks(text: str) -> list[str]:
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()
    if not text:
        return []
    out, i = [], 0
    while i < len(text):
        end = min(len(text), i + CHUNK)
        if end < len(text):  # end on a paragraph / sentence break if there's one nearby
            cut = max(text.rfind("\n\n", i + CHUNK // 2, end), text.rfind(". ", i + CHUNK // 2, end))
            end = cut + 1 if cut > 0 else end
        out.append(text[i:end].strip())
        if end >= len(text):
            break
        i = max(end - OVERLAP, i + 1)
    return [c for c in out if len(c) > 20]


# ------------------------------------------------------------------ keeping the index current
def sync(db: Database | None = None, budget: int = 80) -> dict[str, int]:
    """Add new/changed files, forget deleted ones, embed up to `budget` pieces. Cheap when nothing changed."""
    from .llm import get_llm

    db = db or get_db()
    root = folder()
    stats = {"files": 0, "added": 0, "removed": 0, "embedded": 0}
    if not root.is_dir():
        return stats
    seen: dict[str, float] = {}
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in DOC_EXT and not p.name.startswith((".", "~$")) \
                and p.stat().st_size <= MAX_FILE_BYTES:
            seen[p.relative_to(root).as_posix()] = p.stat().st_mtime
    stats["files"] = len(seen)
    known = {r["path"]: r["mtime"] for r in db.all("SELECT path, MAX(mtime) AS mtime FROM knowledge_chunks GROUP BY path")}
    for rel in set(known) - set(seen):
        db.execute("DELETE FROM knowledge_chunks WHERE path = ?", (rel,))
        stats["removed"] += 1
    for rel, mtime in seen.items():
        if known.get(rel) == mtime:
            continue
        db.execute("DELETE FROM knowledge_chunks WHERE path = ?", (rel,))
        pieces = chunks(read_text(root / rel)) or [f"(empty or unreadable file: {rel})"]
        with db.tx() as c:
            c.executemany("INSERT INTO knowledge_chunks (path, mtime, chunk, text) VALUES (?, ?, ?, ?)",
                          [(rel, mtime, i, t) for i, t in enumerate(pieces)])
        stats["added"] += 1
    llm = get_llm()
    for r in db.all("SELECT id, path, text FROM knowledge_chunks WHERE embedding IS NULL LIMIT ?", (budget,)):
        vec = llm.embed(f"{Path(r['path']).stem}: {r['text']}")
        if not vec:
            break
        db.execute("UPDATE knowledge_chunks SET embedding = ? WHERE id = ?", (json.dumps(vec), r["id"]))
        stats["embedded"] += 1
    return stats


# ------------------------------------------------------------------ finding things
def search(query: str, k: int = 4, db: Database | None = None, min_score: float = 0.5) -> list[dict[str, Any]]:
    from .llm import get_llm

    db = db or get_db()
    scores: dict[int, float] = {}
    q = get_llm().embed(query) if query.strip() else None
    if q:
        for cid, sim in INDEX.search(q, db, k=k * 2, min_score=min_score):
            scores[cid] = sim
    words = [w for w in re.findall(r"[a-z0-9$]+", query.lower()) if len(w) > 2][:8]
    if words and len(scores) < k:
        like = " OR ".join("lower(text) LIKE ?" for _ in words)
        for r in db.all(f"SELECT id, text FROM knowledge_chunks WHERE {like} LIMIT 200", tuple(f"%{w}%" for w in words)):
            hit = sum(w in r["text"].lower() for w in words) / len(words)
            if hit >= 0.5:
                scores[r["id"]] = max(scores.get(r["id"], 0), 0.35 + 0.3 * hit)
    out = []
    for cid, score in sorted(scores.items(), key=lambda kv: -kv[1])[:k]:
        r = db.one("SELECT path, text FROM knowledge_chunks WHERE id = ?", (cid,))
        if r:
            out.append({"file": r["path"], "text": r["text"], "score": round(score, 3)})
    return out


def context_for(user_text: str, db: Database | None = None) -> str:
    """For the turn note: the best-matching piece of your documents, only if it's a close match."""
    try:
        db = db or get_db()
        if not db.scalar("SELECT 1 FROM knowledge_chunks LIMIT 1"):
            return ""
        hits = [h for h in search(user_text, k=2, db=db, min_score=0.62) if h["score"] >= 0.62]
    except Exception:  # noqa: BLE001
        return ""
    return " | ".join(f"From your file “{h['file']}”: {h['text'][:600]}" for h in hits)


def match(text: str) -> tuple[str, dict[str, Any]] | None:
    m = ASK_RE.match(text.strip())
    return ("search_knowledge", {"query": m.group("q")}) if m else None


def answer(query: str) -> dict[str, Any]:
    hits = search(query, k=3)
    if not hits:
        n = get_db().scalar("SELECT COUNT(DISTINCT path) FROM knowledge_chunks")
        where = folder()
        return {"text": f"Nothing about {query} in your files ({n} file{'s' if n != 1 else ''} in {where})."}
    return {"text": " · ".join(f"“{h['file']}”: {h['text'][:400]}" for h in hits), "hits": hits}
