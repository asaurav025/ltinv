"""NSE trading days (Capital Market segment), in IST.

Source: NSE "Trading holidays" for the CM segment, calendar 2026,
https://www.nseindia.com/resources/exchange-communication-holidays (holiday-master API, key "CM",
fetched 2026-10-30; same list as the day-trader-skill fork's trader_common/clock.py).
Years not listed fall back to weekdays only: add the next year's list when NSE publishes it.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ltinv.timeutil import IST

NSE_HOLIDAYS = {
    "2026-01-15", "2026-01-26", "2026-02-15", "2026-03-03", "2026-03-21", "2026-03-26",
    "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-08-15", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-08", "2026-11-10",
    "2026-11-24", "2026-12-25",
}
KNOWN_YEARS = {2026}
MARKET_CLOSE = time(15, 30)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in NSE_HOLIDAYS


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def close_after_trading_days(start: datetime, n: int) -> datetime:
    """Market close on the n-th trading day counting the start day if it is a trading day
    and the market hasn't closed yet. n=1 approved at 10:00 on a trading day -> 15:30 today;
    approved at 20:00 -> 15:30 on the next trading day."""
    local = start.astimezone(IST)
    d = local.date()
    if not (is_trading_day(d) and local.time() < MARKET_CLOSE):
        d = next_trading_day(d)
    for _ in range(n - 1):
        d = next_trading_day(d)
    return datetime.combine(d, MARKET_CLOSE, tzinfo=IST)
