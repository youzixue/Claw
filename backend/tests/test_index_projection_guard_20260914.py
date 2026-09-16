"""No production access: stale index fallbacks must not mutate history."""
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from sqlalchemy import select

from app.data import scheduler as module
from app.data.scheduler import DataScheduler
from app.models.stock import StockDaily
from test_scheduler_kline_fill import scheduler_db_env

CODES = ("000001", "399001", "399006")


@pytest.mark.asyncio
@pytest.mark.parametrize("case,expected_current", [("stale", 0), ("mixed", 1), ("current", 3)])
async def test_index_fallback_only_writes_today_and_explicitly_reports_zero_coverage(
    scheduler_db_env, monkeypatch, case, expected_current,
):
    today = date.today()
    previous = today - timedelta(days=1)
    async with scheduler_db_env() as db:
        db.add_all(StockDaily(code=code, trade_date=previous, close=3000,
                               open=2999, prev_close=2998, change_pct=0.01) for code in CODES)
        await db.commit()
        before = list((await db.execute(select(StockDaily.__table__).order_by(StockDaily.id))).all())

    async def fetch(code):
        day = today if case == "current" or (case == "mixed" and code == CODES[0]) else previous
        return pd.DataFrame([{"date": day.isoformat(), "close": 4000,
                              "open": 3999, "high": 4001, "low": 3998, "volume": 100}])
    scheduler = DataScheduler()
    scheduler._sources = {"index": SimpleNamespace(get_index_spot=AsyncMock(return_value=pd.DataFrame()),
                                                   get_index_daily=AsyncMock(side_effect=fetch))}
    monkeypatch.setattr(module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    monkeypatch.setattr(module.trade_calendar, "get_trade_session", lambda: "morning")
    good, bad = AsyncMock(), AsyncMock()
    monkeypatch.setattr(module.data_quality_guard, "record_success", good)
    monkeypatch.setattr(module.data_quality_guard, "record_failure", bad)
    await scheduler._intraday_indices_and_sentiment()
    async with scheduler_db_env() as db:
        after_old = list((await db.execute(select(StockDaily.__table__).where(
            StockDaily.trade_date == previous).order_by(StockDaily.id))).all())
        assert after_old == before
        current = (await db.scalars(select(StockDaily).where(StockDaily.trade_date == today))).all()
        assert len(current) == expected_current
        assert all(row.close == 4000 for row in current)
    if expected_current == 3:
        index_calls = [call for call in good.call_args_list if call.args[1:3] == ("index", "daily_snapshot")]
        assert len(index_calls) == 1 and index_calls[0].kwargs["record_count"] == 3
    else:
        index_calls = [call for call in bad.call_args_list if call.args[1:3] == ("index", "daily_snapshot")]
        assert len(index_calls) == 1
        assert index_calls[0].kwargs["completeness"] == expected_current / 3
        assert f"仅{expected_current}/3" in index_calls[0].args[3]


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["previous", "future", "unknown", "string"])
async def test_generic_stockdaily_writer_rejects_noncurrent_dates_before_any_db_access(when):
    today = date.today()
    value = {"previous": today-timedelta(days=1), "future": today+timedelta(days=1),
             "unknown": None, "string": today.isoformat()}[when]
    db = AsyncMock()
    with pytest.raises(ValueError, match="actual current date"):
        await DataScheduler._batch_upsert(db, StockDaily,
            [{"code": "000001", "trade_date": value, "close": 9000}], ["code", "trade_date"])
    assert not db.mock_calls
