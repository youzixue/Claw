"""M2 gate integration with isolated ledgers; source parsing is tested separately."""
from datetime import date, datetime
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from app.models.governance import DataQualityRun, TradeCalendarModel
from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot, PromotionShadowEvaluation
from app.models.stock import StockKline, LimitUpPool, StockTag
from app.promotion import shadow, identity_evidence
from app.promotion.ledger import append_prediction_run
from app.promotion.versioning import PromotionModelIdentity, PromotionRuntimeMode
from test_promotion_shadow import shadow_env, _seed_shadow_scope, identity_ready_fixture

PREDICTION_AT = datetime(2026, 8, 27, 20)
EVALUATION_AT = datetime(2026, 9, 8, 17)


async def seed(db, root):
    run_id, artifact_id = await _seed_shadow_scope(db, root)
    await shadow.run_shadow_inference(db, prediction_run_id=run_id, artifact_id=artifact_id)
    return run_id, artifact_id


async def evaluate(db, artifact_id, **kwargs):
    return await shadow.evaluate_shadow_artifact(db, artifact_id=artifact_id, target_board=1,
        snapshot_context="promotion_2000", persist=False, minimum_kline_rows=1,
        now=EVALUATION_AT, **kwargs)


@pytest.mark.asyncio
async def test_default_real_identity_gate_does_not_use_current_tags_or_missing_as_normal(shadow_env, monkeypatch):
    maker, root = shadow_env
    monkeypatch.setattr(shadow, "identity_pair_gate", identity_evidence.identity_pair_gate)
    async with maker() as db:
        _, artifact_id = await seed(db, root)
        db.add_all([StockTag(code=f"00000{i}", board_type="main_sz", board_tag="tradeable", is_st=False, is_delisting=False,
                             is_suspended=False) for i in range(4)])
        await db.commit()
        ensure = AsyncMock(side_effect=AssertionError("read-only path cannot ensure or migrate"))
        monkeypatch.setattr(shadow, "ensure_prediction_ledger_storage", ensure)
        result = await evaluate(db, artifact_id)
        assert await db.scalar(select(func.count()).select_from(PromotionShadowEvaluation)) == 0
    assert result["decision"] == "collecting" and "sample_count" not in result
    assert result["identity_coverage"] == 0
    failure = result["excluded_runs"][0]
    assert "candidate_identity_unverified" in failure["details"]
    assert failure["identity_evidence"]["passed"] is False
    assert len(failure["identity_evidence"]["per_code"]) == 4
    assert not ensure.called and not (root / "identity").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unverified", "wrong_profile", "missing_hash", "true_string", "partial_pass", "wrong_code_pass", "extra_unknown", "verified_with_reason", "error"])
async def test_bad_identity_cannot_drop_one_candidate_to_keep_favorable_rest(shadow_env, monkeypatch, fault):
    maker, root = shadow_env
    def gate(index, **kwargs):
        if fault == "error":
            raise OSError("fixture unavailable")
        result = identity_ready_fixture(index, **kwargs)
        if fault == "unverified":
            result.update(passed=False, reasons=["identity_prediction_outside_profile"])
            result["per_code"][1].update(status="unknown", reasons=["identity_prediction_outside_profile"])
        elif fault == "wrong_profile":
            result["profile"] = "current_tags_not_historical"
        elif fault == "missing_hash":
            result["evidence_hash"] = None
        elif fault == "true_string":
            result["passed"] = "true"
        elif fault == "partial_pass":
            result["per_code"].pop()
        elif fault == "wrong_code_pass":
            result["per_code"][1]["code"] = "600999"
        elif fault == "extra_unknown":
            result["per_code"].append({"code": "600999", "status": "unknown", "reasons": ["gap"]})
        elif fault == "verified_with_reason":
            result["per_code"][1]["reasons"] = ["identity_interval_gap"]
        return result
    monkeypatch.setattr(shadow, "identity_pair_gate", gate)
    async with maker() as db:
        _, artifact_id = await seed(db, root)
        result = await evaluate(db, artifact_id)
    assert result["decision"] == "collecting" and "sample_count" not in result
    assert "candidate_identity_unverified" in result["excluded_runs"][0]["details"]
    assert result["identity_coverage"] == 0


@pytest.mark.asyncio
async def test_prepare_failure_is_closed_even_if_gate_dependency_would_pass(shadow_env, monkeypatch):
    maker, root = shadow_env
    monkeypatch.setattr(shadow, "prepare_identity_index", AsyncMock(side_effect=OSError("fixture lock failure")))
    permissive = AsyncMock()  # Must not reach any downstream decision after failed lock.
    monkeypatch.setattr(shadow, "identity_pair_gate", permissive)
    async with maker() as db:
        _, artifact_id = await seed(db, root)
        result = await evaluate(db, artifact_id)
    assert result["decision"] == "collecting" and not permissive.called
    assert result["excluded_runs"][0]["identity_evidence"]["reasons"] == ["identity_gate_unavailable"]


@pytest.mark.asyncio
async def test_prediction_time_is_official_source_clock_not_later_scoring_clock(shadow_env, monkeypatch):
    maker, root = shadow_env
    calls, prepared = [], []
    original_prepare = shadow.prepare_identity_index
    async def prepare(**kwargs):
        prepared.append(kwargs)
        return await original_prepare(**kwargs)
    def gate(index, **kwargs):
        calls.append(kwargs)
        return identity_ready_fixture(index, **kwargs)
    monkeypatch.setattr(shadow, "prepare_identity_index", prepare)
    monkeypatch.setattr(shadow, "identity_pair_gate", gate)
    async with maker() as db:
        _, artifact_id = await seed(db, root)
        result = await evaluate(db, artifact_id)
    assert prepared == [{"known_cutoff": EVALUATION_AT}]
    assert len(calls) == 1 and calls[0]["prediction_at"] == PREDICTION_AT
    assert calls[0]["prediction_at"] != datetime(2026, 8, 27, 20, 10)
    assert calls[0]["outcome_day"] == date(2026, 8, 28)
    assert calls[0]["evaluation_as_of"] == EVALUATION_AT
    assert set(calls[0]["codes"]) == {f"00000{i}" for i in range(4)}
    assert result["sample_count"] == 4 and result["metadata"]["identity_coverage"] == 1
    assert result["metadata"]["evaluation_version"] == "promotion_shadow_evaluation_v9_sealed_outcomes"


@pytest.mark.asyncio
async def test_receipt_revision_changes_key_but_query_clock_alone_does_not(shadow_env, monkeypatch):
    maker, root = shadow_env
    revision = ["a" * 64]
    def gate(index, **kwargs):
        result = identity_ready_fixture(index, **kwargs)
        result["evidence_hash"] = revision[0]
        # Pure diagnostic clock must not generate another identical evaluation.
        result["query_clock"] = kwargs["evaluation_as_of"].isoformat()
        return result
    monkeypatch.setattr(shadow, "identity_pair_gate", gate)
    async with maker() as db:
        _, artifact_id = await seed(db, root)
        first = await evaluate(db, artifact_id)
        again = await shadow.evaluate_shadow_artifact(db, artifact_id=artifact_id, target_board=1,
            snapshot_context="promotion_2000", persist=False, minimum_kline_rows=1,
            now=datetime(2026, 9, 8, 18))
        assert first["evaluation_key"] == again["evaluation_key"]
        revision[0] = "b" * 64
        changed = await evaluate(db, artifact_id)
    assert first["evaluation_key"] != changed["evaluation_key"]
    assert first["metrics"] == changed["metrics"]  # provenance revision, not invented outcome change


@pytest.mark.asyncio
async def test_persisted_evaluation_remains_idempotent_with_identical_visible_identity(shadow_env):
    maker, root = shadow_env
    async with maker() as db:
        _, artifact_id = await seed(db, root)
        first = await shadow.evaluate_shadow_artifact(db, artifact_id=artifact_id, target_board=1,
            snapshot_context="promotion_2000", persist=True, minimum_kline_rows=1,
            now=datetime(2026, 9, 8, 17))
        second = await shadow.evaluate_shadow_artifact(db, artifact_id=artifact_id, target_board=1,
            snapshot_context="promotion_2000", persist=True, minimum_kline_rows=1,
            now=datetime(2026, 9, 8, 18))
        assert await db.scalar(select(func.count()).select_from(PromotionShadowEvaluation)) == 1
    assert first["created"] is True and second["created"] is False
    assert first["id"] == second["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown_code", [None, "000001"])
async def test_real_pair_gate_consumes_prepared_index_without_candidate_filtering(shadow_env, monkeypatch, unknown_code):
    # Explicit synthetic prepared identity view, not real vendor/receipt evidence.
    from test_promotion_identity_evidence import fixture_index
    maker, root = shadow_env
    async def prepare(*, known_cutoff):
        return fixture_index(known_cutoff=known_cutoff,
            codes=tuple(f"00000{i}" for i in range(4)), unknown_code=unknown_code)
    monkeypatch.setattr(shadow, "prepare_identity_index", prepare)
    monkeypatch.setattr(shadow, "identity_pair_gate", identity_evidence.identity_pair_gate)
    async with maker() as db:
        _, artifact_id = await seed(db, root)
        result = await evaluate(db, artifact_id)
        again = await shadow.evaluate_shadow_artifact(db, artifact_id=artifact_id, target_board=1,
            snapshot_context="promotion_2000", persist=False, minimum_kline_rows=1,
            now=datetime(2026, 9, 8, 18))
    if unknown_code is None:
        assert result["sample_count"] == 4 and result["metadata"]["identity_coverage"] == 1
        assert result["evaluation_key"] == again["evaluation_key"]
    else:
        assert result["decision"] == "collecting" and "sample_count" not in result
        assert result["identity_coverage"] == 0
        gate = result["excluded_runs"][0]["identity_evidence"]
        assert len(gate["per_code"]) == 4 and not gate["passed"]
        assert sum(row["status"] == "verified" for row in gate["per_code"]) == 3
        assert gate["evidence_hash"] == again["excluded_runs"][0]["identity_evidence"]["evidence_hash"]


async def add_second_day(db, run_id, artifact_id, monkeypatch):
    snapshots = list((await db.scalars(select(PromotionPredictionSnapshot).where(
        PromotionPredictionSnapshot.run_id == run_id))).all())
    candidates = []
    for row in snapshots:
        factors = json.loads(row.features_json)
        factors.update(prediction_snapshot_recorded_at="2026-08-31T20:00:00",
                       prediction_snapshot_batch_key="m2-full-paired-second-day")
        candidates.append({"code": row.code, "name": row.name, "target_board": 1,
            "candidate_route": row.candidate_route, "probability": row.calibrated_probability,
            "probability_factors": factors})
    appended = await append_prediction_run(db, candidates, {1: date(2026, 8, 31)},
        identity=PromotionModelIdentity(runtime_mode=PromotionRuntimeMode.LEGACY,
            champion_model_version="shadow_champion_v1", challenger_model_version=None,
            feature_version="legacy_test_features", data_version="legacy_test_data"),
        snapshot_source="schedule", snapshot_context="promotion_2000", quality_gate={"gate_passed": True})
    await db.execute(update(PromotionPredictionRun).where(PromotionPredictionRun.id == appended.run_id)
        .values(created_at=datetime(2026, 8, 31, 20, 0, 1), completed_at=datetime(2026, 8, 31, 20, 0, 2)))
    for day in (date(2026, 8, 31), date(2026, 9, 1)):
        db.add(TradeCalendarModel(trade_date=day, is_trade_day=True))
        for row in snapshots:
            db.add(StockKline(code=row.code, trade_date=day, close=10.2, prev_close=10.2,
                              volume=1000, source="tencent_close"))
        db.add(LimitUpPool(code="000000", trade_date=day, source="fixture"))
        at = datetime.combine(day, datetime.min.time()).replace(hour=16)
        marks = [{"dataset": name, "trade_date": day.isoformat(), "status": "ok",
            "record_count": count, "expected_count": count, "completeness": 1.0,
            "details": {"coverage_scope": "all_rows"}} for name, count in (("stock_kline", 4), ("limit_up_pool", 1))]
        db.add(DataQualityRun(trade_date=day, snapshot_context="promotion_2000", status="ok", gate_passed=True,
            started_at=at, completed_at=at, summary_json=json.dumps({"watermarks": marks})))
    await db.commit()
    monkeypatch.setattr(shadow, "_shadow_now", lambda: datetime(2026, 8, 31, 20, 10))
    return await shadow.run_shadow_inference(db, prediction_run_id=appended.run_id, artifact_id=artifact_id)


@pytest.mark.asyncio
async def test_unknown_day_stays_in_identity_denominator_and_hash_next_to_verified_day(shadow_env, monkeypatch):
    maker, root = shadow_env
    rejected_revision = ["c" * 64]
    def gate(index, **kwargs):
        result = identity_ready_fixture(index, **kwargs)
        if kwargs["prediction_at"].date() == date(2026, 8, 31):
            result.update(passed=False, reasons=["identity_interval_gap"], evidence_hash=rejected_revision[0])
            result["per_code"][1].update(status="unknown", reasons=["identity_interval_gap"])
        return result
    monkeypatch.setattr(shadow, "identity_pair_gate", gate)
    async with maker() as db:
        run_id, artifact_id = await seed(db, root)
        other = await add_second_day(db, run_id, artifact_id, monkeypatch)
        result = await evaluate(db, artifact_id)
        rejected_revision[0] = "d" * 64
        changed = await evaluate(db, artifact_id)
    assert result["sample_count"] == 4 and result["trade_day_count"] == 1
    assert result["metadata"]["identity_coverage"] == .5
    assert result["metadata"]["identity_unverified_run_ids"] == [other["id"]]
    check = next(c for c in result["acceptance"]["checks"] if c["name"] == "identity_coverage")
    assert check["actual"] == .5 and check["required"] == 1 and not check["passed"]
    assert result["decision"] == "collecting"
    assert result["evaluation_key"] != changed["evaluation_key"]
    assert result["metrics"] == changed["metrics"]
