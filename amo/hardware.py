"""Hardware control: AMO's hands. ESP32 boards and Raspberry Pis register their parts with the
brain and take commands from it over Wi-Fi (plain HTTP + JSON — easy for an ESP32).

    device → POST /api/hw/hello   {"device": "desk", "kind": "esp32",
                                   "parts": [{"id": "fan", "type": "switch"},
                                             {"id": "light", "type": "rgb", "name": "desk light"},
                                             {"id": "head", "type": "servo"},
                                             {"id": "temp", "type": "sensor", "unit": "°C"}]}
    device → GET  /api/hw/poll?device=desk&wait=20      (waits up to 20 s for commands)
             ← {"commands": [{"id": 7, "part": "fan", "action": "on", "value": null}]}
    device → POST /api/hw/report  {"device": "desk", "state": {"fan": true, "temp": 23.4},
                                   "done": [{"id": 7, "ok": true}]}

Part types and what they accept:
  switch   on | off | toggle                  (relay, lamp, fan, LED)
  rgb      on | off | set value="#ff0000" or a colour name, or brightness 0-100
  servo    set value=0..180 (degrees)
  motor    set value=-100..100 (speed, negative = reverse) | off
  buzzer   set value="beep" | a frequency in Hz
  display  set value="text to show"
  sensor   read only — reports a number (temperature, humidity, light, distance…)

A command only counts as done when the device confirms it, so AMO never says "the fan is on"
unless it really is.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any

from .db import Database, get_db, local_now, now_iso

ONLINE_SECONDS = 60
EXPIRE_SECONDS = 120  # a command an offline device hasn't picked up by then is dropped (no garage door at 3am)
# Parts that must be named exactly — never picked by a partial match.
SENSITIVE = re.compile(r"\b(?:lock|door|garage|gate|relay|oven|stove|heater|alarm|valve)\b")
TYPES = {"switch", "rgb", "servo", "motor", "buzzer", "display", "sensor"}
COLOURS = {
    "red": "#ff0000", "green": "#00ff00", "blue": "#0000ff", "white": "#ffffff", "warm white": "#ffb46b",
    "yellow": "#ffd000", "orange": "#ff7a00", "purple": "#8a2be2", "pink": "#ff4fa3", "cyan": "#00e5ff",
    "teal": "#00b3a4", "magenta": "#ff00ff",
}


# ------------------------------------------------------------------ registry
def hello(device: str, parts: list[dict[str, Any]], kind: str = "esp32", ip: str | None = None,
          state: dict[str, Any] | None = None, db: Database | None = None) -> dict[str, Any]:
    db = db or get_db()
    name = device.strip().lower()
    clean = []
    for p in parts:
        ptype = str(p.get("type", "switch")).lower()
        if ptype not in TYPES or not p.get("id"):
            continue
        clean.append({"id": str(p["id"]).lower(), "type": ptype, "name": str(p.get("name") or p["id"]).lower(),
                      "unit": p.get("unit", "")})
    row = db.one("SELECT id, state FROM hw_devices WHERE name = ?", (name,))
    merged = {**(json.loads(row["state"]) if row else {}), **(state or {})}
    if row:
        db.update("hw_devices", row["id"], {"kind": kind, "parts": json.dumps(clean), "ip": ip,
                                            "state": json.dumps(merged), "last_seen": now_iso()})
    else:
        db.insert("hw_devices", {"name": name, "kind": kind, "parts": json.dumps(clean), "ip": ip,
                                 "state": json.dumps(merged), "last_seen": now_iso(), "created_at": now_iso()})
    return {"ok": True, "device": name, "parts": [p["id"] for p in clean]}


def devices(db: Database | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    db = db or get_db()
    now = now or local_now()
    out = []
    for d in db.all("SELECT * FROM hw_devices ORDER BY name"):
        seen = datetime.fromisoformat(d["last_seen"]) if d["last_seen"] else None
        out.append({"name": d["name"], "kind": d["kind"], "ip": d["ip"],
                    "online": bool(seen and now - seen < timedelta(seconds=ONLINE_SECONDS)),
                    "last_seen": d["last_seen"], "parts": json.loads(d["parts"]), "state": json.loads(d["state"])})
    return out


def _norm(text: str) -> str:
    t = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    t = re.sub(r"\b(the|my|a|an|on|in|of)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def resolve(target: str, db: Database | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """'fan', 'desk light', 'the light on the desk', 'desk/fan' → (device, part). Raises if unclear."""
    want = _norm(target.replace("/", " ").replace(".", " "))
    if not want:
        raise ValueError("which device?")
    found: list[tuple[int, dict, dict]] = []
    for d in devices(db):
        for p in d["parts"]:
            names = {_norm(p["id"]), _norm(p["name"]), _norm(f"{d['name']} {p['id']}"),
                     _norm(f"{d['name']} {p['name']}"), _norm(f"{p['name']} {d['name']}")}
            if want in names:
                found.append((3, d, p))
            elif not SENSITIVE.search(" ".join(names)) and any(
                    n and (set(n.split()) <= set(want.split()) or set(want.split()) <= set(n.split())
                           or (p["type"] == "sensor" and (n in want or want in n))) for n in names):
                found.append((1, d, p))  # whole words ("fan" ≠ "fancy lamp"); sensors are only read, so looser
    if not found:
        have = ", ".join(f"{p['name']} ({d['name']})" for d in devices(db) for p in d["parts"]) or "nothing yet"
        raise ValueError(f"I don't have a device called '{target}'. Connected: {have}.")
    best = max(score for score, _, _ in found)
    top = [(d, p) for score, d, p in found if score == best]
    if len(top) > 1:
        raise ValueError("which one? " + "; ".join(f"{p['name']} on {d['name']}" for d, p in top))
    return top[0]


# ------------------------------------------------------------------ commands
def _value_for(part: dict[str, Any], action: str, value: Any) -> tuple[str, Any]:
    action = (action or "set").lower().strip()
    t = part["type"]
    if t == "sensor":
        raise ValueError(f"the {part['name']} is a sensor — I can read it, not control it")
    if t == "switch" and action == "set":  # "set the fan to on" — a set value, never a toggle
        v = str(value).lower().strip() if value is not None else ""
        action = {"on": "on", "true": "on", "1": "on", "off": "off", "false": "off", "0": "off"}.get(v, "")
        if not action:
            raise ValueError(f"the {part['name']} can only be turned on or off")
    if action not in ("on", "off", "toggle", "set", "brightness"):
        raise ValueError(f"I can't '{action}' the {part['name']}")
    if action in ("on", "off", "toggle"):
        if t in ("servo", "display", "buzzer"):
            raise ValueError(f"the {part['name']} can't be switched {action}")
        return action, None
    if t == "rgb":
        v = str(value or "").lower().strip()
        if v in COLOURS:
            return "set", COLOURS[v]
        if re.fullmatch(r"#?[0-9a-f]{6}", v):
            return "set", "#" + v.lstrip("#")
        m = re.fullmatch(r"(\d{1,3})\s*%?", v)
        if m:
            return "brightness", max(0, min(100, int(m.group(1))))
        raise ValueError(f"what colour? (e.g. {', '.join(list(COLOURS)[:6])})")
    if t in ("servo", "motor"):
        try:
            num = int(float(value))
        except (TypeError, ValueError):
            raise ValueError(f"what {'angle' if t == 'servo' else 'speed'} for the {part['name']}?") from None
        return "set", max(0, min(180, num)) if t == "servo" else max(-100, min(100, num))
    if t == "buzzer":
        return "set", value or "beep"
    if t == "display":
        return "set", str(value or "")[:120]
    return "set", value


def command(target: str, action: str = "set", value: Any = None, db: Database | None = None,
            wait: float = 4.0) -> dict[str, Any]:
    """Queue a command and wait briefly for the device to confirm it."""
    db = db or get_db()
    dev, part = resolve(target, db)
    action, value = _value_for(part, action, value)
    cid = db.insert("hw_commands", {"device": dev["name"], "part": part["id"], "action": action,
                                    "value": json.dumps(value), "created_at": now_iso()})
    base = {"device": dev["name"], "part": part["id"], "name": part["name"], "action": action, "value": value,
            "command_id": cid}
    if not dev["online"]:
        return {**base, "done": False, "status": "pending",
                "note": f"{dev['name']} is offline — it'll do it if it's back within {EXPIRE_SECONDS // 60} minutes, "
                        "otherwise I'll drop it"}
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        row = db.one("SELECT status, result FROM hw_commands WHERE id = ?", (cid,))
        if row["status"] in ("done", "failed"):
            if row["status"] == "failed":
                return {**base, "done": False, "status": "failed", "error": row["result"] or "the device refused"}
            return {**base, "done": True, "status": "done"}
        time.sleep(0.1)
    return {**base, "done": False, "status": "sent", "note": "sent — the device hasn't confirmed yet"}


def read(target: str, db: Database | None = None) -> dict[str, Any]:
    dev, part = resolve(target, db)
    return {"device": dev["name"], "part": part["id"], "name": part["name"], "value": dev["state"].get(part["id"]),
            "unit": part.get("unit", ""), "online": dev["online"], "as_of": dev["last_seen"]}


# ------------------------------------------------------------------ the device side of the protocol
def poll(device: str, wait: float = 0, db: Database | None = None) -> list[dict[str, Any]]:
    """Commands waiting for this device (marks them sent). Long-polls up to `wait` seconds."""
    db = db or get_db()
    name = device.strip().lower()
    deadline = time.monotonic() + max(0.0, min(wait, 30.0))
    db.execute("UPDATE hw_devices SET last_seen = ? WHERE name = ?", (now_iso(), name))
    stale = (local_now() - timedelta(seconds=EXPIRE_SECONDS)).isoformat()
    db.execute("UPDATE hw_commands SET status = 'expired' WHERE device = ? AND status = 'pending' AND created_at < ?",
               (name, stale))
    while True:
        rows = db.all("SELECT id, part, action, value FROM hw_commands WHERE device = ? AND status = 'pending' "
                      "ORDER BY id", (name,))
        if rows or time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    if wait:  # still here after the long wait
        db.execute("UPDATE hw_devices SET last_seen = ? WHERE name = ?", (now_iso(), name))
    if rows:
        marks = ",".join("?" * len(rows))
        db.execute(f"UPDATE hw_commands SET status = 'sent' WHERE id IN ({marks})", tuple(r["id"] for r in rows))
    return [{"id": r["id"], "part": r["part"], "action": r["action"], "value": json.loads(r["value"] or "null")}
            for r in rows]


def report(device: str, state: dict[str, Any] | None = None, done: list[dict[str, Any]] | None = None,
           db: Database | None = None) -> dict[str, Any]:
    db = db or get_db()
    name = device.strip().lower()
    row = db.one("SELECT id, state FROM hw_devices WHERE name = ?", (name,))
    if not row:
        raise ValueError(f"unknown device {name} — say hello first")
    merged = {**json.loads(row["state"]), **(state or {})}
    db.update("hw_devices", row["id"], {"state": json.dumps(merged), "last_seen": now_iso()})
    for d in done or []:
        db.execute("UPDATE hw_commands SET status = ?, result = ?, done_at = ? WHERE id = ? AND device = ?",
                   ("done" if d.get("ok", True) else "failed", d.get("error") or d.get("result"),
                    now_iso(), d["id"], name))
    return {"ok": True}
