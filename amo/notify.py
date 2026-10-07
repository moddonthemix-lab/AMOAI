"""Notifications: stored in the DB (shown on the dashboard / ESP32) and optionally
pushed to your phone through a self-hosted ntfy server (https://ntfy.sh)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import settings
from .db import Database, get_db, now_iso

log = logging.getLogger(__name__)

_PRIORITY = {"info": "default", "warn": "high", "urgent": "urgent"}


def notify(title: str, body: str = "", level: str = "info", push: bool = True,
           db: Database | None = None) -> dict[str, Any]:
    db = db or get_db()
    level = level if level in _PRIORITY else "info"
    nid = db.insert("notifications", {"title": title, "body": body, "level": level,
                                      "created_at": now_iso()})
    if push and settings.ntfy_url:
        try:
            httpx.post(
                f"{settings.ntfy_url.rstrip('/')}/{settings.ntfy_topic}",
                content=body.encode() or title.encode(),
                headers={"Title": title, "Priority": _PRIORITY[level], "Tags": "robot"},
                timeout=10,
            )
        except httpx.HTTPError as e:  # never let a push failure break the caller
            log.warning("ntfy push failed: %s", e)
    return db.one("SELECT * FROM notifications WHERE id = ?", (nid,))


def mark_read(notification_id: int | None = None, db: Database | None = None) -> int:
    db = db or get_db()
    if notification_id is None:
        return db.execute("UPDATE notifications SET read = 1 WHERE read = 0")
    return db.execute("UPDATE notifications SET read = 1 WHERE id = ?", (notification_id,))
