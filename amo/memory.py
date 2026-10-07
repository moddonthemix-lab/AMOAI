"""Long-term memory: facts AMO knows about you and your businesses.

Search is hybrid: SQLite FTS5 keyword ranking, boosted by embedding cosine
similarity when an Ollama embedding model is available, plus importance and
recency. Everything still works with no embedding model installed.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from .db import Database, days_ago, get_db, now_iso
from .llm import get_llm

CATEGORIES = {
    "general", "preference", "person", "studio", "cravvr", "reselling", "trading", "goal", "health",
}

_WORD = re.compile(r"[A-Za-z0-9$%.']+")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "was", "it", "my",
    "me", "i", "you", "what", "how", "do", "does", "did", "with", "at", "be", "this", "that", "can",
    "about", "should", "would", "could", "have", "has", "from", "by", "as", "your", "we", "our",
}


def _fts_query(text: str) -> str:
    words = [w.strip(".'").lower() for w in _WORD.findall(text)]
    words = [w for w in words if w and w not in _STOP and len(w) > 1]
    # OR together quoted terms with prefix match: robust to punctuation in user text.
    return " OR ".join(f'"{w}"*' for w in dict.fromkeys(words))


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower().rstrip("."))


class Memory:
    def __init__(self, db: Database | None = None, use_embeddings: bool = True):
        self.db = db or get_db()
        self.use_embeddings = use_embeddings

    def add(
        self,
        content: str,
        category: str = "general",
        importance: int = 3,
        source: str = "manual",
    ) -> dict[str, Any]:
        content = content.strip()
        if not content:
            raise ValueError("memory content is empty")
        category = category if category in CATEGORIES else "general"
        importance = max(1, min(5, int(importance)))

        # De-duplicate: identical (normalized) fact already stored → bump it instead.
        norm = _normalize(content)
        for row in self.db.all(
            "SELECT id, content, importance FROM memories WHERE archived = 0 AND category = ?",
            (category,),
        ):
            if _normalize(row["content"]) == norm:
                self.db.update(
                    "memories", row["id"], {"importance": max(row["importance"], importance)}
                )
                return {**self.get(row["id"]), "duplicate": True}

        embedding = None
        if self.use_embeddings:
            vec = get_llm().embed(content)
            embedding = json.dumps(vec) if vec else None
        mem_id = self.db.insert(
            "memories",
            {
                "content": content,
                "category": category,
                "importance": importance,
                "source": source,
                "embedding": embedding,
                "created_at": now_iso(),
            },
        )
        return self.get(mem_id)

    def get(self, mem_id: int) -> dict[str, Any] | None:
        row = self.db.one(
            "SELECT id, content, category, importance, source, created_at, use_count "
            "FROM memories WHERE id = ?",
            (mem_id,),
        )
        return row

    def update(self, mem_id: int, **fields: Any) -> bool:
        allowed = {k: v for k, v in fields.items() if k in {"content", "category", "importance"}}
        return self.db.update("memories", mem_id, allowed)

    def forget(self, mem_id: int) -> bool:
        return self.db.update("memories", mem_id, {"archived": 1})

    def list(self, category: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = (
            "SELECT id, content, category, importance, source, created_at, use_count "
            "FROM memories WHERE archived = 0"
        )
        params: tuple = ()
        if category:
            sql += " AND category = ?"
            params = (category,)
        sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
        return self.db.all(sql, (*params, limit))

    def search(
        self, query: str, limit: int = 8, category: str | None = None, touch: bool = True
    ) -> list[dict[str, Any]]:
        scores: dict[int, float] = {}

        q = _fts_query(query)
        if q:
            sql = (
                "SELECT m.id, bm25(memories_fts) AS rank FROM memories_fts "
                "JOIN memories m ON m.id = memories_fts.rowid "
                "WHERE memories_fts MATCH ? AND m.archived = 0"
            )
            params: list[Any] = [q]
            if category:
                sql += " AND m.category = ?"
                params.append(category)
            sql += " ORDER BY rank LIMIT 50"
            for row in self.db.all(sql, tuple(params)):
                # bm25: lower is better (negative). Map to (0, 1].
                scores[row["id"]] = 1.0 / (1.0 + math.exp(row["rank"]))

        if self.use_embeddings and query.strip():
            qvec = get_llm().embed(query)
            if qvec:
                sql = "SELECT id, embedding FROM memories WHERE archived = 0 AND embedding IS NOT NULL"
                params2: tuple = ()
                if category:
                    sql += " AND category = ?"
                    params2 = (category,)
                for row in self.db.all(sql, params2):
                    sim = _cosine(qvec, json.loads(row["embedding"]))
                    if sim > 0.35:
                        scores[row["id"]] = scores.get(row["id"], 0.0) + sim

        if not scores:
            return []

        ids = list(scores)
        marks = ",".join("?" for _ in ids)
        rows = self.db.all(
            f"SELECT id, content, category, importance, source, created_at, use_count "
            f"FROM memories WHERE id IN ({marks})",
            tuple(ids),
        )
        for r in rows:
            r["score"] = round(scores[r["id"]] + 0.05 * r["importance"], 4)
        rows.sort(key=lambda r: r["score"], reverse=True)
        rows = rows[:limit]
        if touch and rows:
            with self.db.tx() as c:
                c.executemany(
                    "UPDATE memories SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
                    [(now_iso(), r["id"]) for r in rows],
                )
        return rows

    def core(self, limit: int = 10) -> list[dict[str, Any]]:
        """Always-on context: the most important facts (identity, preferences, rules)."""
        return self.db.all(
            "SELECT id, content, category, importance FROM memories "
            "WHERE archived = 0 AND importance >= 4 "
            "ORDER BY importance DESC, use_count DESC, created_at DESC LIMIT ?",
            (limit,),
        )

    def stats(self) -> dict[str, Any]:
        return {
            "total": self.db.scalar("SELECT COUNT(*) FROM memories WHERE archived = 0"),
            "by_category": {
                r["category"]: r["n"]
                for r in self.db.all(
                    "SELECT category, COUNT(*) AS n FROM memories WHERE archived = 0 GROUP BY category"
                )
            },
            "learned_this_week": self.db.scalar(
                "SELECT COUNT(*) FROM memories WHERE archived = 0 AND source != 'manual' "
                "AND created_at >= ?",
                (days_ago(7),),
            ),
        }
