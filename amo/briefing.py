"""AMO's morning brief: weather, severe-weather alerts, your calendar, studio sessions, reminders,
goals and money — written from fixed templates (the AI model never invents details here) and
prepared ahead of time with the audio already rendered, so "good morning" plays instantly.

Free sources, no API keys:
  weather     Open-Meteo (open-meteo.com)
  alerts      US National Weather Service (api.weather.gov)
  calendars   your calendars' private iCal links (AMO_CALENDAR_URLS) — Apple iCloud, Google, Outlook…
  location    AMO_LOCATION ("Atlanta, GA") → Open-Meteo geocoding; if unset, a one-time IP lookup.
"""

from __future__ import annotations

import json
import logging
import random
import re
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from .config import settings
from .db import Database, get_db, local_now

log = logging.getLogger(__name__)
UA = {"User-Agent": "AMO personal assistant (local, non-commercial)"}
PREPARE_MINUTES_BEFORE = 45  # ready well before the brief hour
STALE_HOURS = 3              # asked for later in the day → refresh if older than this

# WMO weather codes (Open-Meteo) → words that fit "it's ___" / "there's ___"
_WMO = {
    0: ("clear", "it's"), 1: ("mostly clear", "it's"), 2: ("partly cloudy", "it's"), 3: ("overcast", "it's"),
    45: ("fog", "there's"), 48: ("freezing fog", "there's"),
    51: ("light drizzle", "there's"), 53: ("drizzle", "there's"), 55: ("heavy drizzle", "there's"),
    56: ("freezing drizzle", "there's"), 57: ("freezing drizzle", "there's"),
    61: ("light rain", "there's"), 63: ("rain", "there's"), 65: ("heavy rain", "there's"),
    66: ("freezing rain", "there's"), 67: ("freezing rain", "there's"),
    71: ("light snow", "there's"), 73: ("snow", "there's"), 75: ("heavy snow", "there's"), 77: ("snow grains", "there's"),
    80: ("rain showers", "there are"), 81: ("rain showers", "there are"), 82: ("heavy rain showers", "there are"),
    85: ("snow showers", "there are"), 86: ("heavy snow showers", "there are"),
    95: ("thunderstorms", "there are"), 96: ("thunderstorms with hail", "there are"),
    99: ("thunderstorms with hail", "there are"),
}


# ------------------------------------------------------------------ location
def location(db: Database | None = None) -> dict[str, Any] | None:
    """{"lat", "lon", "name"} — from AMO_LOCATION, else a one-time IP lookup; cached."""
    db = db or get_db()
    want = settings.location.strip()
    if want.lower() in ("none", "off"):
        return None
    cached = db.get_kv("brief_location")
    if cached:
        loc = json.loads(cached)
        if loc.get("query", "") == want:
            return loc
    loc = None
    try:
        if m := re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", want):
            loc = {"lat": float(m.group(1)), "lon": float(m.group(2)), "name": want}
        elif want:
            city, _, region = (p.strip() for p in want.partition(","))
            r = httpx.get("https://geocoding-api.open-meteo.com/v1/search",
                          params={"name": city, "count": 10, "language": "en"}, timeout=10)
            r.raise_for_status()
            results = r.json().get("results") or []
            region_l = region.lower()
            pick = next((x for x in results if region_l and region_l in (
                (x.get("admin1") or "").lower(), (x.get("country_code") or "").lower(), _state(x.get("admin1")))),
                results[0] if results else None)
            if pick:
                loc = {"lat": pick["latitude"], "lon": pick["longitude"],
                       "name": ", ".join(p for p in (pick.get("name"), pick.get("admin1")) if p)}
        else:
            r = httpx.get("https://ipapi.co/json/", timeout=10, headers=UA)
            r.raise_for_status()
            j = r.json()
            if j.get("latitude") is not None:
                loc = {"lat": j["latitude"], "lon": j["longitude"], "name": j.get("city") or "", "guessed": True}
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.info("brief: couldn't work out the location (%s)", type(e).__name__)
    if loc:
        loc["query"] = want
        db.set_kv("brief_location", json.dumps(loc))
    return loc


_STATES = {"alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca", "colorado": "co",
           "connecticut": "ct", "delaware": "de", "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
           "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks", "kentucky": "ky", "louisiana": "la",
           "maine": "me", "maryland": "md", "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
           "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
           "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny", "north carolina": "nc",
           "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or", "pennsylvania": "pa",
           "rhode island": "ri", "south carolina": "sc", "south dakota": "sd", "tennessee": "tn", "texas": "tx",
           "utah": "ut", "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv",
           "wisconsin": "wi", "wyoming": "wy", "district of columbia": "dc"}


def _state(admin1: str | None) -> str:
    return _STATES.get((admin1 or "").lower(), "")


# ------------------------------------------------------------------ weather
def forecast(loc: dict[str, Any], db: Database | None = None) -> dict[str, Any] | None:
    """Today's weather (Open-Meteo). A copy is kept for 6 hours in case the internet is down."""
    db = db or get_db()
    f_unit = settings.temp_unit.upper().startswith("F")
    try:
        r = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=10, params={
            "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto", "forecast_days": 1,
            "temperature_unit": "fahrenheit" if f_unit else "celsius",
            "current": "temperature_2m,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                     "uv_index_max,sunrise,sunset"})
        r.raise_for_status()
        j = r.json()
        d = j["daily"]
        w = {"now": round(j["current"]["temperature_2m"]), "now_code": j["current"]["weather_code"],
             "code": d["weather_code"][0], "high": round(d["temperature_2m_max"][0]),
             "low": round(d["temperature_2m_min"][0]), "rain": d["precipitation_probability_max"][0] or 0,
             "uv": d["uv_index_max"][0] or 0, "sunset": d["sunset"][0][11:16], "at": local_now().isoformat()}
        db.set_kv("brief_weather", json.dumps(w))
        return w
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as e:
        log.info("brief: weather unavailable (%s)", type(e).__name__)
        raw = db.get_kv("brief_weather")
        if raw:
            w = json.loads(raw)
            if datetime.fromisoformat(w["at"]) > local_now() - timedelta(hours=6):
                return w
        return None


def alerts(loc: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Active NWS watches/warnings for the location, most severe first (None if unreachable)."""
    try:
        r = httpx.get("https://api.weather.gov/alerts/active", timeout=10, headers=UA,
                      params={"point": f"{loc['lat']:.4f},{loc['lon']:.4f}"})
        r.raise_for_status()
        out = []
        for f in r.json().get("features", []):
            p = f.get("properties", {})
            if p.get("status") != "Actual" or p.get("messageType") == "Cancel":
                continue
            out.append({"id": p.get("id") or f.get("id"), "event": p.get("event", "Weather alert"),
                        "severity": p.get("severity", "Unknown"), "ends": p.get("ends") or p.get("expires"),
                        "headline": p.get("headline", "")})
        order = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3}
        seen, unique = set(), []
        for a in sorted(out, key=lambda a: order.get(a["severity"], 4)):
            if a["event"] not in seen:  # several offices issue the same warning
                seen.add(a["event"])
                unique.append(a)
        return unique
    except (httpx.HTTPError, ValueError) as e:
        log.info("brief: alerts unavailable (%s)", type(e).__name__)
        return None


def _until(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(iso)
        from zoneinfo import ZoneInfo

        t = t.astimezone(ZoneInfo(settings.timezone))
    except (ValueError, KeyError):
        return ""
    return f" until {_clock(t.hour, t.minute)}"


def _clock(h: int, m: int) -> str:
    if (h, m) == (12, 0):
        return "noon"
    return f"{h % 12 or 12}{f':{m:02d}' if m else ''} {'AM' if h < 12 else 'PM'}"


def _hm(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    return _clock(h, m)


# ------------------------------------------------------------------ calendars
def calendar_events(day: date) -> list[dict[str, Any]]:
    from .calendar_feed import get_events

    out = []
    for url in (u.strip() for u in settings.calendar_urls.split(",")):
        if url:
            out += get_events(url, day, tz=settings.timezone)
    return sorted(out, key=lambda x: (x["time"] is not None, x["time"] or ""))


# ------------------------------------------------------------------ the script
def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def build(db: Database | None = None, now: datetime | None = None, *, weather: dict | None = None,
          warnings: list | None = None, events: list | None = None, fetch: bool = True) -> dict[str, Any]:
    """The brief as {"parts": [{"key", "text"}], "text": spoken paragraph}. Templates only."""
    from .finance import dashboard
    from .quick_answers import holidays

    db = db or get_db()
    now = now or local_now()
    owner = settings.owner_name
    parts: list[dict[str, str]] = []

    def add(key: str, text: str) -> None:
        if text:
            parts.append({"key": key, "text": text.strip()})

    loc = location(db) if fetch else None
    if fetch and loc:
        weather = forecast(loc, db) if weather is None else weather
        warnings = alerts(loc) if warnings is None else warnings
    if fetch and events is None:
        events = calendar_events(now.date())

    hello = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 17 else "Good evening"
    add("hello", f"{hello}, {owner}. It's {now:%A}, {now:%B} {now.day}.")

    # Severe weather first — it matters most.
    serious = [a for a in (warnings or []) if a["severity"] in ("Extreme", "Severe")]
    if serious:
        add("alerts", "Heads up: " + _join([f"a {a['event']}{_until(a['ends'])}" for a in serious[:2]]) + ".")
    elif warnings:
        add("alerts", f"There's a {warnings[0]['event']}{_until(warnings[0]['ends'])}.")

    if weather:
        now_word, verb = _WMO.get(weather["now_code"], ("", "it's"))
        day_word, _ = _WMO.get(weather["code"], ("", ""))
        line = f"Right now {verb} {now_word}, {weather['now']} degrees" if now_word else \
            f"It's {weather['now']} degrees right now"
        line += f". Today: {day_word}, a high of {weather['high']} and a low of {weather['low']}" if day_word else \
            f". Today's high is {weather['high']}, low {weather['low']}"
        line += "."
        if weather["rain"] >= 50:
            line += f" {weather['rain']}% chance of rain — grab an umbrella."
        elif weather["rain"] >= 30:
            line += f" {weather['rain']}% chance of rain."
        if weather["uv"] >= 7:
            line += " The UV is high, so sunscreen if you're out."
        add("weather", line)

    # Holidays today or coming up.
    for d, name in sorted({**holidays(now.year), **holidays(now.year + 1)}.items()):
        days = (d - now.date()).days
        if days == 0:
            add("holiday", f"It's {name} today.")
        elif 1 <= days <= 3:
            add("holiday", f"{name} is {'tomorrow' if days == 1 else 'on ' + d.strftime('%A')}.")

    # Your calendar.
    if events:
        spoken = []
        for e in events[:4]:
            spoken.append(f"{e['title']} at {_hm(e['time'])}" if e["time"] else f"{e['title']} (all day)")
        more = f", and {len(events) - 4} more" if len(events) > 4 else ""
        add("calendar", f"On your calendar: {_join(spoken)}{more}.")

    d = dashboard(db)
    # Studio.
    sessions = db.all("SELECT s.starts_at, s.hours, s.service, c.name, c.artist_name FROM studio_sessions s "
                      "JOIN clients c ON c.id = s.client_id WHERE substr(s.starts_at, 1, 10) = ? "
                      "AND s.status = 'booked' ORDER BY s.starts_at", (now.date().isoformat(),))
    if sessions:
        def one(s: dict) -> str:
            at = f" at {_hm(s['starts_at'][11:16])}" if len(s["starts_at"]) > 10 else ""
            return f"{s['artist_name'] or s['name']}{at} ({s['hours']:g} hours)" if s["hours"] else \
                f"{s['artist_name'] or s['name']}{at}"
        add("studio", "In the studio: " + _join([one(s) for s in sessions]) + ".")
    if d["studio"]["unpaid"]:
        n = d["studio"]["unpaid"]
        add("unpaid", f"{n} session{'s' if n != 1 else ''} still unpaid.")

    # Reminders today.
    today_end = datetime.combine(now.date(), datetime.max.time()).isoformat()
    rem = db.all("SELECT * FROM timers WHERE status = 'active' AND kind != 'timer' AND due_at <= ? "
                 "ORDER BY due_at", (today_end,))
    if rem:
        items = [f"{r['message'] or 'a reminder'} at {_clock(int(r['due_at'][11:13]), int(r['due_at'][14:16]))}"
                 for r in rem[:3]]
        add("reminders", "Reminders: " + _join(items) + ".")

    # Goals.
    todo = [g["title"] for g in d["goals"] if not g["done"]]
    if todo:
        add("goals", "Daily goals: " + _join(todo[:4]) + ("." if len(todo) <= 4 else f", plus {len(todo) - 4} more."))

    # Money.
    rev = d["revenue"]
    if rev.get("month_target"):
        add("money", f"You're at ${rev['month']:,.0f} of your ${rev['month_target']:,.0f} target this month "
                     f"({rev['month_pct'] * 100:.0f}%).")
    elif rev["month"]:
        add("money", f"${rev['month']:,.0f} made so far this month.")
    if d["trading"]["open_positions"]:
        n = d["trading"]["open_positions"]
        add("trading", f"{n} open trade{'s' if n != 1 else ''}.")
    if d["reselling"]["stale"]:
        add("reselling", f"{d['reselling']['stale']} resale item(s) have sat 30+ days — maybe drop the price.")
    if d["cravvr"]["open_tasks"]:
        add("cravvr", f"Cravvr has {d['cravvr']['open_tasks']} open task(s).")

    if len(parts) == 1:
        add("empty", "Nothing on the books today — it's all yours.")
    rnd = random.Random(now.date().toordinal())  # same sign-off if you ask twice
    add("bye", rnd.choice(["Let's get it.", "Have a good one.", "Make it count.", "Go get it.", "Let's work."]))
    return {"parts": parts, "text": " ".join(p["text"] for p in parts), "date": now.date().isoformat(),
            "weather": bool(weather), "location": (loc or {}).get("name", "")}


# ------------------------------------------------------------------ prepared ahead
def prepare(db: Database | None = None, now: datetime | None = None, render: bool = True) -> dict[str, Any]:
    """Build today's brief and render its audio (cached on disk, so speaking it is instant)."""
    db = db or get_db()
    now = now or local_now()
    b = build(db, now)
    b["prepared_at"] = now.isoformat(timespec="seconds")
    if render:
        try:
            from .voice.tts import synthesize

            synthesize(b["text"])  # lands in the speech cache — the listener/device get it instantly
            b["audio"] = True
        except Exception as e:  # noqa: BLE001 — no voice on this machine: text still works
            log.info("brief: audio not pre-rendered (%s)", e)
    db.set_kv("brief", json.dumps(b))
    return b


def today(db: Database | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Today's prepared brief, refreshed if it's from another day, stale, or missing the weather."""
    db = db or get_db()
    now = now or local_now()
    raw = db.get_kv("brief")
    if raw:
        b = json.loads(raw)
        fresh = datetime.fromisoformat(b["prepared_at"]) > now - timedelta(hours=STALE_HOURS)
        if b["date"] == now.date().isoformat() and fresh:
            return b
    return prepare(db, now, render=False)


def due_to_prepare(db: Database, now: datetime, brief_hour: int) -> bool:
    """Scheduler: prepare once a day, shortly before the brief hour; retry if the weather was missing."""
    start = datetime.combine(now.date(), datetime.min.time()) + timedelta(hours=brief_hour,
                                                                         minutes=-PREPARE_MINUTES_BEFORE)
    if now < start or now.hour >= 12:
        return False
    raw = db.get_kv("brief")
    if not raw:
        return True
    b = json.loads(raw)
    if b["date"] != now.date().isoformat():
        return True
    retry_at = datetime.fromisoformat(b["prepared_at"]) + timedelta(minutes=15)
    return not b.get("weather") and bool(settings.location or db.get_kv("brief_location")) and now >= retry_at


# ------------------------------------------------------------------ severe weather, all day
def watch_alerts(db: Database, now: datetime) -> int:
    """Every 15 minutes: say new Severe/Extreme alerts once (Extreme ones even in quiet hours)."""
    from .proactive import announce

    last = db.get_kv("alerts_checked")
    if last and datetime.fromisoformat(last) > now - timedelta(minutes=15):
        return 0
    db.set_kv("alerts_checked", now.isoformat(timespec="seconds"))
    loc = location(db) if (settings.location or db.get_kv("brief_location")) else None
    if not loc:
        return 0
    said = 0
    for a in alerts(loc) or []:
        if a["severity"] not in ("Extreme", "Severe"):
            continue
        kind = "urgent" if a["severity"] == "Extreme" else "alert"
        said += announce(f"Weather alert: a {a['event']}{_until(a['ends'])}.", kind, f"nws:{a['id']}",
                         ttl_minutes=120, db=db, now=now)
    return said
