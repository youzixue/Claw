"""Prospective frame storage only: no historical reconstruction or live network."""
import importlib.util
import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.db.session import Base
from app.models.stock import AuctionData, StockTag
from app.strategy.auction import AuctionCollector, AuctionAnalyzer
from app.api.v1 import promotion

DAY = date(2026, 9, 22)
AT = datetime(2026, 9, 22, 9, 25, 12)


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'frames.db'}")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def frame(source="tencent", second=9, observed=AT, **changes):
    values = dict(code="600001", trade_date=DAY, auction_time=observed.strftime("%H:%M:%S"),
        auction_price=10.5, prev_close=10, auction_volume=200, auction_amount=210000,
        volume_ratio=3, source=source, source_version=source+"_fixture",
        source_quote_at=datetime(2026, 9, 22, 9, 25, second),
        received_at=observed, observed_at=observed, price_basis="auction_opening",
        volume_basis="auction_matched", volume_unit="lot100", amount_unit="CNY")
    return AuctionData(**(values | changes))


async def save(db, row):
    return await AuctionCollector()._save_verified_auction_candidates(
        db, [row], row.observed_at, DAY, source_label=row.source)


@pytest.mark.asyncio
@pytest.mark.parametrize("second_source", ["eastmoney", "tencent"])
async def test_independent_source_frames_same_second_survive(maker, second_source):
    async with maker() as db:
        db.add(StockTag(code="600001", board_tag="tradeable", board_type="main_sh"))
        await db.commit()
        await save(db, frame())
        await save(db, frame(second_source, 10))
        rows = list((await db.scalars(sa.select(AuctionData).order_by(AuctionData.id))).all())
        assert len(rows) == 2, "second source update must not replace the first source frame"
        assert [r.source_quote_at.second for r in rows] == [9, 10]
        assert [r.auction_time for r in rows] == ["09:25:12"] * 2
        health = await AuctionCollector().get_snapshot_health(db, DAY, as_of_at=AT)
        assert health["latest_code_count"] == 1
        assert health["snapshot_count"] == 1
        assert health["multi_frame_complete_count"] == 1
        page = await promotion._build_auction_snapshot_health(db, DAY)
        assert page["latest_code_count"] == 1


@pytest.mark.asyncio
async def test_refetch_preserves_original_payload_and_clock(maker):
    async with maker() as db:
        first = await save(db, frame())
        repeated = await save(db, frame(observed=AT+timedelta(seconds=1), auction_price=11))
        assert first["written"] == 1
        assert repeated["accepted"] == 1 and repeated["written"] == 0
        rows = list((await db.scalars(sa.select(AuctionData))).all())
        assert len(rows) == 1, "same source quote is not an independent frame"
        assert rows[0].auction_price == 10.5
        assert rows[0].observed_at == AT
        assert rows[0].auction_time == "09:25:12"


@pytest.mark.asyncio
async def test_same_source_clock_across_sources_does_not_relax_multiframe(maker):
    async with maker() as db:
        await save(db, frame())
        await save(db, frame("eastmoney", 9))
        rows = list((await db.scalars(sa.select(AuctionData))).all())
        assert len(rows) == 2
        health = await AuctionCollector().get_snapshot_health(db, DAY, as_of_at=AT)
        assert health["latest_code_count"] == 1
        assert health["multi_frame_complete_count"] == 0


@pytest.mark.asyncio
async def test_legacy_same_second_is_not_overwritten_or_given_provenance(maker):
    async with maker() as db:
        legacy = AuctionData(code="600001", trade_date=DAY, auction_time="09:25:12",
                             auction_price=9, prev_close=10)
        db.add(legacy)
        await db.commit()
        old_id = legacy.id
        await save(db, frame())
        await db.refresh(legacy)
        assert legacy.id == old_id and legacy.auction_price == 9
        assert legacy.source_quote_at is None
        assert legacy.source is None
        assert len(list((await db.scalars(sa.select(AuctionData))).all())) == 2


@pytest.mark.asyncio
async def test_latest_tie_uses_observed_clock_without_quality_fallback(maker):
    async with maker() as db:
        # Legacy/manual rows remain readable, but same-second latest must be one
        # deterministic row, even if it is lower quality.
        old = frame(observed=AT.replace(microsecond=100))
        bad = frame("eastmoney", 10, AT.replace(microsecond=200), volume_ratio=0)
        db.add_all([old, bad])
        await db.commit()
        health = await AuctionCollector().get_snapshot_health(db, DAY, as_of_at=AT+timedelta(seconds=1))
        assert health["latest_code_count"] == 1
        assert health["feed_complete_count"] == 0
        page = await promotion._build_auction_snapshot_health(db, DAY)
        assert page["latest_code_count"] == 1 and page["feed_complete_count"] == 0
        factors = await AuctionAnalyzer().get_auction_factors("600001", db, DAY)
        assert factors["auction_feed_complete"] is False


@pytest.mark.asyncio
async def test_concurrent_same_frame_is_idempotent(maker):
    async def write_one():
        async with maker() as db:
            await save(db, frame())
    await asyncio.gather(write_one(), write_one())
    async with maker() as db:
        rows = list((await db.scalars(sa.select(AuctionData))).all())
        assert len(rows) == 1
        assert rows[0].observed_at == AT


@pytest.mark.asyncio
async def test_independent_frames_and_latest_selection_match_quality_watermark(maker):
    from app.core.prediction_data_quality import PredictionDataQualityAuditor
    async with maker() as db:
        db.add(StockTag(code="600001", board_tag="tradeable", board_type="main_sh"))
        await db.commit()
        await save(db, frame())
        await save(db, frame("eastmoney", 10))
        marks = await PredictionDataQualityAuditor()._build_watermarks(
            db, DAY, "promotion_0925", as_of_at=AT)
        mark = next(item for item in marks if item["dataset"] == "auction_data")
        assert mark["record_count"] == 1
        assert mark["expected_count"] == 1
        assert mark["completeness"] == 1
        assert mark["details"]["auction_health"]["latest_code_count"] == 1


@pytest.mark.asyncio
async def test_036_preserves_every_legacy_value_and_refuses_lossy_downgrade(tmp_path):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/036_auction_source_frames.py"
    assert path.exists(), "formal 036 migration is required"
    spec = importlib.util.spec_from_file_location("auction_036", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "035_shared_paper_portfolio"
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    def exercise(conn):
        metadata = sa.MetaData()
        table = AuctionData.__table__.to_metadata(metadata)
        # Build exact pre-036 schema without touching any running database.
        if "source_frame_key" in table.c:
            table._columns.remove(table.c.source_frame_key)
            for constraint in list(table.constraints):
                if constraint.name == "uq_auction_source_frame_key":
                    table.constraints.remove(constraint)
            for index in list(table.indexes):
                if index.name == "ix_auction_date_code_time":
                    table.indexes.remove(index)
        table.append_constraint(sa.UniqueConstraint("code", "trade_date", "auction_time",
                                                    name="uq_auction_code_date_time"))
        metadata.create_all(conn)
        conn.execute(table.insert().values(id=71, code="600001", trade_date=DAY,
                                          auction_time="09:25:12", auction_price=9))
        before = conn.execute(sa.select(table)).one()
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()
        migration.upgrade()  # already-correct 036 shape is explicitly verified
        assert conn.execute(sa.select(table)).one() == before
        assert conn.execute(sa.text("SELECT source_frame_key FROM auction_data")).scalar() is None
        unique = {x["name"] for x in sa.inspect(conn).get_unique_constraints("auction_data")}
        assert "uq_auction_code_date_time" not in unique
        assert "uq_auction_source_frame_key" in unique
        assert {x["name"]: x["column_names"] for x in sa.inspect(conn).get_indexes("auction_data")}["ix_auction_date_code_time"] == ["trade_date", "code", "auction_time"]
        conn.execute(sa.text("INSERT INTO auction_data(code,trade_date,auction_time,source_frame_key) "
                             "VALUES ('600001','2026-09-22','09:25:12',:key)"), {"key": "a"*64})
        with pytest.raises(RuntimeError):
            migration.downgrade()
        assert conn.execute(sa.text("SELECT count(*) FROM auction_data")).scalar() == 2
    try:
        async with engine.begin() as c:
            await c.run_sync(exercise)
    finally:
        await engine.dispose()
