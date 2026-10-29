"""Market data (Yahoo Finance chart API — free, no key): candles for any stock, ETF,
index, future or crypto, on 60-minute, daily, weekly, monthly and quarterly timeframes."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

# Yahoo rejects full browser user-agents (429) but accepts a plain one.
YAHOO_UA = {"User-Agent": "Mozilla/5.0"}

# Friendly names and futures/crypto roots → Yahoo symbols.
ALIASES = {
    "NQ": "NQ=F", "MNQ": "MNQ=F", "ES": "ES=F", "MES": "MES=F", "YM": "YM=F", "RTY": "RTY=F",
    "CL": "CL=F", "OIL": "CL=F", "CRUDE": "CL=F", "GC": "GC=F", "GOLD": "GC=F", "SI": "SI=F", "SILVER": "SI=F",
    "BTC": "BTC-USD", "BITCOIN": "BTC-USD", "ETH": "ETH-USD", "ETHEREUM": "ETH-USD", "SOL": "SOL-USD",
    "SOLANA": "SOL-USD", "XRP": "XRP-USD", "RIPPLE": "XRP-USD", "DOGE": "DOGE-USD", "DOGECOIN": "DOGE-USD",
    "ADA": "ADA-USD", "CARDANO": "ADA-USD", "AVAX": "AVAX-USD", "AVALANCHE": "AVAX-USD", "LINK": "LINK-USD",
    "CHAINLINK": "LINK-USD", "LTC": "LTC-USD", "LITECOIN": "LTC-USD", "DOT": "DOT-USD", "POLKADOT": "DOT-USD",
    "BNB": "BNB-USD", "SHIB": "SHIB-USD", "PEPE": "PEPE24478-USD", "SUI": "SUI20947-USD",
    "MBT": "MBT=F",  # micro bitcoin futures (CME)
    "SPX": "^GSPC", "S&P": "SPY", "S AND P": "SPY", "SP500": "SPY", "NASDAQ": "QQQ", "NDX": "^NDX",
    "DOW": "DIA", "VIX": "^VIX", "DXY": "DX-Y.NYB", "DOLLAR": "DX-Y.NYB", "RUSSELL": "IWM",
    "APPLE": "AAPL", "TESLA": "TSLA", "NVIDIA": "NVDA", "MICROSOFT": "MSFT", "AMAZON": "AMZN",
    "GOOGLE": "GOOGL", "META": "META", "NETFLIX": "NFLX", "AMD": "AMD",
}

# Weekly/monthly/quarterly candles are built from daily data: Yahoo's own 1wk/1mo series append a
# separate bar for the latest session, which would make the "current month" look like one day.
INTRADAY = ("60m", "3mo")  # three months of hourly bars — also builds the 4-hour candles
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
    # "BTCUSD", "BTC/USD", "BTC-USDT", "BTCUSDT" (exchange spellings) → the coin
    m = re.fullmatch(r"([A-Z]{2,6})[-/]?USDT?", s)
    if m and m.group(1) in ALIASES and ALIASES[m.group(1)].endswith("-USD"):
        return ALIASES[m.group(1)]
    return ALIASES.get(s, s)


def is_crypto(symbol: str) -> bool:
    """Trades 24/7 — no opening bell, no weekends off."""
    return normalize(symbol).endswith("-USD")


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


def _crypto_day_start_spec() -> tuple[int, int] | None:
    """None = midnight UTC; (hour, minute) = that time of day in New York (AMO's time zone)."""
    from .config import settings

    spec = (settings.crypto_day_start or "utc").strip().lower()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", spec)
    return (int(m.group(1)), int(m.group(2))) if m else None


def crypto_day_key(t: int) -> Any:
    """Which crypto trading day the moment `t` (unix seconds) belongs to."""
    spec = _crypto_day_start_spec()
    if spec is None:
        return t // 86400
    from zoneinfo import ZoneInfo

    from .config import settings

    local = datetime.fromtimestamp(t, ZoneInfo(settings.timezone))
    return (local - timedelta(hours=spec[0], minutes=spec[1])).date()


def crypto_day_open_text(now: datetime | None = None) -> str:
    """'8:00 PM New York time (midnight UTC)' — when crypto's daily candle opens and closes."""
    from zoneinfo import ZoneInfo

    from .config import settings

    tz = ZoneInfo(settings.timezone)
    spec = _crypto_day_start_spec()
    if spec is not None:
        h, mi = spec
        return f"{(h - 1) % 12 + 1}:{mi:02d} {'AM' if h < 12 else 'PM'} New York time"
    now = now or datetime.now(timezone.utc)
    midnight = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    local = midnight.astimezone(tz)
    return f"{local:%-I:%M %p} {'New York' if 'New_York' in settings.timezone else 'local'} time (midnight UTC)"


def crypto_daily(daily: list[Bar], hourly: list[Bar]) -> list[Bar]:
    """Crypto's daily candles, the recent ones rebuilt from hourly bars on the crypto day boundary.
    Yahoo's crypto dailies can skip a day right after the daily rollover; the hourly bars don't."""
    groups: list[list[Bar]] = []
    last = None
    for b in hourly:
        k = crypto_day_key(b.t)
        if k != last:
            groups.append([])
            last = k
        groups[-1].append(b)
    if len(groups) < 3:
        return daily
    built = [Bar(g[0].t, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c) for g in groups[1:]]  # first may be partial
    first = groups[1][0].t
    older = [b for b in daily if b.t < first and crypto_day_key(b.t) != crypto_day_key(first)]
    return older + built


def candles(symbol: str, tf: str = "D") -> tuple[list[Bar], dict[str, Any]]:
    """Candles for a timeframe: 60, 4H, D, W, M or Q (4H from hourly, W/M/Q from daily). Cached 60s."""
    symbol = normalize(symbol)
    key = f"{symbol}:{tf}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    if tf == "60":
        out = _fetch(symbol, *INTRADAY)
    elif tf == "4H":
        hourly, meta = candles(symbol, "60")
        out = (four_hour(hourly, symbol, meta), meta)
    elif tf == "D":
        out = _fetch(symbol, *DAILY)
        if symbol.endswith("-USD"):  # crypto: 24/7, with a clean daily rollover (see crypto_daily)
            try:
                out = (crypto_daily(out[0], candles(symbol, "60")[0]), out[1])
            except Exception:  # noqa: BLE001 — no hourly data: keep Yahoo's dailies
                pass
    elif tf in ("W", "M", "Q"):
        daily, meta = candles(symbol, "D")
        out = (aggregate(daily, tf, meta.get("gmtoffset", 0)), meta)
    else:
        raise ValueError(f"unknown timeframe {tf} (use 60, 4H, D, W, M or Q)")
    _cache[key] = (time.time(), out)
    return out


def four_hour(hourly: list[Bar], symbol: str, meta: dict[str, Any] | None = None) -> list[Bar]:
    """4-hour candles from hourly ones, on the boundaries traders' charts use:
    crypto — UTC 00:00, 04:00, 08:00…;  futures — from the 6 PM New York session open (18, 22, 02, 06, 10, 14);
    stocks — each day's session in blocks of four hours from the open (9:30–13:30, 13:30–16:00)."""
    from zoneinfo import ZoneInfo

    sym = normalize(symbol)
    try:
        tz = ZoneInfo((meta or {}).get("exchangeTimezoneName") or "America/New_York")
    except Exception:  # noqa: BLE001 — unknown zone name
        tz = ZoneInfo("America/New_York")

    if sym.endswith("-USD"):
        def key(b: Bar) -> tuple:
            return (b.t // 14400,)
    elif sym.endswith("=F"):
        def key(b: Bar) -> tuple:  # hours since 6 PM the evening before, in New York time (DST-proof)
            local = datetime.fromtimestamp(b.t, tz)
            session = (local - timedelta(hours=18)).date()
            since = (local.hour - 18) % 24
            return (session, since // 4)
    else:
        position: dict[int, tuple] = {}  # bar time → (day, which 4-hour block of that day's session)
        count: dict[Any, int] = {}
        for x in hourly:
            d = datetime.fromtimestamp(x.t, tz).date()
            position[x.t] = (d, count.get(d, 0) // 4)
            count[d] = count.get(d, 0) + 1

        def key(b: Bar) -> tuple:
            return position[b.t]

    out: list[Bar] = []
    current_key = None
    for b in hourly:
        k = key(b)
        if k != current_key:
            out.append(Bar(b.t, b.o, b.h, b.l, b.c))
            current_key = k
        else:
            last = out[-1]
            last.h, last.l, last.c = max(last.h, b.h), min(last.l, b.l), b.c
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
