from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from app.data.sources.ths_kline_source import ThsKlineSource


def _kline(trade_date: str, close: float) -> dict:
    return {
        "code": "000001",
        "trade_date": trade_date,
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "volume": 1_000_000,
        "amount": 10_000_000,
        "turnover": 1.0,
        "source": "ths",
    }


@pytest.mark.asyncio
async def test_collect_daily_derives_change_before_selecting_today(monkeypatch):
    source = ThsKlineSource()
    today = date.today()
    previous_day = today - timedelta(days=1)
    source._fetch_kline = AsyncMock(return_value=[
        _kline(previous_day.isoformat(), 10.0),
        _kline(today.isoformat(), 11.0),
    ])

    rows = await source.collect_daily("000001")

    assert len(rows) == 1
    assert rows[0]["trade_date"] == today.isoformat()
    assert rows[0]["prev_close"] == 10.0
    assert rows[0]["change_pct"] == 10.0


@pytest.mark.asyncio
async def test_collect_repair_returns_historical_gap_days(monkeypatch):
    source = ThsKlineSource()
    source._fetch_kline = AsyncMock(return_value=[
        _kline("2026-06-28", 10.0),
        _kline("2026-06-29", 10.5),
        _kline("2026-07-27", 11.0),
        _kline("2026-07-28", 11.5),
    ])
    monkeypatch.setattr("app.data.sources.ths_kline_source.asyncio.sleep", AsyncMock())

    rows = await source.collect_repair(
        "000001",
        lookback_days=30,
        as_of=date(2026, 7, 29),
    )

    assert [row["trade_date"] for row in rows] == [
        "2026-06-29",
        "2026-07-27",
        "2026-07-28",
    ]
    assert rows[0]["prev_close"] == 10.0
    assert rows[-1]["change_pct"] == 4.55


@pytest.mark.asyncio
async def test_collect_repair_mode_does_not_fall_back_to_daily(monkeypatch):
    source = ThsKlineSource()
    source.collect_repair = AsyncMock(return_value=[_kline("2026-07-28", 11.5)])
    source.collect_daily = AsyncMock(return_value=[])
    monkeypatch.setattr("app.data.sources.ths_kline_source.asyncio.sleep", AsyncMock())

    rows = await source.collect(
        session=None,
        codes=["000001"],
        mode="repair",
        repair_days=14,
    )

    assert len(rows) == 1
    source.collect_repair.assert_awaited_once_with("000001", lookback_days=14)
    source.collect_daily.assert_not_awaited()


def test_first_bar_has_unknown_previous_close_not_open_or_zero_return():
    source = ThsKlineSource()
    row = source._calc_derived([_kline("2026-09-14", 10.5)])[0]
    assert row["prev_close"] is None
    assert row["change_pct"] is None


@pytest.mark.asyncio
async def test_single_row_daily_keeps_unknown_return():
    source = ThsKlineSource()
    source._fetch_kline = AsyncMock(return_value=[_kline(date.today().isoformat(), 10.5)])
    rows = await source.collect_daily("000001")
    assert rows[0]["prev_close"] is None
    assert rows[0]["change_pct"] is None


@pytest.mark.asyncio
async def test_collect_init_reports_unavailable_years_without_certifying_coverage(monkeypatch):
    source = ThsKlineSource()
    source.rate_limit = 0
    source._fetch_kline = AsyncMock(side_effect=lambda code, suffix: (
        [_kline("2026-09-14", 10.5)] if suffix in ("last.js", "2026.js") else None
    ))
    monkeypatch.setattr("app.data.sources.ths_kline_source.asyncio.sleep", AsyncMock())
    coverage = {}
    rows = await source.collect_init("000001", coverage=coverage)
    assert len(rows) == 1
    assert coverage["certified"] is False
    assert coverage["last_rows"] == 1
    assert 2018 in coverage["unavailable_years"]
    assert coverage["year_rows"]["2018"] == 0
