"""Forward approval evidence cannot be manufactured by scoring old outcomes later."""
from datetime import date, datetime, timezone
import json

import pytest
from sqlalchemy import delete, select, update

from app.models.governance import TradeCalendarModel, DataQualityRun
from app.models.promotion import PromotionModelArtifact, PromotionPredictionRun, PromotionPredictionSnapshot, PromotionShadowPrediction
from app.models.stock import StockKline, LimitUpPool
from app.promotion import shadow
from app.promotion.ledger import append_prediction_run
from app.promotion.outcome_evidence import forward_shadow_clock_evidence
from app.promotion.versioning import PromotionModelIdentity, PromotionRuntimeMode
from test_promotion_shadow import shadow_env, _seed_shadow_scope


CLOCKS = dict(prediction_as_of=datetime(2026, 8, 27, 20),
    prediction_created=datetime(2026, 8, 27, 20, 0, 1),
    prediction_completed=datetime(2026, 8, 27, 20, 0, 2),
    artifact_created=datetime(2026, 8, 26, 21),
    shadow_created=datetime(2026, 8, 27, 20, 1),
    shadow_completed=datetime(2026, 8, 27, 20, 1, 1),
    outcome_date=date(2026, 8, 28), evaluation_at=datetime(2026, 8, 28, 16))


@pytest.mark.parametrize("changes,reason", [
    ({}, ""),
    ({"shadow_completed": datetime(2026, 8, 28, 9, 14, 59, 999999)}, ""),
    ({"shadow_completed": datetime(2026, 8, 28, 9, 15)}, "scored_after_outcome_window_opened"),
    ({"shadow_completed": datetime(2026, 8, 28, 15)}, "scored_after_outcome_window_opened"),
    ({"artifact_created": datetime(2026, 8, 28, 8)}, "artifact_registered_after_scoring"),
    ({"prediction_completed": None}, "missing_or_invalid_forward_clock"),
    ({"prediction_completed": datetime(2026, 8, 27, 20, 2)}, "forward_clock_order_invalid"),
    ({"shadow_completed": datetime(2026, 8, 29, 16)}, "forward_clock_order_invalid"),
    ({"shadow_created": datetime(2026, 8, 27, 20, 1, tzinfo=timezone.utc)}, "missing_or_invalid_forward_clock"),
])
def test_exact_forward_deadline_and_all_causal_clocks(changes, reason):
    result = forward_shadow_clock_evidence(**{**CLOCKS, **changes})
    assert result["passed"] is (not reason) and result["reason"] == reason
    assert result["deadline_exclusive"] == "2026-08-28T09:15:00"


async def evaluate(db, artifact_id):
    return await shadow.evaluate_shadow_artifact(db, artifact_id=artifact_id, target_board=1,
        snapshot_context="promotion_2000", persist=False, minimum_kline_rows=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("scored_at,eligible", [
    (datetime(2026, 8, 28, 9, 14, 59), True),
    (datetime(2026, 8, 28, 9, 15), False),
    (datetime(2026, 9, 8, 16), False),
])
async def test_replay_scores_are_retained_but_not_forward_samples(shadow_env, monkeypatch, scored_at, eligible):
    maker, root = shadow_env
    async with maker() as db:
        run_id, artifact_id = await _seed_shadow_scope(db, root)
        monkeypatch.setattr(shadow, "_shadow_now", lambda: scored_at)
        replay = await shadow.run_shadow_inference(db, prediction_run_id=run_id, artifact_id=artifact_id)
        assert replay["candidate_count"] == 4  # Reproduction is allowed, governance is separate.
        result = await evaluate(db, artifact_id)
    if eligible:
        assert result["sample_count"] == 4 and result["metadata"]["forward_timing_coverage"] == 1
    else:
        assert result["decision"] == "collecting" and "sample_count" not in result
        rejection = next(r for r in result["excluded_runs"] if r["reason"] == "forward_timing_unverified")
        assert rejection["timing"]["reason"] == "scored_after_outcome_window_opened"


@pytest.mark.asyncio
async def test_matching_shadow_cannot_validate_a_failed_official_source(shadow_env):
    maker, root = shadow_env
    async with maker() as db:
        run_id, artifact_id = await _seed_shadow_scope(db, root)
        await shadow.run_shadow_inference(db, prediction_run_id=run_id, artifact_id=artifact_id)
        await db.execute(update(PromotionPredictionRun).where(
            PromotionPredictionRun.id == run_id).values(status="failed"))
        await db.commit()
        result = await evaluate(db, artifact_id)
    assert result["decision"] == "collecting" and "sample_count" not in result
    assert result["excluded_runs"][0]["timing"]["reason"] == "forward_source_identity_or_quality_unproven"


@pytest.mark.asyncio
async def test_registry_artifact_must_exist_before_score_not_only_claim_old_fit_end(shadow_env):
    maker, root = shadow_env
    async with maker() as db:
        run_id, artifact_id = await _seed_shadow_scope(db, root)
        await db.execute(update(PromotionModelArtifact).where(PromotionModelArtifact.id == artifact_id)
                         .values(created_at=datetime(2026, 8, 28, 8)))
        await db.commit()
        await shadow.run_shadow_inference(db, prediction_run_id=run_id, artifact_id=artifact_id)
        result = await evaluate(db, artifact_id)
    assert result["decision"] == "collecting"
    assert result["excluded_runs"][0]["timing"]["reason"] == "artifact_registered_after_scoring"


@pytest.mark.asyncio
async def test_late_individual_score_row_invalidates_whole_candidate_batch(shadow_env):
    maker, root = shadow_env
    async with maker() as db:
        run_id, artifact_id = await _seed_shadow_scope(db, root)
        await shadow.run_shadow_inference(db, prediction_run_id=run_id, artifact_id=artifact_id)
        await db.execute(update(PromotionShadowPrediction).where(PromotionShadowPrediction.code == "000001")
                         .values(created_at=datetime(2026, 8, 28, 9, 15)))
        await db.commit()
        result = await evaluate(db, artifact_id)
    assert result["decision"] == "collecting"
    assert "paired_score_receipt_clock_invalid" in result["excluded_runs"][0]["details"]


@pytest.mark.asyncio
async def test_late_day_remains_in_coverage_denominator_next_to_valid_day(shadow_env, monkeypatch):
    maker, root = shadow_env
    async with maker() as db:
        run_id, artifact_id = await _seed_shadow_scope(db, root)
        await shadow.run_shadow_inference(db, prediction_run_id=run_id, artifact_id=artifact_id)
        snapshots = list((await db.scalars(select(PromotionPredictionSnapshot).where(
            PromotionPredictionSnapshot.run_id == run_id))).all())
        candidates = []
        for row in snapshots:
            frozen = json.loads(row.features_json)
            frozen.update(prediction_snapshot_recorded_at="2026-08-31T20:00:00",
                          prediction_snapshot_batch_key="late-forward-regression")
            candidates.append({"code": row.code, "name": row.name, "target_board": 1,
                "candidate_route": row.candidate_route, "probability": row.calibrated_probability,
                "probability_factors": frozen})
        second = await append_prediction_run(db, candidates, {1: date(2026, 8, 31)},
            identity=PromotionModelIdentity(runtime_mode=PromotionRuntimeMode.LEGACY,
                champion_model_version="shadow_champion_v1", challenger_model_version=None,
                feature_version="legacy_test_features", data_version="legacy_test_data"),
            snapshot_source="schedule", snapshot_context="promotion_2000", quality_gate={"gate_passed": True})
        await db.execute(update(PromotionPredictionRun).where(PromotionPredictionRun.id == second.run_id)
                         .values(created_at=datetime(2026, 8, 31, 20, 0, 1), completed_at=datetime(2026, 8, 31, 20, 0, 2)))
        for day in (date(2026, 8, 31), date(2026, 9, 1)):
            db.add(TradeCalendarModel(trade_date=day, is_trade_day=True))
            for row in snapshots:
                db.add(StockKline(code=row.code, trade_date=day, close=10.2, prev_close=10.2,
                                  volume=1000, source="tencent_close"))
        db.add(LimitUpPool(code="000000", trade_date=date(2026, 9, 1), source="test"))
        await db.commit()
        monkeypatch.setattr(shadow, "_shadow_now", lambda: datetime(2026, 9, 1, 10))
        await shadow.run_shadow_inference(db, prediction_run_id=second.run_id, artifact_id=artifact_id)
        result = await evaluate(db, artifact_id)
    assert result["sample_count"] == 4 and result["trade_day_count"] == 1
    assert result["metadata"]["forward_timing_coverage"] == .5
    check = next(c for c in result["acceptance"]["checks"] if c["name"] == "forward_timing_coverage")
    assert not check["passed"] and check["required"] == 1
    assert result["decision"] == "collecting"


@pytest.mark.asyncio
async def test_changed_artifact_bytes_cannot_borrow_prior_shadow_evidence(shadow_env):
    maker, root = shadow_env
    async with maker() as db:
        run_id, artifact_id = await _seed_shadow_scope(db, root)
        await shadow.run_shadow_inference(db, prediction_run_id=run_id, artifact_id=artifact_id)
        path = root / "shadow_t1_v1.json"
        data = json.loads(path.read_text())
        data["created_at"] = "2026-08-26T22:00:00"  # Same scores, different immutable bytes.
        path.write_text(json.dumps(data))
        with pytest.raises(ValueError, match="artifact bytes"):
            await evaluate(db, artifact_id)
        with pytest.raises(ValueError, match="register a new artifact"):
            await shadow.run_shadow_inference(db, prediction_run_id=run_id, artifact_id=artifact_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"challenger_probability": .123}, {"champion_probability": .99},
    {"features_hash": "0"*64}, {"market_regime": "fabricated_style"},
])
async def test_mutated_score_payload_or_regime_cannot_improve_governance(shadow_env, change):
    maker, root = shadow_env
    async with maker() as db:
        run_id, artifact_id = await _seed_shadow_scope(db, root)
        await shadow.run_shadow_inference(db, prediction_run_id=run_id, artifact_id=artifact_id)
        await db.execute(update(PromotionShadowPrediction).where(PromotionShadowPrediction.code == "000001")
                         .values(**change))
        await db.commit()
        result = await evaluate(db, artifact_id)
    assert result["decision"] == "collecting" and "sample_count" not in result
    assert "immutable_paired_payload_mismatch" in result["excluded_runs"][0]["details"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing", "presence_only", "partial_pool", "late_prediction_audit", "failed_retry"])
async def test_full_negative_label_universe_needs_recorded_truth_audit(shadow_env, mutation):
    maker, root = shadow_env
    async with maker() as db:
        run_id, artifact_id = await _seed_shadow_scope(db, root)
        await shadow.run_shadow_inference(db, prediction_run_id=run_id, artifact_id=artifact_id)
        day = date(2026, 8, 28)
        audit = await db.scalar(select(DataQualityRun).where(DataQualityRun.trade_date == day))
        if mutation == "missing":
            await db.execute(delete(DataQualityRun).where(DataQualityRun.id == audit.id))
        elif mutation in {"presence_only", "partial_pool"}:
            data = json.loads(audit.summary_json)
            pool = next(w for w in data["watermarks"] if w["dataset"] == "limit_up_pool")
            if mutation == "presence_only":
                pool["expected_count"] = None
            else:
                pool["expected_count"] = 2
                pool["completeness"] = .5
            audit.summary_json = json.dumps(data)
        elif mutation == "late_prediction_audit":
            await db.execute(update(DataQualityRun).where(DataQualityRun.trade_date == date(2026, 8, 27))
                             .values(started_at=datetime(2026, 8, 27, 21), completed_at=datetime(2026, 8, 27, 21)))
        else:
            db.add(DataQualityRun(trade_date=day, snapshot_context="promotion_2000", status="failed",
                gate_passed=False, started_at=datetime(2026, 8, 28, 17), completed_at=datetime(2026, 8, 28, 17),
                summary_json=audit.summary_json))
        await db.commit()
        result = await evaluate(db, artifact_id)
    assert result["decision"] == "collecting" and "sample_count" not in result
    assert result["excluded_runs"][0]["reason"] == "recorded_truth_audit_incomplete"
