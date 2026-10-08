"""Synthetic fields diagnose possibilities, not proof of the missing live vendor field."""
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.data.auction_evidence import (
    auction_evidence_status, diagnose_missing_evidence_fields, merge_diagnoses,
)
from app.db.session import Base
from app.models.stock import AuctionData
from app.strategy.auction import AuctionCollector

DAY = date(2026, 9, 21)
AT = datetime(2026, 9, 21, 9, 25, 12)


def candidate(code="600001", **changes):
    values = dict(code=code, trade_date=DAY, auction_time="09:25:12",
        auction_price=10.5, prev_close=10, auction_volume=200, auction_amount=210000,
        volume_ratio=3.5, source="tencent", source_version="tencent_qt_auction_open_v1",
        source_quote_at=AT-timedelta(seconds=2), received_at=AT-timedelta(seconds=1),
        observed_at=AT, price_basis="auction_opening", volume_basis="auction_matched",
        volume_unit="lot100", amount_unit="CNY")
    return AuctionData(**(values | changes))


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auction.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("field,value", [
    ("auction_amount", 0), ("auction_volume", 0), ("volume_ratio", None),
    ("auction_price", float("nan")), ("prev_close", True),
])
def test_missing_field_diagnostic_matches_unchanged_gate(field, value):
    row = candidate(**{field: value})
    diag = diagnose_missing_evidence_fields(row, decision_at=AT)
    assert diag["status"] == auction_evidence_status(row, decision_at=AT) == "incomplete_values"
    assert diag["missing_positive_fields"] == {field: 1}
    assert getattr(row, field) is value


def test_samples_are_bounded_and_no_raw_payload_is_logged():
    rows = [candidate(f"60{i:04d}", auction_amount=0) for i in range(30)]
    diag = merge_diagnoses([diagnose_missing_evidence_fields(row, decision_at=AT) for row in rows])
    assert diag["missing_positive_fields"] == {"auction_amount": 30}
    assert len(diag["sample_codes_for_incomplete_values"]) == 5
    assert "10.5" not in str(diag)
    assert diagnose_missing_evidence_fields(None, decision_at=AT)["status"] == "unknown"
    bad = diagnose_missing_evidence_fields(candidate(code="raw-sensitive-input", auction_amount=0), decision_at=AT)
    assert bad["sample_codes_for_incomplete_values"] == []


def test_bad_clock_and_unverified_early_evidence_still_fail_closed():
    assert diagnose_missing_evidence_fields(
        candidate(received_at=AT-timedelta(seconds=3)), decision_at=AT)["status"] == "invalid_clock"
    assert auction_evidence_status(candidate(source="sina", source_quote_at=None,
        auction_volume=0, price_basis="spot_open_unverified", volume_basis="intraday_cumulative"),
        decision_at=AT) == "unknown"


@pytest.mark.asyncio
async def test_rejected_source_rows_report_exact_synthetic_fields_without_writing(maker):
    collector = AuctionCollector()
    rows = [candidate("600001", auction_amount=0), candidate("600002", volume_ratio=0),
            candidate("600003", received_at=AT-timedelta(seconds=3))]
    async with maker() as db:
        result = await collector._save_verified_auction_candidates(
            db, rows, AT, DAY, source_label="isolated")
        assert result["written"] == 0
        assert result["rejected"] == {"incomplete_values": 2, "invalid_clock": 1}
        assert result["diagnostic"]["missing_positive_fields"] == {"auction_amount": 1, "volume_ratio": 1}
        assert await db.scalar(select(func.count()).select_from(AuctionData)) == 0


@pytest.mark.asyncio
async def test_verified_batch_skips_irrelevant_historical_kline_scan(maker, monkeypatch):
    collector = AuctionCollector()
    history = AsyncMock(side_effect=AssertionError("valid source ratios need no history query"))
    monkeypatch.setattr(collector, "_load_average_daily_volume", history)
    rows = [candidate(f"60{i:04d}") for i in range(3000)]
    async with maker() as db:
        result = await collector._save_verified_auction_candidates(
            db, rows, AT, DAY, source_label="isolated")
        assert result["written"] == 3000
        assert result["distinct_source_quote_at"] == 1  # 3000 stocks are NOT 3000 frames per stock.
        assert await db.scalar(select(func.count()).select_from(AuctionData)) == 3000
        stored = await db.scalar(select(AuctionData).where(AuctionData.code == "600001"))
        assert stored.source_quote_at == rows[1].source_quote_at
        assert stored.volume_ratio == 3.5 and stored.auction_amount == 210000
    history.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_missing_ratios_query_history_and_keep_original_formula(maker, monkeypatch):
    collector = AuctionCollector()
    history = AsyncMock(return_value={"600002": 100000})
    monkeypatch.setattr(collector, "_load_average_daily_volume", history)
    frame = pd.DataFrame([dict(code=code, open=10.5, prev_close=10, volume=200,
        amount=210000, volume_ratio=ratio, volume_basis="auction_matched", volume_unit="lot100",
        amount_unit="CNY") for code, ratio in (("600001", 3.14159), ("600002", 0))])
    frame.attrs["auction_observed_at"] = AT
    async with maker() as db:
        assert await collector.save_auction_data(db, frame, DAY) == 2
        history.assert_awaited_once_with(db, ["600002"], DAY)
        rows = list((await db.scalars(select(AuctionData).order_by(AuctionData.code))).all())
        assert [row.volume_ratio for row in rows] == [3.142, 4.0]


@pytest.mark.asyncio
async def test_verified_path_never_derives_a_missing_ratio_from_daily_history(maker, monkeypatch):
    collector = AuctionCollector()
    history = AsyncMock()
    monkeypatch.setattr(collector, "_load_average_daily_volume", history)
    async with maker() as db:
        result = await collector._save_verified_auction_candidates(
            db, [candidate(volume_ratio=0)], AT, DAY, source_label="isolated")
    assert result["written"] == 0
    history.assert_not_awaited()
