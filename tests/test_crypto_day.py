"""Crypto trades 24/7 and its daily candle rolls over at midnight UTC (8 PM New York in summer)."""

from datetime import datetime, timezone

from amo import market
from amo.config import settings
from amo.market import Bar, crypto_daily, crypto_day_open_text

H = 3600
DAY0 = int(datetime(2026, 10, 25, tzinfo=timezone.utc).timestamp())


def hourly(days: int) -> list[Bar]:
    return [Bar(DAY0 + i * H, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(days * 24)]


def test_a_day_yahoo_skipped_is_rebuilt_from_hourly_bars():
    # Yahoo's dailies right after the rollover: Oct 26 and 27 are missing
    yahoo = [Bar(DAY0 - 86400, 1, 2, 0.5, 1.5), Bar(DAY0, 100, 124, 99, 123.5), Bar(DAY0 + 3 * 86400, 1, 1, 1, 1)]
    out = crypto_daily(yahoo, hourly(4))
    starts = [b.t for b in out]
    assert starts == [DAY0 - 86400, DAY0, DAY0 + 86400, DAY0 + 2 * 86400, DAY0 + 3 * 86400]
    oct26 = out[2]  # rebuilt from its 24 hourly bars
    assert (oct26.o, oct26.h, oct26.l, oct26.c) == (124, 148, 123, 147.5)


def test_new_york_8pm_in_summer_7pm_in_winter_when_set_to_utc(monkeypatch):
    monkeypatch.setattr(settings, "crypto_day_start", "utc")
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    assert crypto_day_open_text(datetime(2026, 10, 28, 20, 5, tzinfo=timezone.utc)) == \
        "8:00 PM New York time (midnight UTC)"
    assert crypto_day_open_text(datetime(2026, 12, 1, tzinfo=timezone.utc)).startswith("7:00 PM")


def test_a_fixed_new_york_day_start(monkeypatch):
    monkeypatch.setattr(settings, "crypto_day_start", "20:00")
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    assert crypto_day_open_text() == "8:00 PM New York time"
    winter_8pm = int(datetime(2026, 12, 2, 1, 0, tzinfo=timezone.utc).timestamp())  # 8 PM EST
    assert market.crypto_day_key(winter_8pm) != market.crypto_day_key(winter_8pm - 60)


def test_amo_knows_crypto_never_closes(db):
    from amo.agent import Agent

    prompt = Agent(db).build_system_prompt()
    assert "24 hours a day, 7 days a week" in prompt and "midnight UTC" in prompt
