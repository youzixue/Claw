"""Auction latency repairs: isolated source shapes, never historical fill evidence."""
import asyncio
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.stock import AuctionData, StockKline
from app.strategy.auction import AuctionCollector

DAY = date(2026, 9, 24)
AT = datetime(2026, 9, 24, 9, 24, 31)


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auction.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def frame(rows):
    data = pd.DataFrame(rows)
    data.attrs["auction_observed_at"] = AT
    return data


def row(code="600001", **changes):
    return dict(code=code, open=10.5, prev_close=10, volume=200, amount=210000,
                volume_ratio=0, volume_basis="intraday_cumulative", volume_unit="share",
                amount_unit="CNY", source="sina", source_version="akshare_spot_open_v1_unverified",
                source_quote_at=None, received_at=AT, price_basis="spot_open_unverified", **changes)


@pytest.mark.asyncio
async def test_unverified_fallback_never_reads_irrelevant_full_market_history(maker, monkeypatch):
    collector = AuctionCollector()
    history = AsyncMock(side_effect=AssertionError("ordinary cumulative volume cannot use history"))
    monkeypatch.setattr(collector, "_load_average_daily_volume", history)
    data = frame([row(f"60{i:04d}") for i in range(300)])
    async with maker() as db:
        assert await collector.save_auction_data(db, data, DAY) == 300
        records = (await db.scalars(select(AuctionData).order_by(AuctionData.code))).all()
        assert len(records) == 300
        assert all(r.volume_ratio == 0 and r.auction_price == 10.5 for r in records)
        assert all(r.source_quote_at is None and r.observed_at == AT for r in records)
        assert all(r.volume_basis == "intraday_cumulative" for r in records)
    history.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_scope_only_rows_that_can_reach_original_formula(maker, monkeypatch):
    collector = AuctionCollector()
    history = AsyncMock(return_value={"600001": 100000, "600006": 100000})
    monkeypatch.setattr(collector, "_load_average_daily_volume", history)
    rows = [
        row("600001") | {"volume_basis": "auction_matched", "volume_unit": "lot100"},
        row("600002"),
        row("600003") | {"volume_basis": "auction_matched", "volume_unit": "unknown"},
        row("600004") | {"volume_basis": "auction_matched", "open": 0},
        row("600005") | {"volume_ratio": 3.14159},
        row("600006") | {"volume_basis": "indicative_matched", "volume_unit": "share"},
    ]
    async with maker() as db:
        assert await collector.save_auction_data(db, frame(rows), DAY) == 5
        history.assert_awaited_once_with(db, ["600001", "600006"], DAY)
        records = (await db.scalars(select(AuctionData).order_by(AuctionData.code))).all()
        assert {r.code: r.volume_ratio for r in records} == {
            "600001": 4.0, "600002": 0.0, "600003": 0.0, "600005": 3.142, "600006": 0.04}


@pytest.mark.asyncio
async def test_empty_or_invalid_clock_has_no_history_or_write(maker, monkeypatch):
    collector = AuctionCollector()
    history = AsyncMock(side_effect=AssertionError("unused history"))
    monkeypatch.setattr(collector, "_load_average_daily_volume", history)
    async with maker() as db:
        assert await collector.save_auction_data(db, frame([]), DAY) == 0
        data = frame([row()])
        data.attrs["auction_observed_at"] = AT + timedelta(minutes=10)
        assert await collector.save_auction_data(db, data, DAY) == 0
        assert list((await db.scalars(select(AuctionData))).all()) == []
    history.assert_not_awaited()


@pytest.mark.asyncio
async def test_genuine_history_latest_five_positive_and_excludes_today(maker):
    collector = AuctionCollector()
    async with maker() as db:
        for n, value in enumerate([0, -5, 10, 20, None, 30, 40, 50, 60], start=1):
            db.add(StockKline(code="600001", trade_date=DAY-timedelta(days=n), volume=value))
        db.add(StockKline(code="600001", trade_date=DAY, volume=999999))
        await db.commit()
        result = await collector._load_average_daily_volume(db, ["600001"], DAY)
        assert result == {"600001": float(np.mean([10, 20, 30, 40, 50]))}


@pytest.mark.asyncio
async def test_history_bounds_rows_without_changing_positive_observation_mean(maker, monkeypatch):
    collector = AuctionCollector()
    values = [float("inf"), None, -1, 0, 10, float("-inf"), 20, 30, 40, 50, 999]
    async with maker() as db:
        db.add_all([
            StockKline(code=code, trade_date=DAY-timedelta(days=n), volume=value)
            for code in ("600001", "600002") for n, value in enumerate(values, start=1)
        ])
        db.add(StockKline(code="600003", trade_date=DAY-timedelta(days=1), volume=70))
        db.add(StockKline(code="600001", trade_date=DAY, volume=999999))
        db.add(StockKline(code="600001", trade_date=DAY+timedelta(days=1), volume=999999))
        await db.commit()
        execute = db.execute
        sizes = []

        async def measured(statement, *args, **kwargs):
            result = (await execute(statement, *args, **kwargs)).freeze()
            sizes.append(len(result().all()))
            return result()

        monkeypatch.setattr(db, "execute", measured)
        result = await collector._load_average_daily_volume(
            db, ["600001", "600002", "600003", "600004"], DAY)
        assert sizes == [11], "只物化每股最新五个正有限历史量，不能加载全历史"
        assert result == {"600001": 30.0, "600002": 30.0, "600003": 70.0}


@pytest.mark.asyncio
async def test_ratio_repair_only_reads_history_for_otherwise_verified_frames(maker, monkeypatch):
    collector = AuctionCollector()
    history = AsyncMock(return_value={"600001": 100000})
    monkeypatch.setattr(collector, "_load_average_daily_volume", history)
    def candidate(code, **changes):
        fields = dict(
            code=code, trade_date=DAY, auction_time=AT.strftime("%H:%M:%S"),
            auction_price=10.5, prev_close=10, auction_volume=200, auction_amount=210000,
            volume_ratio=0, volume_basis="indicative_matched", volume_unit="lot100",
            amount_unit="CNY", source="tencent", source_version="isolated",
            source_quote_at=AT-timedelta(seconds=3), received_at=AT, observed_at=AT,
            price_basis="indicative_match",
        )
        fields.update(changes)
        return AuctionData(**fields)

    rows = [candidate("600001"), candidate("600002", received_at=None),
            candidate("600003", source_quote_at=AT-timedelta(minutes=3)),
            candidate("600004", volume_basis="intraday_cumulative"),
            candidate("600005", volume_ratio=2.5)]
    async with maker() as db:
        await collector._resolve_frame_volume_ratios(db, rows, DAY, decision_at=AT)
        history.assert_awaited_once_with(db, ["600001"], DAY)
    assert [r.volume_ratio for r in rows] == [4.0, 0, 0, 0, 2.5]


@pytest.mark.asyncio
@pytest.mark.parametrize("provided,history_value,clock_change,expected_ratio,reads_history", [
    (0, 100000, {}, 4.0, True),
    (0, None, {}, None, True),
    (2.75, None, {}, 2.75, False),
    (0, 100000, {"received_at": None}, None, False),
    (0, 100000, {"observed_at": datetime(2026, 9, 24, 9, 25, 31)}, None, False),
    (0, 100000, {"f124": int(datetime(2026, 9, 24, 9, 24).timestamp())}, None, False),
])
async def test_eastmoney_missing_ratio_uses_same_guarded_formula(
    maker, monkeypatch, provided, history_value, clock_change, expected_ratio, reads_history,
):
    collector = AuctionCollector()
    observed = datetime(2026, 9, 24, 9, 25, 10)
    quote = {
        "f12": "600001", "f17": 10.5, "f18": 10, "f5": 200,
        "f6": 210000, "f10": provided,
        "f124": int(datetime(2026, 9, 24, 9, 25, 5).timestamp()),
        "received_at": observed, "observed_at": observed, **clock_change,
    }
    monkeypatch.setattr(collector, "_fetch_eastmoney_quotes",
                        AsyncMock(return_value={"600001": quote}))
    history = AsyncMock(return_value={} if history_value is None else {"600001": history_value})
    monkeypatch.setattr(collector, "_load_average_daily_volume", history)
    async with maker() as db:
        result = await collector.collect_eastmoney_auction_evidence(
            db, DAY, now=observed, codes=["600001"])
        records = (await db.scalars(select(AuctionData))).all()
        if reads_history:
            history.assert_awaited_once_with(db, ["600001"], DAY)
        else:
            history.assert_not_awaited()
        assert result["written"] == int(expected_ratio is not None)
        if expected_ratio is not None:
            assert records[0].volume_ratio == expected_ratio
            assert records[0].auction_volume == 200
            assert records[0].volume_unit == "lot100"
            assert records[0].auction_amount == 210000
            assert records[0].observed_at == observed
        else:
            assert records == []

