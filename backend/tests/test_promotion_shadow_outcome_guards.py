"""Outcome regressions use isolated real ledgers, not production/model training."""
from datetime import date
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, update

from app.models.stock import StockKline, LimitUpPool
from app.models.governance import TradeCalendarModel
from app.promotion import shadow
from test_promotion_shadow import shadow_env, _seed_shadow_scope


async def seed(session, root):
    run_id, artifact_id = await _seed_shadow_scope(session, root)
    await shadow.run_shadow_inference(session, prediction_run_id=run_id, artifact_id=artifact_id)
    return run_id, artifact_id


async def evaluate(session, artifact_id):
    return await shadow.evaluate_shadow_artifact(session, artifact_id=artifact_id, target_board=1,
        snapshot_context="promotion_2000", persist=False, minimum_kline_rows=1)


@pytest.mark.asyncio
async def test_missing_entire_real_t1_does_not_relabel_later_kline_day(shadow_env):
    maker, root = shadow_env
    async with maker() as db:
        _, artifact_id = await seed(db, root)
        await db.execute(delete(StockKline).where(StockKline.trade_date == date(2026, 8, 28)))
        for code in ("000000", "000001", "000002", "000003"):
            db.add(StockKline(code=code, trade_date=date(2026, 8, 31), close=10.2,
                              prev_close=10.2, volume=1000, source="tencent_close"))
        db.add(LimitUpPool(code="000001", trade_date=date(2026, 8, 31), source="test"))
        await db.commit()
        result = await evaluate(db, artifact_id)
    assert result["decision"] == "collecting"
    failure = next(r for r in result["excluded_runs"] if r["reason"] == "authoritative_outcome_quality_incomplete")
    assert failure["outcome_trade_date"] == date(2026, 8, 28) and failure["kline_count"] == 0


@pytest.mark.asyncio
async def test_existing_kline_cannot_replace_missing_calendar(shadow_env):
    maker, root = shadow_env
    async with maker() as db:
        _, artifact_id = await seed(db, root)
        await db.execute(delete(TradeCalendarModel).where(TradeCalendarModel.trade_date == date(2026, 8, 28)))
        await db.commit()
        result = await evaluate(db, artifact_id)
    assert result["decision"] == "collecting"
    assert result["excluded_runs"][0]["reason"] == "outcome_calendar_gap"


@pytest.mark.asyncio
@pytest.mark.parametrize("change,expected", [
    ({"source": "spot_fallback"}, "candidate_outcome_source_unverified"),
    ({"volume": 0}, "candidate_outcome_suspended_or_invalid"),
    ({"prev_close": 5}, "candidate_outcome_price_chain_discontinuity"),
])
async def test_single_unknown_candidate_blocks_whole_paired_day(shadow_env, change, expected):
    maker, root = shadow_env
    async with maker() as db:
        _, artifact_id = await seed(db, root)
        await db.execute(update(StockKline).where(StockKline.code == "000001",
            StockKline.trade_date == date(2026, 8, 28)).values(**change))
        await db.commit()
        result = await evaluate(db, artifact_id)
    assert result["decision"] == "collecting" and "sample_count" not in result
    failure = result["excluded_runs"][0]
    if expected == "candidate_outcome_source_unverified":
        assert failure["reason"] == "authoritative_outcome_quality_incomplete"
        assert failure["kline_count"] == 3 and failure["required_kline_count"] == 4
    else:
        assert expected in failure["details"]


@pytest.mark.asyncio
async def test_quarantined_event_is_unknown_not_negative(shadow_env):
    maker, root = shadow_env
    async with maker() as db:
        _, artifact_id = await seed(db, root)
        db.add(LimitUpPool(code="000001", trade_date=date(2026, 8, 28), source="test", quarantined=True))
        await db.commit()
        result = await evaluate(db, artifact_id)
    assert result["decision"] == "collecting"
    assert "candidate_limit_event_quarantined" in result["excluded_runs"][0]["details"]


@pytest.mark.asyncio
async def test_nonpersisting_paths_never_ensure_storage_and_material_revision_changes_key(shadow_env, monkeypatch):
    maker, root = shadow_env
    async with maker() as db:
        run_id, artifact_id = await seed(db, root)
        ensure = AsyncMock(side_effect=AssertionError("read-only must not ensure schema"))
        monkeypatch.setattr(shadow, "ensure_prediction_ledger_storage", ensure)
        first = await evaluate(db, artifact_id)
        await shadow.run_shadow_inference(db, prediction_run_id=run_id, artifact_id=artifact_id, persist=False)
        assert not ensure.called
        await db.execute(update(StockKline).where(StockKline.code == "000001",
            StockKline.trade_date == date(2026, 8, 28)).values(volume=1100))
        await db.commit()
        second = await evaluate(db, artifact_id)
    assert first["evaluation_key"] != second["evaluation_key"]
    assert first["positive_count"] == second["positive_count"] == 1
    assert first["metadata"]["historical_outcome_availability_verified"] is False
    assert first["metadata"]["evaluation_version"] == "promotion_shadow_evaluation_v9_sealed_outcomes"
