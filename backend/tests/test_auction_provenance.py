from datetime import date, datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import create_async_engine

from app.data.auction_evidence import auction_context_complete, auction_evidence_status, volume_in_shares
from app.models.stock import AuctionData
from app.strategy.auction import AuctionAnalyzer, AuctionCollector
from auction_test_evidence import verified_auction_fields
from test_auction_data import auction_db_env  # noqa: F401

DAY = date(2026, 9, 7)
NOW = datetime(2026, 9, 7, 9, 26)


def row_at(clock="09:25:00", **overrides):
    values = dict(
        code="000001", trade_date=DAY, auction_time=clock,
        auction_price=10.5, prev_close=10, auction_volume=100_000,
        auction_amount=1_050_000, volume_ratio=2.2, open_change=5,
        **verified_auction_fields(DAY, clock),
    )
    values.update(overrides)
    return AuctionData(**values)


@pytest.mark.parametrize("changes,status", [
    ({}, "ok"),
    ({"source_quote_at": None}, "unknown"),
    ({"received_at": pd.NaT}, "unknown"),
    ({"source": None}, "unknown"),
    ({"source_quote_at": NOW + timedelta(seconds=1)}, "future"),
    ({"source_quote_at": None, "observed_at": NOW + timedelta(seconds=1)}, "future"),
    ({"received_at": datetime(2026, 9, 7, 9, 24, 59)}, "invalid_clock"),
    ({"auction_time": "09:20:00"}, "invalid_clock"),
    ({"source_quote_at": datetime(2026, 9, 6, 9, 25)}, "invalid_clock"),
    ({"source_quote_at": datetime(2026, 9, 7, 9, 24, 29)}, "stale_source"),
    ({"price_basis": "spot_open_unverified"}, "unverified_basis"),
    ({"volume_basis": "intraday_cumulative"}, "unverified_basis"),
    ({"price_basis": "indicative_match", "volume_basis": "indicative_matched"}, "unverified_basis"),
    ({"volume_unit": None}, "unknown_unit"),
    ({"amount_unit": "wan_CNY"}, "unknown_unit"),
    ({"auction_volume": 0}, "incomplete_values"),
    ({"auction_amount": float("inf")}, "incomplete_values"),
    ({"volume_ratio": None}, "incomplete_values"),
    ({"auction_price": True}, "incomplete_values"),
])
def test_auction_status_does_not_infer_missing_provenance(changes, status):
    row = row_at(**changes)
    assert auction_evidence_status(row, decision_at=NOW) == status


@pytest.mark.parametrize("missing", ["auction_feed_complete", "auction_evidence_status", "auction_evidence_contract"])
def test_old_prediction_flags_cannot_promote_unverified_auction(missing):
    from app.api.v1.promotion import _is_auction_surge_sprint_candidate
    factors = {
        "auction_feed_complete": True, "auction_evidence_status": "ok",
        "auction_evidence_contract": "auction_provenance_v1",
        "auction_strength_score": 80, "auction_open_change": 3.0,
    }
    candidate = {"probability": 0.5, "route_score": 70, "support_strength_score": 70,
                 "sector_strength_score": 70, "change_pct": 3.0, "volume_ratio": 2.0,
                 "probability_factors": factors}
    assert auction_context_complete(factors)
    assert _is_auction_surge_sprint_candidate(candidate)
    factors.pop(missing)
    assert not auction_context_complete(factors)
    assert not _is_auction_surge_sprint_candidate(candidate)


def test_clock_phase_and_timezone_boundaries():
    row = row_at("09:24:00")
    assert auction_evidence_status(row, decision_at=NOW) == "ok"
    row.source_quote_at = row.source_quote_at.replace(tzinfo=timezone(timedelta(hours=8)))
    assert auction_evidence_status(row, decision_at=NOW) == "ok"
    assert auction_evidence_status(row, decision_at=NOW - timedelta(minutes=3)) == "future"
    row = row_at("09:25:30", source_quote_at=datetime(2026, 9, 7, 9, 25))
    assert auction_evidence_status(row, decision_at=NOW) == "ok"
    row = row_at("09:25:31")
    assert auction_evidence_status(row, decision_at=NOW) == "invalid_clock"
    assert auction_evidence_status(row_at("09:14:59"), decision_at=NOW) == "invalid_clock"


@pytest.mark.parametrize("unit,expected", [("share", 0.01), ("lot100", 1.0), (None, 0.0)])
def test_ratio_uses_declared_units_not_volume_magnitude(unit, expected):
    row = pd.Series({"volume": 5000, "volume_unit": unit, "volume_basis": "indicative_matched"})
    assert AuctionCollector()._resolve_volume_ratio(row, 10_000_000) == expected
    assert volume_in_shares(float("inf"), unit) is None
    row["volume_basis"] = "intraday_cumulative"
    assert AuctionCollector()._resolve_volume_ratio(row, 10_000_000) == 0


@pytest.mark.parametrize("value", [True, "inf万", "NaN%", float("inf")])
def test_collector_never_persists_nonfinite_sentinels(value):
    assert AuctionCollector._safe_float(value) == 0


@pytest.mark.asyncio
async def test_positive_legacy_rows_remain_unknown_in_all_consumers(auction_db_env):
    from app.api.v1.promotion import _build_auction_snapshot_health, _load_auction_surge_context_map
    async with auction_db_env() as db:
        legacy = row_at(auction_volume=3_000_000, auction_amount=31_500_000)
        for key in verified_auction_fields(DAY, "09:25:00"):
            setattr(legacy, key, None)
        db.add(legacy)
        await db.commit()
        before = (legacy.auction_price, legacy.auction_volume, legacy.source_quote_at)
        health = await AuctionCollector().get_snapshot_health(db, DAY, as_of_at=NOW)
        promotion_health = await _build_auction_snapshot_health(db, DAY)
        context = await _load_auction_surge_context_map(db, DAY, exclude_codes=set())
        factors = await AuctionAnalyzer().get_auction_factors("000001", db, DAY)
        signals = await AuctionAnalyzer().get_strong_auctions(db, DAY, min_score=30)
        assert health["feed_complete_count"] == promotion_health["feed_complete_count"] == 0
        assert health["evidence_status_counts"] == {"unknown": 1}
        assert not context["000001"]["auction_feed_complete"]
        assert factors["auction_volume_ratio"] is None
        assert factors["auction_evidence_status"] == "unknown"
        assert not signals
        await db.refresh(legacy)
        assert (legacy.auction_price, legacy.auction_volume, legacy.source_quote_at) == before


@pytest.mark.asyncio
async def test_receipt_replay_does_not_count_as_two_source_frames(auction_db_env):
    async with auction_db_env() as db:
        db.add_all([row_at(), row_at("09:25:10", source_quote_at=datetime(2026, 9, 7, 9, 25))])
        await db.commit()
        health = await AuctionCollector().get_snapshot_health(db, DAY, as_of_at=NOW)
        assert health["feed_complete_count"] == 1
        assert health["multi_frame_complete_count"] == 0
        assert health["path_degraded"] is True


@pytest.mark.asyncio
async def test_real_source_fields_survive_save_and_unknown_receipt_is_not_relabelled(auction_db_env):
    collector = AuctionCollector()
    async with auction_db_env() as db:
        frame = pd.DataFrame([dict(code="000001", open=10.5, prev_close=10,
                                   volume=100_000, amount=1_050_000, volume_ratio=2.2,
                                   **verified_auction_fields(DAY, "09:25:00"))])
        frame.attrs["auction_observed_at"] = datetime(2026, 9, 7, 9, 25, 2)
        assert await collector.save_auction_data(db, frame, DAY, "09:25:00") == 1
        row = (await db.scalars(sa.select(AuctionData))).one()
        assert row.auction_time == "09:25:02"
        assert row.source_quote_at == row.received_at == datetime(2026, 9, 7, 9, 25)
        assert auction_evidence_status(row, decision_at=NOW) == "ok"
        frame = frame.drop(columns=["source_quote_at", "received_at"])
        frame.attrs["auction_observed_at"] = datetime(2026, 9, 7, 9, 25, 4)
        assert await collector.save_auction_data(db, frame, DAY) == 1
        rows = (await db.scalars(sa.select(AuctionData).order_by(AuctionData.auction_time))).all()
        assert rows[-1].source_quote_at is None and rows[-1].received_at is None
        assert auction_evidence_status(rows[-1], decision_at=NOW) == "unknown"
        health = await collector.get_snapshot_health(db, DAY, as_of_at=NOW)
        assert health["feed_complete_count"] == 0  # no fallback to older valid final


@pytest.mark.asyncio
async def test_known_future_frame_cannot_be_prediction_seed(auction_db_env):
    from app.api.v1.promotion import _load_auction_surge_context_map
    async with auction_db_env() as db:
        db.add(row_at(auction_volume=3_000_000, auction_amount=31_500_000))
        await db.commit()
        context = await _load_auction_surge_context_map(
            db, DAY, exclude_codes=set(), as_of_at=datetime(2026, 9, 7, 9, 24)
        )
        assert context == {}


@pytest.mark.asyncio
async def test_receipt_after_0924_does_not_make_pre0924_source_timely(auction_db_env):
    async with auction_db_env() as db:
        db.add_all([
            row_at("09:23:40"),
            row_at("09:24:00", source_quote_at=datetime(2026, 9, 7, 9, 23, 50)),
        ])
        await db.commit()
        health = await AuctionCollector().get_snapshot_health(db, DAY, as_of_at=NOW)
        assert health["multi_frame_complete_count"] == 1
        assert health["timely_snapshot_count"] == 1
        assert health["verified_timely_snapshot_count"] == 0
        assert health["stale"] is True


@pytest.mark.asyncio
async def test_auction_migration_is_nullable_idempotent_and_preserves_history(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy_auction.db'}")
    path = Path(__file__).resolve().parents[1] / "alembic/versions/026_auction_evidence.py"
    spec = importlib.util.spec_from_file_location("auction_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    def exercise(connection):
        metadata = sa.MetaData()
        legacy = sa.Table("auction_data", metadata,
                          sa.Column("id", sa.Integer, primary_key=True),
                          sa.Column("auction_time", sa.String),
                          sa.Column("auction_volume", sa.Integer))
        metadata.create_all(connection)
        connection.execute(legacy.insert().values(id=1, auction_time="09:25:00", auction_volume=123))
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        assert connection.execute(sa.select(legacy)).one() == (1, "09:25:00", 123)
        columns = ", ".join(migration.COLUMNS)
        assert connection.execute(sa.text(f"SELECT {columns} FROM auction_data")).one() == (None,) * len(migration.COLUMNS)
        migration.downgrade()
        assert connection.execute(sa.select(legacy)).one() == (1, "09:25:00", 123)
        assert "source_quote_at" not in {c["name"] for c in sa.inspect(connection).get_columns("auction_data")}

    try:
        async with engine.begin() as conn:
            await conn.run_sync(exercise)
    finally:
        await engine.dispose()
