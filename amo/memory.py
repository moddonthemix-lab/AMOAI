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


_NUM = re.compile(r"\$?\d+(?:[.,]\d+)?%?|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|twenty|thirty|"
                  r"forty|fifty|hundred|thousand)\b", re.I)
_DAYS = re.compile(r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)days?\b|\b(?:am|pm)\b", re.I)
_FILLER = {"now", "actually", "anymore", "currently", "these", "days", "no", "longer", "not"}


def _words(text: str) -> set[str]:
    """Meaningful words, without numbers, days and filler — the *subject* of a fact."""
    t = _DAYS.sub(" ", _NUM.sub(" ", text.lower().replace("’", "'")))
    return {w.strip(".'s") for w in _WORD.findall(t)} - _STOP - _FILLER - {""}


def _values(text: str) -> set[str]:
    """The changeable parts of a fact: numbers, prices, days, times."""
    return {v.lower().replace(",", "") for v in _NUM.findall(text) + _DAYS.findall(text)}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def replaces(new: str, old: str) -> bool:
    """Is `new` an updated version of `old`? ("Jay's rate is $60 now" vs "Jay's rate is $50")
    Same subject, different number / price / day / time."""
    va, vb = _values(new), _values(old)
    return bool(va and vb and va != vb and _jaccard(_words(new), _words(old)) >= 0.6)


def near_duplicate(a: str, b: str) -> bool:
    """Same fact, worded a little differently (and no conflicting numbers)."""
    return _values(a) == _values(b) and _jaccard(_words(a), _words(b)) >= 0.8


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

        # Updated facts replace the old version instead of piling up next to it.
        replaced = [r for r in self.db.all(
            "SELECT id, content, importance FROM memories WHERE archived = 0 AND category IN (?, 'general', "
            "'person', 'preference')", (category,))
            if replaces(content, r["content"]) or near_duplicate(content, r["content"])]

        embedding = None
        if self.use_embeddings:
            vec = get_llm().embed(content)
            embedding = json.dumps(vec) if vec else None
        if replaced:
            importance = max([importance] + [r["importance"] for r in replaced])
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
        for r in replaced:
            self.db.update("memories", r["id"], {"archived": 1, "superseded_by": mem_id})
        out = self.get(mem_id)
        if replaced:
            out["replaced"] = [r["content"] for r in replaced]
        return out

    def tidy(self) -> dict[str, int]:
        """Nightly: merge facts that say the same thing, and let newer versions replace older ones."""
        rows = self.db.all("SELECT id, content, category, importance FROM memories WHERE archived = 0 ORDER BY id")
        merged = 0
        alive = {r["id"]: r for r in rows}
        for i, old in enumerate(rows):
            if old["id"] not in alive:
                continue
            for new in rows[i + 1:]:
                if new["id"] not in alive or new["category"] != old["category"]:
                    continue
                if replaces(new["content"], old["content"]) or near_duplicate(new["content"], old["content"]):
                    self.db.update("memories", new["id"], {"importance": max(new["importance"], old["importance"])})
                    self.db.update("memories", old["id"], {"archived": 1, "superseded_by": new["id"]})
                    alive.pop(old["id"])
                    merged += 1
                    break
        return {"merged": merged}

    def stale(self, days: int = 90, limit: int = 5) -> list[dict[str, Any]]:
        """Facts with numbers/prices/days that haven't come up in a while — worth double-checking."""
        rows = self.db.all(
            "SELECT id, content, created_at, last_used_at FROM memories WHERE archived = 0 "
            "AND COALESCE(last_used_at, created_at) < ? ORDER BY importance DESC, created_at", (days_ago(days),))
        return [r for r in rows if _values(r["content"])][:limit]

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
                from .recall import MEMORY_INDEX

                hits = MEMORY_INDEX.search(qvec, self.db, k=50, min_score=0.35)
                if category and hits:
                    keep = {r["id"] for r in self.db.all(
                        f"SELECT id FROM memories WHERE category = ? AND id IN ({','.join('?' * len(hits))})",
                        (category, *[i for i, _ in hits]))}
                    hits = [(i, sim) for i, sim in hits if i in keep]
                for mid, sim in hits:
                    scores[mid] = scores.get(mid, 0.0) + sim

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
            "ORDER BY importance DESC, id DESC LIMIT ?",  # stable order: keeps the prompt cacheable
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
