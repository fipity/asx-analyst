"""ASX trading calendar (rule-based, checked against 2014-2026 ASX 200 history).

Closed: New Year's Day, Australia Day, Good Friday, Easter Monday, Anzac Day, King's Birthday
(2nd Monday of June), Christmas Day, Boxing Day. New Year's Day and Australia Day move to Monday
when they fall on a weekend; Christmas/Boxing Day move to the next weekdays. Anzac Day is NOT moved
(e.g. Saturday 25 April 2026: the ASX traded on Monday 27 April). One-off closures are listed in
ONE_OFF. Christmas Eve and New Year's Eve close early (2:10pm), which doesn't matter for daily data.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

from app.config import MARKET_TZ

ONE_OFF = {date(2022, 9, 22): "National Day of Mourning"}


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _monday_if_weekend(d: date) -> date:
    return d + timedelta(days=(7 - d.weekday()) % 7) if d.weekday() >= 5 else d


@lru_cache(maxsize=64)
def holidays(year: int) -> dict[date, str]:
    out = {_monday_if_weekend(date(year, 1, 1)): "New Year's Day",
           _monday_if_weekend(date(year, 1, 26)): "Australia Day"}
    e = easter(year)
    out[e - timedelta(days=2)] = "Good Friday"
    out[e + timedelta(days=1)] = "Easter Monday"
    anzac = date(year, 4, 25)
    if anzac.weekday() < 5:
        out[anzac] = "Anzac Day"
    june1 = date(year, 6, 1)
    out[june1 + timedelta(days=(7 - june1.weekday()) % 7 + 7)] = "King's Birthday"
    xmas, boxing = date(year, 12, 25), date(year, 12, 26)
    if xmas.weekday() == 5:          # Sat: Mon + Tue
        out[date(year, 12, 27)], out[date(year, 12, 28)] = "Christmas Day", "Boxing Day"
    elif xmas.weekday() == 6:        # Sun: Mon (Boxing Day) + Tue
        out[date(year, 12, 26)], out[date(year, 12, 27)] = "Boxing Day", "Christmas Day"
    else:
        out[xmas] = "Christmas Day"
        out[boxing if boxing.weekday() < 5 else date(year, 12, 28)] = "Boxing Day"
    out.update({d: n for d, n in ONE_OFF.items() if d.year == year})
    return out


def _as_date(d) -> date:
    if isinstance(d, datetime):
        return d.astimezone(MARKET_TZ).date() if d.tzinfo else d.date()
    if isinstance(d, str):
        return date.fromisoformat(d[:10])
    return d


def holiday_name(d) -> str | None:
    d = _as_date(d)
    return holidays(d.year).get(d)


def is_trading_day(d) -> bool:
    d = _as_date(d)
    return d.weekday() < 5 and d not in holidays(d.year)


def next_trading_day(d, include: bool = True) -> date:
    d = _as_date(d)
    if not include:
        d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d, include: bool = True) -> date:
    d = _as_date(d)
    if not include:
        d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_days_after(start, end) -> int:
    """Trading days in (start, end]."""
    a, b = _as_date(start), _as_date(end)
    n, d = 0, a + timedelta(days=1)
    while d <= b:
        n += is_trading_day(d)
        d += timedelta(days=1)
    return n


def today() -> date:
    return datetime.now(timezone.utc).astimezone(MARKET_TZ).date()


def upcoming(n: int = 3, start: date | None = None) -> list[tuple[date, str]]:
    start = start or today()
    out = sorted((d, name) for y in (start.year, start.year + 1) for d, name in holidays(y).items() if d >= start)
    return out[:n]
