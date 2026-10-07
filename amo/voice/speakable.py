"""Turn AMO's written replies into something that sounds natural when spoken.

  "2U-1-2D reversal"        → "two up, one, two down reversal"
  "AMZN (256.29)"           → "A M Z N, 256.29"
  "NQ=F" / "BTC-USD"        → "N Q futures" / "B T C"
  "$50/h ($150)"            → "50 dollars an hour, 150 dollars"
  "Fri Jan 4 at 7 PM"       → "Friday January 4th at 7 PM"
  "2030-01-04 19:00"        → "January 4th at 7 PM"
  "R:R 1.55"                → "risk to reward 1.55"
"""

from __future__ import annotations

import re
from datetime import datetime

_STRAT = {"1": "one", "2": "two", "3": "three", "2U": "two up", "2D": "two down"}
_DAYS = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday", "Fri": "Friday",
         "Sat": "Saturday", "Sun": "Sunday"}
_MONTHS = {"Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April", "Jun": "June", "Jul": "July",
           "Aug": "August", "Sep": "September", "Sept": "September", "Oct": "October", "Nov": "November",
           "Dec": "December"}
# Capitalised words that are words, not tickers to spell out.
_SAY_AS_WORD = {"AMO": "Amo", "NASDAQ": "Nasdaq", "OK": "OK", "AM": "AM", "PM": "PM", "CEO": "C E O",
                "FTFC": "full timeframe continuity", "TFC": "timeframe continuity", "USD": "dollars",
                "ETF": "E T F", "ATH": "all time high", "NOT": "not", "IN": "in", "UP": "up", "DOWN": "down",
                "TRIGGERED": "triggered", "FULL": "full", "TIMEFRAME": "timeframe", "CONTINUITY": "continuity",
                "LEANING": "leaning", "BULLISH": "bullish", "BEARISH": "bearish", "NEUTRAL": "neutral",
                "HIGH": "high", "LOW": "low", "BOTH": "both", "I": "I", "A": "A"}


def _ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _clock(h: int, m: int) -> str:
    hour = h % 12 or 12
    return f"{hour}{f':{m:02d}' if m else ''} {'AM' if h < 12 else 'PM'}"


def _iso_datetime(m: re.Match) -> str:
    try:
        d = datetime.fromisoformat(m.group(0).replace(" ", "T"))
    except ValueError:
        return m.group(0)
    text = f"{d:%B} {_ordinal(d.day)}"
    if m.group("time"):
        text += f" at {_clock(d.hour, d.minute)}"
    return text


def _strat_combo(m: re.Match) -> str:
    return ", ".join(_STRAT.get(p.upper(), p) for p in m.group(0).split("-"))


def _money(m: re.Match) -> str:
    whole = m.group(1)
    cents = m.group(2)
    text = f"{whole} dollars"
    if cents and int(cents):
        text += f" and {int(cents)} cents"
    return text


def _ticker(m: re.Match) -> str:
    word = m.group(0)
    if word in _SAY_AS_WORD:
        return _SAY_AS_WORD[word]
    return " ".join(word)  # AMZN → A M Z N


def to_speech(text: str) -> str:
    t = text
    # markdown, links, code, bullets
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", "a link", t)
    t = re.sub(r"[*_#>|~]", "", t)
    t = re.sub(r"^\s*[-•]\s+", "", t, flags=re.M)
    t = re.sub(r"[\U0001F000-\U0001FFFF☀-➿️]", "", t)

    # trading notation
    t = re.sub(r"\bR:R\b", "risk to reward", t)
    t = re.sub(r"=F\b(?: futures)?", " futures", t)
    t = re.sub(r"-USD\b", "", t)
    t = re.sub(r"\b[123][UD]?(?:-[123][UD]?)+\b", _strat_combo, t)       # 2U-1-2D
    t = re.sub(r"\b([23])([UD])\b", lambda m: _STRAT[m.group(0)], t)     # lone 2U / 2D
    t = re.sub(r"\bQ\b(?= [▲▼])|▲|▼", lambda m: {"▲": " up", "▼": " down"}.get(m.group(0), m.group(0)), t)

    # dates and times
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}(?P<time>[ T]\d{2}:\d{2}(?::\d{2})?)?\b", _iso_datetime, t)
    t = re.sub(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", lambda m: _DAYS[m.group(0)], t)
    t = re.sub(r"\b(Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec) (\d{1,2})\b",
               lambda m: f"{_MONTHS[m.group(1)]} {_ordinal(int(m.group(2)))}", t)

    # money and units
    t = re.sub(r"(\d) ?/ ?(?:h|hr|hour)\b", r"\1 an hour", t)
    t = re.sub(r"\$(\d[\d,]*)(?:\.(\d{2}))?", _money, t)
    t = re.sub(r"(\d)h\b", r"\1 hours", t)
    t = re.sub(r"#(\d+)", r"number \1", t)

    # symbols and punctuation that read badly
    t = re.sub(r"(\d)\s*[–—-]\s*(\d)", r"\1 to \2", t)                  # ranges 758.79–772.65
    t = t.replace("→", ", then ").replace("·", ",").replace("&", " and ")
    t = re.sub(r"\s*[—–]\s*", ", ", t)
    t = re.sub(r"\(([^)]*)\)", r", \1,", t)
    t = re.sub(r"\be\.g\.", "for example", t)
    t = re.sub(r"\bvs\.?\b", "versus", t)
    t = re.sub(r"[“”\"]", "", t)

    # tickers and all-caps words
    t = re.sub(r"\b[A-Z]{2,5}\b", _ticker, t)

    # tidy
    t = re.sub(r"\s+,", ",", t)
    t = re.sub(r",\s*([,.!?])", r"\1", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip(" ,")
