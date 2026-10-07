"""Market data (Yahoo Finance chart API — free, no key): candles for any stock, ETF,
index, future or crypto, on 60-minute, daily, weekly, monthly and quarterly timeframes."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

# Yahoo rejects full browser user-agents (429) but accepts a plain one.
YAHOO_UA = {"User-Agent": "Mozilla/5.0"}

# Friendly names and futures/crypto roots → Yahoo symbols.
ALIASES = {
    "NQ": "NQ=F", "MNQ": "MNQ=F", "ES": "ES=F", "MES": "MES=F", "YM": "YM=F", "RTY": "RTY=F",
    "CL": "CL=F", "OIL": "CL=F", "CRUDE": "CL=F", "GC": "GC=F", "GOLD": "GC=F", "SI": "SI=F", "SILVER": "SI=F",
    "BTC": "BTC-USD", "BITCOIN": "BTC-USD", "ETH": "ETH-USD", "ETHEREUM": "ETH-USD", "SOL": "SOL-USD",
    "SPX": "^GSPC", "S&P": "SPY", "S AND P": "SPY", "SP500": "SPY", "NASDAQ": "QQQ", "NDX": "^NDX",
    "DOW": "DIA", "VIX": "^VIX", "DXY": "DX-Y.NYB", "DOLLAR": "DX-Y.NYB", "RUSSELL": "IWM",
    "APPLE": "AAPL", "TESLA": "TSLA", "NVIDIA": "NVDA", "MICROSOFT": "MSFT", "AMAZON": "AMZN",
    "GOOGLE": "GOOGL", "META": "META", "NETFLIX": "NFLX", "AMD": "AMD",
}

# Weekly/monthly/quarterly candles are built from daily data: Yahoo's own 1wk/1mo series append a
# separate bar for the latest session, which would make the "current month" look like one day.
INTRADAY = ("60m", "1mo")
DAILY = ("1d", "10y")
_cache: dict[str, tuple[float, Any]] = {}


@dataclass
class Bar:
    t: int        # unix seconds (bar start)
    o: float
    h: float
    l: float  # noqa: E741
    c: float

    def as_dict(self) -> dict[str, Any]:
        return {"t": self.t, "o": self.o, "h": self.h, "l": self.l, "c": self.c}


def normalize(symbol: str) -> str:
    s = symbol.strip().upper().lstrip("$")
    return ALIASES.get(s, s)


def lookup(name: str) -> str:
    """Resolve a company/asset name ("tesla", "nasdaq") or ticker to a Yahoo symbol."""
    s = normalize(name)
    if s != name.strip().upper().lstrip("$") or re.fullmatch(r"[\^A-Z0-9.=\-]{1,10}", s):
        return s
    r = httpx.get("https://query1.finance.yahoo.com/v1/finance/search", params={"q": name, "quotesCount": 1},
                  headers=YAHOO_UA, timeout=15)
    quotes = r.json().get("quotes") or []
    if not quotes:
        raise ValueError(f"I couldn't find a ticker for “{name}”")
    return quotes[0]["symbol"]


def _span(interval: str) -> int:
    return {"60m": 3600, "1d": 86400}.get(interval, 86400)


def _fetch(symbol: str, interval: str, rng: str) -> tuple[list[Bar], dict[str, Any]]:
    params = {"interval": interval, "range": rng, "includePrePost": "false"}
    for host in ("query1", "query2"):
        r = httpx.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}",
                      params=params, headers=YAHOO_UA, timeout=20)
        if r.status_code != 429:
            break
        time.sleep(1)
    if r.status_code == 404:
        raise ValueError(f"no market data for {symbol} — check the ticker")
    r.raise_for_status()
    res = (r.json().get("chart", {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        raise ValueError(f"no market data for {symbol}")
    q = res["indicators"]["quote"][0]
    bars = [Bar(t, o, h, l, c) for t, o, h, l, c in zip(res["timestamp"], q["open"], q["high"], q["low"], q["close"])
            if None not in (o, h, l, c)]
    # Yahoo sometimes appends a zero-range "last trade" bar after the real latest bar — drop it.
    if len(bars) > 1 and bars[-1].h == bars[-1].l and bars[-1].t - bars[-2].t < _span(interval):
        bars.pop()
    return bars, res.get("meta", {})


def candles(symbol: str, tf: str = "D") -> tuple[list[Bar], dict[str, Any]]:
    """Candles for a timeframe: 60, D, W, M or Q (quarterly is built from monthly). Cached 60s."""
    symbol = normalize(symbol)
    key = f"{symbol}:{tf}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    if tf == "60":
        out = _fetch(symbol, *INTRADAY)
    elif tf == "D":
        out = _fetch(symbol, *DAILY)
    elif tf in ("W", "M", "Q"):
        daily, meta = candles(symbol, "D")
        out = (aggregate(daily, tf, meta.get("gmtoffset", 0)), meta)
    else:
        raise ValueError(f"unknown timeframe {tf} (use 60, D, W, M or Q)")
    _cache[key] = (time.time(), out)
    return out


def _period_key(ts: int, tf: str, offset: int) -> tuple:
    d = datetime.fromtimestamp(ts + offset, tz=timezone.utc)
    if tf == "W":
        iso = d.isocalendar()
        return (iso[0], iso[1])
    if tf == "M":
        return (d.year, d.month)
    return (d.year, (d.month - 1) // 3)  # Q


def aggregate(daily: list[Bar], tf: str, offset: int = 0) -> list[Bar]:
    """Build weekly (W), monthly (M) or quarterly (Q) candles from daily candles, using calendar
    periods in the exchange's time zone (offset = seconds from UTC)."""
    out: list[Bar] = []
    current = None
    for b in daily:
        key = _period_key(b.t, tf, offset)
        if key != current:
            out.append(Bar(b.t, b.o, b.h, b.l, b.c))
            current = key
        else:
            p = out[-1]
            p.h, p.l, p.c = max(p.h, b.h), min(p.l, b.l), b.c
    return out
