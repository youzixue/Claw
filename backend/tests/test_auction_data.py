from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models import stock as stock_models  # noqa: F401
from app.models.stock import AuctionData, StockKline, StockSpot, StockTag
from app.strategy.auction import AuctionAnalyzer, AuctionCollector
from auction_test_evidence import verified_auction_fields


@pytest_asyncio.fixture
async def auction_db_env(tmp_path: Path):
    db_path = tmp_path / "auction.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        yield SessionLocal
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_auction_data_upserts_snapshot_time_and_fills_volume_ratio(auction_db_env):
    SessionLocal = auction_db_env
    collector = AuctionCollector()

    async with SessionLocal() as session:
        for offset, volume in enumerate([10_000_000, 11_000_000, 9_000_000, 10_500_000, 9_500_000], start=1):
            session.add(
                StockKline(
                    code="000001",
                    trade_date=date(2026, 4, 20 + offset),
                    open=9.8,
                    close=10.0,
                    high=10.1,
                    low=9.7,
                    volume=volume,
                    amount=100_000_000,
                    turnover=4.0,
                    change_pct=1.0,
                    prev_close=9.9,
                    source="test",
                )
            )
        await session.commit()

        df = pd.DataFrame(
            [
                {"code": "000001", "name": "竞价A", "open": 10.3, "prev_close": 10.0, "volume": 5_000, "amount": 22_000_000},
                {"code": "000002", "name": "竞价B", "open": 8.3, "prev_close": 8.0, "volume": 20_000, "amount": 11_000_000, "volume_ratio": 2.8},
            ]
        )
        df.attrs["auction_observed_at"] = datetime(2026, 4, 27, 9, 24, 30)
        for key, value in verified_auction_fields(date(2026, 4, 27), "09:24:30").items():
            df[key] = value
        saved = await collector.save_auction_data(session, df, date(2026, 4, 27), auction_time="09:24:30")
        saved_again = await collector.save_auction_data(session, df, date(2026, 4, 27), auction_time="09:24:30")
        total = await session.scalar(select(func.count()).select_from(AuctionData))
        row = await session.scalar(
            select(AuctionData).where(
                AuctionData.code == "000001",
                AuctionData.trade_date == date(2026, 4, 27),
                AuctionData.auction_time == "09:24:30",
            )
        )

    assert saved == 2
    assert saved_again == 0  # 同一源帧被唯一键去重；返回实际新增行数而非候选数。
    assert total == 2
    assert row is not None
    assert row.open_change == pytest.approx(3.0)
    assert row.volume_ratio > 0


@pytest.mark.asyncio
async def test_auction_data_drops_zero_price_sentinel_instead_of_storing_minus_100(auction_db_env):
    SessionLocal = auction_db_env
    collector = AuctionCollector()
    df = pd.DataFrame([
        {"code": "000001", "name": "无效竞价", "open": 0, "prev_close": 10.0, "volume": 0, "amount": 0},
        {"code": "000002", "name": "有效竞价", "open": 8.2, "prev_close": 8.0, "volume": 20_000, "amount": 10_000_000},
    ])

    df.attrs["auction_observed_at"] = datetime(2026, 4, 27, 9, 20)
    async with SessionLocal() as session:
        saved = await collector.save_auction_data(
            session,
            df,
            date(2026, 4, 27),
            auction_time="09:20:00",
        )
        rows = list((await session.execute(select(AuctionData))).scalars().all())

    assert saved == 1
    assert [row.code for row in rows] == ["000002"]
    assert rows[0].open_change == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_auction_snapshot_health_marks_price_only_rows_degraded(auction_db_env):
    SessionLocal = auction_db_env
    collector = AuctionCollector()

    async with SessionLocal() as session:
        session.add(
            AuctionData(
                code="000001",
                trade_date=date(2026, 4, 27),
                auction_time="09:25:00",
                auction_price=10.3,
                prev_close=10.0,
                open_change=3.0,
                auction_volume=0,
                auction_amount=0,
                volume_ratio=0,
            )
        )
        await session.commit()
        health = await collector.get_snapshot_health(session, date(2026, 4, 27))
        ensured = await collector.ensure_auction_data_snapshot(
            session,
            date(2026, 4, 27),
            auction_time="09:25:00",
        )

    assert health["missing"] is False
    assert health["degraded"] is True
    assert health["feed_complete_count"] == 0
    assert ensured["status"] == "degraded"
    assert ensured["saved"] == 0


@pytest.mark.asyncio
async def test_auction_snapshot_health_requires_two_positive_frames(auction_db_env):
    SessionLocal = auction_db_env
    collector = AuctionCollector()
    target = date(2026, 4, 27)

    async with SessionLocal() as session:
        session.add(
            AuctionData(
                code="000001",
                trade_date=target,
                auction_time="09:25:00",
                **verified_auction_fields(target, "09:25:00"),
                auction_price=10.3,
                prev_close=10.0,
                auction_volume=100_000,
                auction_amount=1_000_000,
                volume_ratio=1.5,
            )
        )
        await session.commit()
        health = await collector.get_snapshot_health(session, target)

    assert health["feed_complete_count"] == 1
    assert health["multi_frame_complete_count"] == 0
    assert health["path_degraded"] is True
    assert health["status"] == "degraded"


@pytest.mark.asyncio
async def test_auction_snapshot_health_scopes_to_tagged_tradeable_universe(auction_db_env):
    SessionLocal = auction_db_env
    collector = AuctionCollector()
    target = date(2026, 4, 27)

    async with SessionLocal() as session:
        session.add_all(
            [
                StockTag(
                    code="000001",
                    name="主板",
                    board_type="main_sz",
                    board_tag="tradeable",
                    is_st=False,
                    is_suspended=False,
                    is_delisting=False,
                ),
                StockTag(
                    code="300001",
                    name="创业板",
                    board_type="gem",
                    board_tag="observe_only",
                    is_st=False,
                    is_suspended=False,
                    is_delisting=False,
                ),
                AuctionData(
                    code="000001",
                    trade_date=target,
                    auction_time="09:24:00",
                    **verified_auction_fields(target, "09:24:00"),
                    auction_price=10.2,
                    prev_close=10.0,
                    auction_volume=80_000,
                    auction_amount=800_000,
                    volume_ratio=1.2,
                ),
                AuctionData(
                    code="000001",
                    trade_date=target,
                    auction_time="09:25:00",
                    **verified_auction_fields(target, "09:25:00"),
                    auction_price=10.3,
                    prev_close=10.0,
                    auction_volume=100_000,
                    auction_amount=1_000_000,
                    volume_ratio=1.5,
                ),
                AuctionData(
                    code="300001",
                    trade_date=target,
                    auction_time="09:25:00",
                    **verified_auction_fields(target, "09:25:00"),
                    auction_price=20.6,
                    prev_close=20.0,
                    auction_volume=100_000,
                    auction_amount=2_000_000,
                    volume_ratio=1.5,
                ),
            ]
        )
        await session.commit()
        health = await collector.get_snapshot_health(session, target)

    assert health["coverage_scope"] == "tradeable_stock_tags"
    assert health["raw_snapshot_count"] == 2
    assert health["snapshot_count"] == 1
    assert health["tradeable_universe_count"] == 1
    assert health["feed_complete_count"] == 1
    assert health["multi_frame_complete_count"] == 1
    assert health["path_degraded"] is False
    assert health["status"] == "ok"


@pytest.mark.asyncio
async def test_auction_analyzer_uses_latest_snapshot_per_code(auction_db_env):
    SessionLocal = auction_db_env
    analyzer = AuctionAnalyzer()

    async with SessionLocal() as session:
        session.add_all(
            [
                AuctionData(
                    code="000001",
                    trade_date=date(2026, 4, 27),
                    auction_time="09:20:00",
                    auction_price=10.2,
                    prev_close=10.0,
                    open_change=2.0,
                    volume_ratio=1.0,
                    auction_amount=8_000_000,
                ),
                AuctionData(
                    code="000001",
                    trade_date=date(2026, 4, 27),
                    auction_time="09:25:00",
                    **verified_auction_fields(date(2026, 4, 27), "09:25:00"),
                    auction_volume=100_000,
                    auction_price=10.5,
                    prev_close=10.0,
                    open_change=5.0,
                    volume_ratio=2.2,
                    auction_amount=30_000_000,
                ),
            ]
        )
        await session.commit()
        signals = await analyzer.analyze(session, date(2026, 4, 27))
        factors = await analyzer.get_auction_factors("000001", session, date(2026, 4, 27))

    assert len(signals) == 1
    assert signals[0].open_change == 5.0
    assert factors["auction_open_change"] == 5.0
    assert factors["auction_volume_ratio"] == 2.2


@pytest.mark.asyncio
async def test_auction_collector_falls_back_to_sina_live_feed(monkeypatch, auction_db_env):
    SessionLocal = auction_db_env
    collector = AuctionCollector()
    ak = pytest.importorskip("akshare")

    from app.strategy import auction as auction_module

    class AuctionClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 4, 27, 9, 24, 30)

    def failed_eastmoney():
        raise ConnectionError("eastmoney disconnected")

    monkeypatch.setattr(auction_module, "datetime", AuctionClock)
    monkeypatch.setattr(ak, "stock_zh_a_spot_em", failed_eastmoney)
    monkeypatch.setattr(
        ak,
        "stock_zh_a_spot",
        lambda: pd.DataFrame(
            [
                {
                    "代码": "sz000001",
                    "名称": "新浪竞价",
                    "今开": 10.3,
                    "昨收": 10.0,
                    "成交量": 50_000,
                    "成交额": 20_000_000,
                    "涨跌幅": 3.0,
                }
            ]
        ),
    )

    async with SessionLocal() as session:
        df = await collector.collect_auction_data(session, date(2026, 4, 27))

    assert len(df) == 1
    assert df.iloc[0]["code"] == "sz000001"
    assert df.iloc[0]["open"] == pytest.approx(10.3)
    assert df.iloc[0]["prev_close"] == pytest.approx(10.0)
    assert df.iloc[0]["_auction_source"] == "sina"
    assert df.iloc[0]["source_quote_at"] is None
    assert df.iloc[0]["price_basis"] == "spot_open_unverified"
    assert df.iloc[0]["volume_basis"] == "intraday_cumulative"
    assert df.iloc[0]["volume_unit"] == "share"


@pytest.mark.asyncio
async def test_auction_collector_does_not_relabel_historical_stock_spot(monkeypatch, auction_db_env):
    SessionLocal = auction_db_env
    collector = AuctionCollector()

    ak = pytest.importorskip("akshare")

    monkeypatch.setattr(ak, "stock_zh_a_spot_em", lambda: pd.DataFrame())
    async with SessionLocal() as session:
        session.add(
            StockSpot(
                code="000001",
                name="竞价兜底",
                price=10.4,
                prev_close=10.0,
                open=10.3,
                change_pct=3.0,
                volume=50_000,
                amount=18_000_000,
                volume_ratio=2.6,
                turnover=1.2,
                updated_at=datetime(2026, 4, 27, 9, 24, 30),
            )
        )
        await session.commit()

        df = await collector.collect_auction_data(session, date(2026, 4, 27))
        saved = await collector.save_auction_data(session, df, date(2026, 4, 27), auction_time="09:25:00")
        row = await session.scalar(
            select(AuctionData).where(
                AuctionData.code == "000001",
                AuctionData.trade_date == date(2026, 4, 27),
                AuctionData.auction_time == "09:25:00",
            )
        )

    # 历史可变报价不是当时竞价的不可变采集证据；不能倒填9:25。
    assert df.empty
    assert saved == 0
    assert row is None


@pytest.mark.asyncio
async def test_auction_collector_keeps_missing_snapshot_missing_after_close(monkeypatch, auction_db_env):
    SessionLocal = auction_db_env
    collector = AuctionCollector()

    ak = pytest.importorskip("akshare")

    monkeypatch.setattr(ak, "stock_zh_a_spot_em", lambda: pd.DataFrame())
    async with SessionLocal() as session:
        session.add(
            StockSpot(
                code="000001",
                name="竞价确保",
                price=10.5,
                prev_close=10.0,
                open=10.4,
                change_pct=4.0,
                volume=60_000,
                amount=24_000_000,
                volume_ratio=3.1,
                turnover=1.6,
                updated_at=datetime(2026, 4, 27, 15, 0, 0),
            )
        )
        await session.commit()

        result = await collector.ensure_auction_data_snapshot(
            session,
            date(2026, 4, 27),
            auction_time="09:25:00",
        )
        row = await session.scalar(
            select(AuctionData).where(
                AuctionData.code == "000001",
                AuctionData.trade_date == date(2026, 4, 27),
                AuctionData.auction_time == "09:25:00",
            )
        )

    assert result["status"] == "missing"
    assert result["saved"] == 0
    assert result["feed_complete_count"] == 0
    assert row is None
