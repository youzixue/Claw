"""Offline 036 consumer ties; source-clock evidence count remains unchanged."""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from app.models.stock import AuctionData
from app.models.paper import PaperShadowEvent
from app.signal.anomaly_scanner import AnomalyScanner
from app.paper import strategy_iteration_shadow as shadow
from auction_test_evidence import verified_auction_fields
from test_strategy_iteration_shadow import shadow_env, _seed_structures, _quote


@pytest.mark.asyncio
async def test_scanner_same_second_latest_bad_frame_is_not_old_good(shadow_env, monkeypatch):
    day = date(2026, 9, 1)
    fields = verified_auction_fields(day, "09:25:10")
    async with shadow_env() as db:
        db.add(AuctionData(id=2, code="600004", trade_date=day, auction_time="09:25:10",
            auction_price=10.5, prev_close=10, open_change=5, volume_ratio=3,
            auction_volume=1000, auction_amount=10500, **fields))
        await db.flush()
        fields = dict(fields, observed_at=fields["observed_at"] + timedelta(microseconds=1),
                      volume_basis="unknown")
        db.add(AuctionData(id=1, code="600004", trade_date=day, auction_time="09:25:10",
            auction_price=10.5, prev_close=10, open_change=5, volume_ratio=3,
            auction_volume=1000, auction_amount=10500, **fields))
        await db.commit()
        scanner = AnomalyScanner()
        monkeypatch.setattr(scanner, "_calc_technical_indicators", AsyncMock(return_value={}))
        result = await scanner._generate_next_day_plan(
            "600004", SimpleNamespace(level="A", risk_warnings=[]), db, target_date=day)
        assert result["auction_evidence_status"] != "ok"
        assert result["strategy"] == "normal"


@pytest.mark.asyncio
@pytest.mark.parametrize("same_clock", [True, False])
async def test_shadow_final_clock_tie_and_cancel_source_dedup(shadow_env, monkeypatch, same_clock):
    monkeypatch.setattr(shadow.settings, "PAPER_STRATEGY_ITERATION_MIN_QUOTE_COVERAGE", .2)
    day = date(2026, 9, 1)
    async with shadow_env() as db:
        await _seed_structures(db)
        rows = list((await db.scalars(select(AuctionData).where(AuctionData.code == "600004"))).all())
        cancel = [r for r in rows if "09:20" <= r.auction_time < "09:25"]
        assert len(cancel) >= 2
        for row in cancel:
            row.source_quote_at = datetime(2026, 9, 1, 9, 22)
            row.received_at = row.observed_at = datetime(2026, 9, 1, 9, 22)
            row.auction_time = "09:22:00"
        # Two sources carrying the same provider clock must still count as one sample.
        cancel[-1].source = "other_fixture"
        original = next(r for r in rows if r.auction_time == "09:25:00")
        fields = verified_auction_fields(day, "09:25:00")
        fields["received_at"] = fields["observed_at"] = datetime(2026, 9, 1, 9, 25, 1)
        if not same_clock:
            # Both remain in final phase; provider time outranks later receipt.
            original.source_quote_at += timedelta(microseconds=500000)
            original.received_at = original.observed_at = original.source_quote_at
        db.add(AuctionData(code="600004", trade_date=day, auction_time="09:25:01",
            auction_price=10.08, prev_close=10, auction_volume=1000,
            auction_amount=10080, **fields))
        await db.commit()
        await shadow.scan_strategy_iteration_shadow(db, [_quote("600004")], datetime(2026, 9, 1, 9, 40))
        events = list((await db.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.route_id == shadow.ROUTE_D,
            PaperShadowEvent.event_type == "structural_pool"))).all())
        prior = json.loads(events[0].snapshot_json)["prior_structure"]
        assert prior["final_change_pct"] == pytest.approx(.8 if same_clock else .5)
        assert prior["cancel_phase_positive_samples"] == 1
        assert prior["cancel_phase_verified"] is False
        assert prior["auction_volume_path_verified"] is False
