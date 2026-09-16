from datetime import date, timedelta

import pytest

from app.core.trade_calendar import TradeCalendar, is_official_closed_day


def _calendar_with_weekday_cache(start: date, end: date) -> TradeCalendar:
    calendar = TradeCalendar()
    current = start
    while current <= end:
        calendar._cache[current] = current.weekday() < 5
        current += timedelta(days=1)
    return calendar


def test_official_holiday_override_marks_weekday_closed():
    assert is_official_closed_day(date(2026, 6, 19))
    assert is_official_closed_day(date(2026, 10, 9))
    assert not is_official_closed_day(date(2026, 10, 12))


@pytest.mark.asyncio
async def test_next_and_previous_trade_day_cross_long_national_day_closure():
    calendar = _calendar_with_weekday_cache(date(2026, 9, 20), date(2026, 10, 20))

    assert await calendar.next_trade_day(date(2026, 9, 30)) == date(2026, 10, 12)
    assert await calendar.previous_trade_day(date(2026, 10, 12)) == date(2026, 9, 30)


@pytest.mark.asyncio
async def test_shift_trade_day_rejects_closed_zero_offset():
    calendar = _calendar_with_weekday_cache(date(2026, 6, 15), date(2026, 6, 25))

    with pytest.raises(ValueError, match="不是交易日"):
        await calendar.shift_trade_day(date(2026, 6, 19), 0)
