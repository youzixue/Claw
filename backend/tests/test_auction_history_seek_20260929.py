"""Latest-positive-volume seeks: unchanged observations, bounded indexed work."""
from datetime import date, timedelta
from unittest.mock import AsyncMock

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.stock import StockKline
from app.strategy.auction import AuctionCollector

DAY = date(2026, 9, 29)


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'history.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(StockKline.__table__.create)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_history_seeks_are_indexed_bounded_and_keep_original_vectors(maker, monkeypatch):
    collector = AuctionCollector()
    codes = [f"60{n:04d}" for n in range(205)]
    async with maker() as db:
        for number, code in enumerate(codes):
            values = [float("inf"), None, 0, -1, number+1, 20, 30, 40, 50, 999]
            db.add_all([
                StockKline(code=code, trade_date=DAY-timedelta(days=n), volume=value)
                for n, value in enumerate(values, 1)
            ])
        # Today and future values must never enter the old or new calculation.
        db.add_all([StockKline(code=codes[0], trade_date=DAY+timedelta(days=n), volume=99999)
                    for n in (0, 1)])
        await db.commit()
        old = (await db.execute(
            select(StockKline.code, StockKline.volume).where(
                StockKline.code.in_(codes), StockKline.trade_date < DAY
            ).order_by(StockKline.code, StockKline.trade_date.desc())
        )).all()
        vectors = {}
        for code, volume in old:
            parsed = collector._safe_float(volume)
            if parsed > 0 and len(vectors.get(code, [])) < 5:
                vectors.setdefault(code, []).append(parsed)
        expected = {code: float(np.mean(v)) for code, v in vectors.items()}
        execute = db.execute
        sizes, plans = [], []
        connection = await db.connection()

        async def measured(statement, *args, **kwargs):
            # EXPLAIN using the exact compiled positional bindings (including infinity).
            compiled = statement.compile(dialect=connection.dialect,
                                         compile_kwargs={"render_postcompile": True})
            params = tuple(compiled.params[key] for key in compiled.positiontup)
            plan = await connection.exec_driver_sql("EXPLAIN QUERY PLAN " + str(compiled), params)
            plans.extend(row[3] for row in plan)
            result = (await execute(statement, *args, **kwargs)).freeze()
            sizes.append(len(result().all()))
            return result()

        monkeypatch.setattr(db, "execute", measured)
        # Repeated / unsorted input codes must retain original IN-set semantics.
        actual = await collector._load_average_daily_volume(db, codes[::-1] + codes[:5], DAY)
        assert actual == expected
        assert len(sizes) == 3, "100-code chunks bound bind count and event-loop work"
        assert max(sizes) <= 500 and sum(sizes) == 205*5
        accesses = [p for p in plans if "stock_kline" in p]
        assert len(accesses) == 205
        assert all("SEARCH stock_kline USING INDEX" in p and "code=? AND trade_date<?" in p
                   for p in accesses), accesses


@pytest.mark.asyncio
async def test_empty_history_makes_no_query():
    db = AsyncMock()
    assert await AuctionCollector()._load_average_daily_volume(db, [], DAY) == {}
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_sparse_observations_and_date_scope_do_not_use_cache(maker):
    collector = AuctionCollector()
    async with maker() as db:
        # Latest valid entries can be years apart; no fixed calendar lookback.
        for days, value in ((2, 10), (400, 20), (800, 30), (1200, 40), (1600, 50), (2000, 900)):
            db.add(StockKline(code="600000", trade_date=DAY-timedelta(days=days), volume=value))
        db.add(StockKline(code="600001", trade_date=DAY-timedelta(days=1), volume=0))
        await db.commit()
        assert await collector._load_average_daily_volume(
            db, ["600000", "600001", "999999"], DAY
        ) == {"600000": 30.0}
        assert await collector._load_average_daily_volume(
            db, ["600000"], DAY-timedelta(days=500)
        ) == {"600000": 255.0}
        db.add(StockKline(code="600000", trade_date=DAY-timedelta(days=1), volume=100))
        await db.commit()
        assert await collector._load_average_daily_volume(db, ["600000"], DAY) == {"600000": 40.0}
