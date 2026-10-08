"""Bounded K-line consumption must preserve candidates and let the scheduler run."""
import asyncio
from datetime import date, datetime, timedelta
import pytest
import pytest_asyncio
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.v1 import tenbagger as api
from app.db.session import Base
from app.models.stock import LimitUpPool, StockKline, StockSpot


@pytest_asyncio.fixture
async def market(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'market.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
            StockKline.__table__, StockSpot.__table__, LimitUpPool.__table__]))
    async def news(*args, **kwargs):
        return {}
    async def funds(*args, **kwargs):
        return {"status": "calendar_incomplete", "items": {}, "version": "fixture",
                "basis": "dated_latest_not_pit", "decision_at": "2026-07-13T16:00:00",
                "through_date": "2026-07-13", "session_dates": [], "expected_count": 5}
    monkeypatch.setattr(api, "load_direct_stock_catalyst_map", news)
    monkeypatch.setattr(api, "load_main_fund_window", funds)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db
    finally:
        await engine.dispose()


async def seed(db, count=65, days=60, bad_values=False):
    start = date(2026, 5, 15)
    closes = [10.0 + i * .08 for i in range(48)] + [
        13.8, 15.18, 16.70, 16.10, 17.20, 18.92,
        19.60, 20.30, 21.10, 22.00, 22.70, 23.40]
    spots, bars = [], []
    for n in range(count):
        code = f"60{n:04d}"
        spots.append(dict(code=code, name=f"测试{n}", price=23.4, prev_close=22.7,
                          volume=0, circ_market_cap=200, turnover=8, change_pct=3.08,
                          volume_ratio=1.2, updated_at=datetime(2026, 7, 13, 16)))
        for i, close in enumerate(closes[:days]):
            bars.append(dict(code=code, trade_date=start+timedelta(days=i),
                             open=close*.99, close=close, high=close*1.02, low=close*.98,
                             volume=None if bad_values else 10000000+i*200000,
                             turnover=None if bad_values else 5, change_pct=3, source="test"))
    if spots:
        await db.execute(insert(StockSpot), spots)
    if bars:
        await db.execute(insert(StockKline), bars)
    await db.commit()
    return start + timedelta(days=days-1)


@pytest.mark.asyncio
async def test_scoring_yields_between_stocks_without_changing_order(market, monkeypatch):
    day = await seed(market)
    original = api._score_main_wave_kline_pattern
    ticks = 0
    seen = []
    stop = False
    async def observer():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)
    def scoring(**kwargs):
        seen.append(ticks)
        return original(**kwargs)
    monkeypatch.setattr(api, "_score_main_wave_kline_pattern", scoring)
    task = asyncio.create_task(observer())
    try:
        result = await api._load_main_wave_plan_candidates(market, day, set(), limit=80)
    finally:
        stop = True
        await task
    assert len(seen) == 65
    assert len(set(seen)) > 1, "all stock scoring monopolized one event-loop turn"
    assert [r["code"] for r in result] == [f"60{n:04d}" for n in range(65)]
    assert all(r["dynamic_pool_source"] == "main_wave_pattern" for r in result)
    assert all(r["dynamic_pool_stats"]["setup_state"] == "armed_main_wave_pullback" for r in result)


@pytest.mark.asyncio
async def test_market_kline_query_is_streamed_and_cursor_closed(market, monkeypatch):
    day = await seed(market, count=35)
    execute, stream = market.execute, market.stream
    cursors = []
    def is_screening(stmt):
        names = [d.get("name") for d in getattr(stmt, "column_descriptions", [])]
        return names == ["code", "trade_date", "open", "close", "high", "low", "volume", "turnover", "change_pct"]
    async def guarded_execute(stmt, *args, **kwargs):
        assert not is_screening(stmt), "full market query may not buffer with execute/all"
        return await execute(stmt, *args, **kwargs)
    async def tracked_stream(stmt, *args, **kwargs):
        result = await stream(stmt, *args, **kwargs)
        if is_screening(stmt):
            cursors.append(result)
        return result
    monkeypatch.setattr(market, "execute", guarded_execute)
    monkeypatch.setattr(market, "stream", tracked_stream)
    result = await api._load_main_wave_plan_candidates(market, day, set(), limit=80)
    assert len(result) == 35
    assert len(cursors) == 1 and cursors[0].closed
    assert market.in_transaction(), "loader must not commit/rollback its owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel", "read_error"])
@pytest.mark.parametrize("close_fails", [False, True])
async def test_stream_failure_closes_cursor_and_preserves_owner(market, monkeypatch, failure, close_fails):
    day = await seed(market, count=1)
    closed = []
    class BrokenStream:
        def partitions(self, size):
            async def batches():
                if failure == "cancel":
                    raise asyncio.CancelledError("test cancel")
                raise RuntimeError("test read error")
                yield []
            return batches()
        async def close(self):
            closed.append(True)
            if close_fails:
                raise ValueError("test close failure")
    async def broken_stream(*args, **kwargs):
        return BrokenStream()
    monkeypatch.setattr(market, "stream", broken_stream)
    expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
    with pytest.raises(expected):
        await api._load_main_wave_plan_candidates(market, day, set(), limit=80)
    assert closed == [True]
    assert market.in_transaction()
    assert (await market.execute(select(StockSpot.code))).scalars().all() == ["600000"]


@pytest.mark.asyncio
async def test_real_task_cancel_closes_real_cursor(market, monkeypatch):
    day = await seed(market, count=35)
    original = market.stream
    ready = asyncio.Event()
    cursors = []
    class PausedStream:
        def __init__(self, result):
            self.result = result
        async def partitions(self, size):
            async for batch in self.result.partitions(size):
                yield batch
                ready.set()
                await asyncio.Event().wait()
        async def close(self):
            await self.result.close()
    async def pause_stream(*args, **kwargs):
        result = await original(*args, **kwargs)
        cursors.append(result)
        return PausedStream(result)
    monkeypatch.setattr(market, "stream", pause_stream)
    task = asyncio.create_task(api._load_main_wave_plan_candidates(market, day, set(), limit=80))
    await asyncio.wait_for(ready.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cursors[0].closed
    assert len((await market.execute(select(StockSpot.code))).all()) == 35
    assert market.in_transaction()


@pytest.mark.asyncio
async def test_close_failure_on_success_is_not_silently_accepted(market, monkeypatch):
    day = await seed(market, count=1)
    class CloseFailure:
        async def partitions(self, size):
            if False:
                yield []
        async def close(self):
            raise ValueError("test close failed after success")
    async def stream(*args, **kwargs):
        return CloseFailure()
    monkeypatch.setattr(market, "stream", stream)
    with pytest.raises(ValueError, match="after success"):
        await api._load_main_wave_plan_candidates(market, day, set(), limit=80)
    assert market.in_transaction()


@pytest.mark.asyncio
@pytest.mark.parametrize("count,days", [(0,60),(1,59)])
async def test_empty_or_short_history_remains_empty(market, count, days):
    day = await seed(market, count=count, days=days)
    assert await api._load_main_wave_plan_candidates(market, day, set(), limit=80) == []


@pytest.mark.asyncio
async def test_exclusion_and_limit_still_apply(market):
    day = await seed(market, count=35)
    rows = await api._load_main_wave_plan_candidates(market, day, {"600000", "600002"}, limit=2)
    assert [r["code"] for r in rows] == ["600001", "600003"]


@pytest.mark.asyncio
async def test_null_volume_keeps_existing_pattern_semantics(market):
    day = await seed(market, count=1, bad_values=True)
    rows = await api._load_main_wave_plan_candidates(market, day, set(), limit=80)
    # This is a research shape candidate, not an authorized order. Preserve the
    # existing null normalization rather than changing finance rules for a perf fix.
    assert len(rows) == 1 and rows[0]["code"] == "600000"
    assert rows[0]["turnover"] == 0 and rows[0]["volume_ratio"] == 1
    assert rows[0]["candidate_source"] == "main_wave_pattern"
