"""Time helpers. Everything is IST; the machine's time zone is never used."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def today_ist() -> date:
    return now_ist().date()


def iso(dt: datetime) -> str:
    return dt.astimezone(IST).isoformat(timespec="seconds")


def kite_session_expiry(logged_in_at: datetime) -> datetime:
    """Kite access tokens expire at 06:00 IST the morning after login."""
    local = logged_in_at.astimezone(IST)
    expiry = datetime.combine(local.date(), time(6, 0), tzinfo=IST)
    if local >= expiry:
        expiry += timedelta(days=1)
    return expiry
