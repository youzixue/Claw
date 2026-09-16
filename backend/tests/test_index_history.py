from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.trade_calendar import is_official_closed_day
from app.data.index_history import reconcile_index_history
from app.db.session import Base
from app.models.governance import TradeCalendarModel
from app.models.stock import StockDaily


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'index-history.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


def trade_days(through: date, count: int) -> list[date]:
    days = []
    cursor = through
    while len(days) < count:
        if cursor.weekday() < 5 and not is_official_closed_day(cursor):
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def history_frame(days: list[date], *, base: float) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "date": day.isoformat(), "open": base + index - 1,
            "high": base + index + 1, "low": base + index - 2,
            "close": base + index, "volume": 1000 + index,
        }
        for index, day in enumerate(days)
    ])


@pytest.mark.asyncio
async def test_reconcile_audits_only_real_calendar_gaps_without_writing_history(db):
    through = date(2026, 9, 7)
    days = trade_days(through, 65)
    missing = days[20]
    non_trade_day = next(
        through - timedelta(days=offset)
        for offset in range(1, 30)
        if (through - timedelta(days=offset)).weekday() >= 5
    )
    future_day = through + timedelta(days=1)
    db.add_all(TradeCalendarModel(trade_date=day, is_trade_day=True) for day in days)
    for code in ("000001", "399001", "399006"):
        db.add_all(
            StockDaily(code=code, trade_date=day, close=9000.0 if day == days[0] else 3000.0)
            for day in days if day != missing
        )
    await db.flush()

    frames = {
        code: history_frame(days + [non_trade_day, future_day], base=3000 + index * 1000)
        for index, code in enumerate(("000001", "399001", "399006"))
    }
    source = SimpleNamespace(get_index_daily=AsyncMock(side_effect=lambda code: frames[code]))

    first = await reconcile_index_history(db, source, through_date=through)
    second = await reconcile_index_history(db, source, through_date=through)

    assert first["complete"] is False  # projection still has its original gaps
    assert first["inserted_count"] == first["repaired_invalid_count"] == 0
    assert first["candidate_count"] == 3
    assert first["mode"] == "audit_only_no_historical_projection"
    assert first["historical_projection_changed"] is False
    assert all(item["inserted_dates"] == [] for item in first["codes"].values())
    assert all(item["missing_candidate_dates"] == [missing.isoformat()] for item in first["codes"].values())
    assert all(item["missing_after"] == [missing.isoformat()] for item in first["codes"].values())
    assert all(item["source_observed_at"] for item in first["codes"].values())
    assert all(len(item["source_payload_sha256"]) == 64 for item in first["codes"].values())
    assert all(item["source_expected_row_count"] == 65 for item in first["codes"].values())
    assert second["complete"] is False and second["inserted_count"] == 0
    assert second["candidate_count"] == 3  # a second observation is still not a historical write
    assert await db.scalar(select(StockDaily).where(StockDaily.trade_date == missing)) is None
    assert await db.scalar(select(StockDaily).where(StockDaily.trade_date == non_trade_day)) is None
    assert await db.scalar(select(StockDaily).where(StockDaily.trade_date == future_day)) is None
    preserved = await db.scalar(select(StockDaily.close).where(
        StockDaily.code == "000001", StockDaily.trade_date == days[0],
    ))
    assert preserved == 9000.0


@pytest.mark.asyncio
async def test_reconcile_stays_incomplete_when_true_source_lacks_expected_day(db):
    through = date(2026, 9, 7)
    days = trade_days(through, 65)
    missing = days[10]
    db.add_all(TradeCalendarModel(trade_date=day, is_trade_day=True) for day in days)
    for code in ("000001", "399001", "399006"):
        db.add_all(
            StockDaily(code=code, trade_date=day, close=3000.0)
            for day in days if day != missing
        )
    await db.flush()
    frame = history_frame([day for day in days if day != missing], base=3000)
    source = SimpleNamespace(get_index_daily=AsyncMock(return_value=frame))

    result = await reconcile_index_history(db, source, through_date=through)

    assert result["complete"] is False
    assert result["inserted_count"] == 0
    assert all(item["missing_after"] == [missing.isoformat()] for item in result["codes"].values())
    assert result["reason"] == "真实指数历史源仍缺少期望交易日"


@pytest.mark.asyncio
async def test_reconcile_does_not_fetch_when_calendar_is_incomplete(db):
    through = date(2026, 9, 7)
    days = trade_days(through, 64)
    db.add_all(TradeCalendarModel(trade_date=day, is_trade_day=True) for day in days)
    await db.flush()
    source = SimpleNamespace(get_index_daily=AsyncMock())

    result = await reconcile_index_history(db, source, through_date=through)

    assert result["complete"] is False
    assert result["reason"] == "完整交易日历不足65日"
    source.get_index_daily.assert_not_awaited()


@pytest.mark.asyncio
async def test_calendar_gap_cannot_be_replaced_by_an_older_sixty_fifth_day(db):
    through = date(2026, 9, 7)
    days = trade_days(through, 66)
    calendar_gap = days[-10]
    db.add_all(
        TradeCalendarModel(trade_date=day, is_trade_day=True)
        for day in days if day != calendar_gap
    )
    await db.flush()
    source = SimpleNamespace(get_index_daily=AsyncMock())

    result = await reconcile_index_history(db, source, through_date=through)

    assert result["complete"] is False
    assert result["expected_count"] == 0
    source.get_index_daily.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_unique_row_remains_unchanged_and_is_audited_as_a_candidate(db):
    through = date(2026, 9, 7)
    days = trade_days(through, 65)
    invalid_day = days[20]
    db.add_all(TradeCalendarModel(trade_date=day, is_trade_day=True) for day in days)
    for code in ("000001", "399001", "399006"):
        db.add_all(StockDaily(
            code=code,
            trade_date=day,
            close=(None if code == "000001" and day == invalid_day else 3000.0),
        ) for day in days)
    await db.flush()
    source = SimpleNamespace(get_index_daily=AsyncMock(
        side_effect=lambda code: history_frame(days, base=4000.0),
    ))


    result = await reconcile_index_history(db, source, through_date=through)

    repaired = result["codes"]["000001"]
    assert result["complete"] is False
    assert result["inserted_count"] == result["repaired_invalid_count"] == 0
    assert result["candidate_count"] == 1
    assert repaired["inserted_dates"] == repaired["repaired_invalid_dates"] == []
    assert repaired["invalid_candidate_dates"] == [invalid_day.isoformat()]
    assert await db.scalar(select(StockDaily.close).where(
        StockDaily.code == "000001", StockDaily.trade_date == invalid_day,
    )) is None
    assert await db.scalar(select(StockDaily.close).where(
        StockDaily.code == "399001", StockDaily.trade_date == days[0],
    )) == 3000.0


@pytest.mark.asyncio
async def test_audit_uses_only_selects_and_never_flushes_caller_pending_or_dirty_rows(db):
    from sqlalchemy import event
    through = date(2026, 9, 7)
    days = trade_days(through, 65)
    db.add_all(TradeCalendarModel(trade_date=day, is_trade_day=True) for day in days)
    original = StockDaily(code="000001", trade_date=days[0], close=None)
    db.add(original)
    await db.commit()
    original.close = 123.0
    pending = StockDaily(code="399006", trade_date=through, close=987.0)
    db.add(pending)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip().split()[0].upper())
    event.listen(db.bind.sync_engine, "before_cursor_execute", capture)
    try:
        result = await reconcile_index_history(
            db, SimpleNamespace(get_index_daily=AsyncMock(return_value=history_frame(days, base=3000))),
            through_date=through, codes=("000001",),
        )
    finally:
        event.remove(db.bind.sync_engine, "before_cursor_execute", capture)
    assert statements and set(statements) == {"SELECT"}
    assert result["candidate_count"] == 65
    assert result["codes"]["000001"]["invalid_candidate_dates"] == [days[0].isoformat()]
    assert original in db.dirty and pending in db.new
    await db.rollback()
    assert await db.scalar(select(StockDaily.close).where(StockDaily.code == "000001")) is None
    assert await db.scalar(select(StockDaily.id).where(StockDaily.code == "399006")) is None
