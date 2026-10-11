"""Long-term recall: local embeddings + a vector index.

Two things are searchable by meaning (not just keywords):
- memories (facts AMO saved)       → Memory.search uses MEMORY_INDEX
- every past conversation          → "what did I say about Jay's album last month?"

Embeddings come from the local Ollama embedding model (nomic-embed-text). Conversations are
embedded in the background by the scheduler when you've gone quiet, so it never slows a reply.
The index lives in RAM (a numpy matrix when numpy is installed, plain Python otherwise) and
reloads itself whenever new vectors land in the database. It's small: 10,000 conversations
≈ 30 MB — fine on a Raspberry Pi.
"""

from __future__ import annotations

import json
import math
import re
import threading
from typing import Any

from .db import Database, get_db
from .llm import get_llm

try:
    import numpy as np
except ImportError:  # the core install doesn't need numpy
    np = None  # type: ignore[assignment]


class VectorIndex:
    """Cosine-similarity index over the `embedding` column of one table."""

    def __init__(self, table: str, where: str = "1=1"):
        self.table, self.where = table, where
        self._key: tuple | None = None
        self._ids: list[int] = []
        self._mat: Any = None
        self._lock = threading.Lock()

    def _load(self, db: Database) -> None:
        key = (id(db), db.scalar(f"SELECT COUNT(*) || ':' || COALESCE(MAX(id), 0) FROM {self.table} "
                                 f"WHERE embedding IS NOT NULL AND {self.where}"))
        if key == self._key:
            return
        rows = db.all(f"SELECT id, embedding FROM {self.table} WHERE embedding IS NOT NULL AND {self.where}")
        ids, vecs = [], []
        for r in rows:
            try:
                v = json.loads(r["embedding"])
            except ValueError:
                continue
            if v and (not vecs or len(v) == len(vecs[0])):
                ids.append(r["id"])
                vecs.append(v)
        if np is not None and vecs:
            m = np.asarray(vecs, dtype=np.float32)
            m /= np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
            self._mat = m
        else:
            self._mat = [_unit(v) for v in vecs]
        self._ids, self._key = ids, key

    def search(self, qvec: list[float], db: Database | None = None, k: int = 10,
               min_score: float = 0.0) -> list[tuple[int, float]]:
        db = db or get_db()
        with self._lock:
            self._load(db)
            if not self._ids:
                return []
            q = _unit(qvec)
            if np is not None:
                if len(q) != self._mat.shape[1]:
                    return []
                sims = self._mat @ np.asarray(q, dtype=np.float32)
                top = np.argsort(-sims)[:k]
                hits = [(self._ids[i], float(sims[i])) for i in top]
            else:
                if len(q) != len(self._mat[0]):
                    return []
                sims = [sum(a * b for a, b in zip(q, v)) for v in self._mat]
                order = sorted(range(len(sims)), key=lambda i: -sims[i])[:k]
                hits = [(self._ids[i], sims[i]) for i in order]
        return [(i, s) for i, s in hits if s >= min_score]


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


MEMORY_INDEX = VectorIndex("memories", "archived = 0")
CONVERSATION_INDEX = VectorIndex("conversations", "role = 'user'")


# ------------------------------------------------------------------ conversations
def _exchange(db: Database, row: dict[str, Any]) -> str:
    reply = db.one("SELECT content FROM conversations WHERE id > ? AND role = 'assistant' AND channel = ? "
                   "ORDER BY id LIMIT 1", (row["id"], row["channel"]))
    text = f"{row['created_at'][:10]} — you: {row['content']}"
    return text + (f"\nAMO: {reply['content'][:400]}" if reply else "")


def index_conversations(db: Database | None = None, batch: int = 40) -> int:
    """Embed conversations that aren't indexed yet (scheduler, when idle). Returns how many."""
    db = db or get_db()
    rows = db.all("SELECT id, channel, content, created_at FROM conversations WHERE role = 'user' "
                  "AND embedding IS NULL AND length(content) > 3 ORDER BY id DESC LIMIT ?", (batch,))
    llm, done = get_llm(), 0
    for r in rows:
        vec = llm.embed(_exchange(db, r))
        if not vec:  # no embedding model — stop quietly, keyword search still works
            break
        db.execute("UPDATE conversations SET embedding = ? WHERE id = ?", (json.dumps(vec), r["id"]))
        done += 1
    return done


_STOP = {"what", "did", "i", "say", "said", "about", "the", "a", "an", "we", "talk", "talked", "tell", "told",
         "you", "when", "was", "is", "my", "me", "do", "remember", "last", "time", "to", "of", "on", "for"}


def search_conversations(query: str, limit: int = 5, db: Database | None = None) -> list[dict[str, Any]]:
    """Past exchanges most related to `query`: by meaning when embeddings exist, else by keywords."""
    db = db or get_db()
    scores: dict[int, float] = {}
    qvec = get_llm().embed(query) if query.strip() else None
    if qvec:
        for cid, sim in CONVERSATION_INDEX.search(qvec, db, k=limit * 3, min_score=0.45):
            scores[cid] = sim
    words = [w for w in re.findall(r"[a-z0-9']+", query.lower()) if w not in _STOP and len(w) > 2]
    if words:
        like = " OR ".join("lower(content) LIKE ?" for _ in words)
        for r in db.all(f"SELECT id, content FROM conversations WHERE role = 'user' AND ({like}) "
                        "ORDER BY id DESC LIMIT 50", tuple(f"%{w}%" for w in words)):
            hit = sum(w in r["content"].lower() for w in words) / len(words)
            scores[r["id"]] = scores.get(r["id"], 0) + 0.5 * hit
    out = []
    for cid, score in sorted(scores.items(), key=lambda kv: -kv[1])[:limit]:
        row = db.one("SELECT id, channel, content, created_at FROM conversations WHERE id = ?", (cid,))
        if not row:
            continue
        reply = db.one("SELECT content FROM conversations WHERE id > ? AND role = 'assistant' AND channel = ? "
                       "ORDER BY id LIMIT 1", (cid, row["channel"]))
        out.append({"when": row["created_at"][:16].replace("T", " "), "you": row["content"],
                    "amo": (reply or {}).get("content", "")[:300], "score": round(score, 3)})
    return out
