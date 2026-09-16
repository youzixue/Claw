import json
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import model_lab
from app.config.settings import settings
from app.db.session import Base, get_db
from app.models.promotion import (
    PromotionDeploymentEvent,
    PromotionModelArtifact,
    PromotionPredictionRun,
    PromotionPredictionSnapshot,
    PromotionShadowEvaluation,
    PromotionShadowPrediction,
    PromotionShadowRun,
)
from app.models.stock import LimitUpPool, StockKline
from app.models.governance import TradeCalendarModel, DataQualityRun
from app.promotion.deployment import (
    apply_active_promotion_overlay,
    approve_challenger,
    current_deployment,
    rollback_challenger,
)
from app.promotion.ledger import append_prediction_run
from app.promotion.modeling.acceptance import (
    DEFAULT_ACCEPTANCE_POLICY,
    SHADOW_ACCEPTANCE_POLICY,
)
from app.promotion.modeling.features import FEATURE_VERSION
from app.promotion.modeling.inference import load_promotion_artifact
from app.promotion.shadow import (
    _daily_probability_rank_metrics,
    _is_completed_outcome_date,
    _official_champion_rank_metrics,
    evaluate_shadow_artifact,
    run_shadow_inference,
)
from app.promotion.versioning import PromotionModelIdentity, PromotionRuntimeMode


def identity_ready_fixture(index, *, codes, prediction_at, outcome_day, evaluation_as_of):
    # Isolated gate dependency for unrelated rank/deployment tests, NOT an archive
    # or vendor proof. Identity-specific tests restore the real gate independently.
    from app.promotion.shadow import IDENTITY_PROFILE, _digest
    result = {"passed": True, "profile": IDENTITY_PROFILE, "reasons": [],
              "per_code": [{"code": code, "status": "verified", "reasons": [],
                            "prediction_refs": ["fixture:known-before-prediction"],
                            "outcome_refs": ["fixture:whole-session-identity"]} for code in sorted(codes)]}
    result["evidence_hash"] = _digest({"fixture": result, "prediction_at": prediction_at,
                                      "outcome_day": outcome_day})
    return result


@pytest_asyncio.fixture
async def shadow_env(tmp_path: Path, monkeypatch):
    # Isolated historical fixture: score BEFORE the fixture outcome opens, not at
    # wall-clock test execution time. Production has no backdated-scoring input.
    monkeypatch.setattr("app.promotion.shadow._shadow_now", lambda: datetime(2026, 8, 27, 20, 10))
    from app.promotion.identity_evidence import prepare_identity_index
    async def isolated_identity_index(*, known_cutoff):
        return await prepare_identity_index(known_cutoff=known_cutoff, archive_root=tmp_path / "identity")
    monkeypatch.setattr("app.promotion.shadow.prepare_identity_index", isolated_identity_index)
    monkeypatch.setattr("app.promotion.shadow.identity_pair_gate", identity_ready_fixture)
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'shadow.db'}", future=True
    )
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    # Explicit fake material gate for unrelated mechanics tests; not raw proof.
    from test_promotion_paired_material import load_material_fixture, outcome_ready_fixture
    async def isolated_outcome_index(*, known_cutoff):
        async with SessionLocal() as fixture_db:
            return await load_material_fixture(fixture_db)
    monkeypatch.setattr("app.promotion.shadow.prepare_outcome_index", isolated_outcome_index)
    monkeypatch.setattr("app.promotion.shadow.outcome_pair_gate", outcome_ready_fixture)
    yield SessionLocal, tmp_path
    await engine.dispose()


def _write_artifact(tmp_path: Path, *, model_version: str = "shadow_t1_v1") -> Path:
    payload = {
        "schema_version": "promotion_model_artifact_v1",
        "model_version": model_version,
        "created_at": "2026-08-26T21:00:00",
        "target_board": 1,
        "feature_version": FEATURE_VERSION,
        "data_version": "shadow_test_data_v1",
        "model": {
            "vectorizer": {
                "feature_version": FEATURE_VERSION,
                "numeric_names": ["route_score"],
                "route_categories": [],
                "regime_categories": [],
                "mean": [0.0],
                "scale": [1.0],
                "feature_names": ["route_score"],
            },
            "classifier": {
                "algorithm": "numpy_logistic_regression",
                "coefficients": [1.2],
                "intercept": -1.0,
                "l2": 0.02,
                "learning_rate": 0.03,
                "positive_weight": 1.0,
                "iterations": 10,
                "final_loss": 0.5,
            },
            "calibrator": {
                "method": "platt_out_of_time_tail",
                "slope": 1.0,
                "intercept": -0.1,
            },
            "fit_start_date": "2026-08-01",
            "fit_end_date": "2026-08-20",
            "calibration_start_date": "2026-08-21",
            "calibration_end_date": "2026-08-26",
        },
        "walk_forward": {},
        "acceptance": {"passed": True, "decision": "shadow_eligible"},
        "training_config": {
            "dataset_source": "prediction_snapshots",
            "snapshot_context": "promotion_2000",
        },
        "dataset_diagnostics": {},
    }
    path = tmp_path / f"{model_version}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


async def _seed_shadow_scope(session: AsyncSession, tmp_path: Path) -> tuple[int, int]:
    prediction_day = date(2026, 8, 27)
    outcome_day = date(2026, 8, 28)
    for trade_day in (prediction_day, outcome_day):
        session.add(TradeCalendarModel(trade_date=trade_day, is_trade_day=True))
        for index in range(4):
            session.add(
                StockKline(
                    code=f"00000{index}",
                    trade_date=trade_day,
                    open=10,
                    high=11,
                    low=9,
                    close=10.2,
                    prev_close=10.2,
                    source="tencent_close",
                    change_pct=2,
                    volume=1000,
                    amount=1_000_000,
                    turnover=2,
                )
            )
    # Explicit all-row truth audit fixtures; production never synthesizes these
    # denominators from the rows it happens to have.
    session.add(LimitUpPool(code="000099", trade_date=prediction_day, source="test"))
    for day in (prediction_day, outcome_day):
        marks = [{"dataset": dataset, "trade_date": day.isoformat(), "status": "ok",
                  "record_count": count, "expected_count": count, "completeness": 1.0,
                  "details": {"coverage_scope": "all_rows"}}
                 for dataset, count in (("stock_kline", 4), ("limit_up_pool", 1))]
        at = datetime.combine(day, datetime.min.time()).replace(hour=16)
        session.add(DataQualityRun(trade_date=day, snapshot_context="promotion_2000",
            status="ok", gate_passed=True, started_at=at, completed_at=at,
            summary_json=json.dumps({"watermarks": marks})))
    session.add(
        LimitUpPool(
            code="000000",
            name="真实涨停",
            trade_date=outcome_day,
            consecutive_days=1,
            source="test",
        )
    )
    await session.commit()

    # A formal ledger fixture must use an actual registered route. This does
    # not relax shadow identity, PIT, outcome coverage or manual-promotion gates.
    candidates = []
    for index in range(4):
        candidates.append(
            {
                "code": f"00000{index}",
                "name": f"候选{index}",
                "target_board": 1,
                "candidate_route": "mainline_spread_start",
                "probability": 0.1 + index * 0.03,
                "probability_factors": {
                    "route_score": float(3 - index),
                    "prediction_snapshot_source": "schedule",
                    "prediction_snapshot_context": "promotion_2000",
                    "prediction_snapshot_recorded_at": "2026-08-27T20:00:00",
                    "prediction_snapshot_batch_key": "shadow-test-20260827",
                    "prediction_record_scope": "ranked",
                    "prediction_rank_contract_version": "promotion_rank_contract_v1",
                    "prediction_rank_contract_complete": True,
                    "prediction_rank_eligible": True,
                    "prediction_rank_eligible_count": 4,
                    "prediction_ranked_selected": True,
                    "prediction_ranked_position": index + 1,
                    "prediction_ranked_limit": 12,
                    "prediction_recall_ranked_position": index + 1,
                    "prediction_recall_ranked_limit": 30,
                    "prediction_pool_rank": index + 1,
                    "prediction_actionable": index == 0,
                },
            }
        )
    identity = PromotionModelIdentity(
        runtime_mode=PromotionRuntimeMode.LEGACY,
        champion_model_version="shadow_champion_v1",
        challenger_model_version=None,
        feature_version="legacy_test_features",
        data_version="legacy_test_data",
    )
    append_result = await append_prediction_run(
        session,
        candidates,
        {1: prediction_day},
        identity=identity,
        snapshot_source="schedule",
        snapshot_context="promotion_2000",
        quality_gate={"gate_passed": True},
    )
    # The ledger was frozen at the fixture prediction time, not rebuilt after its
    # known outcome; only this isolated fixture receives historical source clocks.
    await session.execute(update(PromotionPredictionRun).where(
        PromotionPredictionRun.id == append_result.run_id).values(
        created_at=datetime(2026, 8, 27, 20, 0, 1), completed_at=datetime(2026, 8, 27, 20, 0, 2)))
    await session.execute(update(PromotionPredictionSnapshot).where(
        PromotionPredictionSnapshot.run_id == append_result.run_id).values(
        created_at=datetime(2026, 8, 27, 20, 0, 1)))
    artifact_path = _write_artifact(tmp_path)
    artifact = PromotionModelArtifact(
        model_version="shadow_t1_v1",
        feature_version=FEATURE_VERSION,
        data_version="shadow_test_data_v1",
        algorithm="numpy_logistic_platt",
        status="shadow_eligible",
        artifact_uri=str(artifact_path),
        params_json="{}",
        metrics_json="{}",
        created_at=datetime(2026, 8, 26, 21, 0),
    )
    session.add(artifact)
    await session.commit()
    assert append_result is not None
    return int(append_result.run_id), int(artifact.id)


def test_artifact_loader_rejects_registry_mismatch(tmp_path: Path):
    path = _write_artifact(tmp_path)
    loaded = load_promotion_artifact(
        str(path), expected_model_version="shadow_t1_v1", expected_target_board=1
    )
    assert loaded.feature_count == 1
    with pytest.raises(ValueError, match="model_version"):
        load_promotion_artifact(str(path), expected_model_version="wrong")


def test_shadow_outcome_clock_never_settles_intraday():
    outcome_day = date(2026, 8, 31)
    assert not _is_completed_outcome_date(
        outcome_day, now=datetime(2026, 8, 31, 9, 45)
    )
    assert _is_completed_outcome_date(
        outcome_day, now=datetime(2026, 8, 31, 15, 10)
    )
    assert _is_completed_outcome_date(
        date(2026, 8, 28), now=datetime(2026, 8, 31, 9, 0)
    )


def test_shadow_rank_metrics_exclude_production_ineligible_candidates():
    contract = {
        "version": "promotion_rank_contract_v1",
        "complete": True,
        "eligible_count": 2,
        "formal_limit": 12,
        "recall_limit": 30,
    }
    observations = [
        {
            "prediction_trade_date": "2026-08-27",
            "prediction_id": 1,
            "code": "000001",
            "label": 0,
            "challenger_probability": 0.8,
            "champion_rank_position": 1,
            "champion_recall_rank_position": 1,
            "rank_eligible": True,
            "rank_contract": {**contract, "eligible": True},
        },
        {
            "prediction_trade_date": "2026-08-27",
            "prediction_id": 2,
            "code": "000002",
            "label": 0,
            "challenger_probability": 0.7,
            "champion_rank_position": 2,
            "champion_recall_rank_position": 2,
            "rank_eligible": True,
            "rank_contract": {**contract, "eligible": True},
        },
        {
            "prediction_trade_date": "2026-08-27",
            "prediction_id": 3,
            "code": "000003",
            "label": 1,
            "challenger_probability": 0.99,
            "champion_rank_position": None,
            "champion_recall_rank_position": None,
            "rank_eligible": False,
            "rank_contract": {**contract, "eligible": False},
        },
    ]

    challenger_rank = _daily_probability_rank_metrics(
        observations, probability_key="challenger_probability"
    )
    official_rank = _official_champion_rank_metrics(observations)

    assert challenger_rank["5"]["selected_count"] == 2
    assert challenger_rank["5"]["hit_count"] == 0
    assert challenger_rank["5"]["positive_count"] == 1
    assert official_rank["metadata_coverage"] == 1.0
    observations[0]["rank_contract"] = {}
    assert _official_champion_rank_metrics(observations)["metadata_coverage"] == 0.0


@pytest.mark.asyncio
async def test_shadow_rejects_registry_status_without_artifact_offline_evidence(
    shadow_env,
):
    SessionLocal, tmp_path = shadow_env
    async with SessionLocal() as session:
        prediction_run_id, artifact_id = await _seed_shadow_scope(session, tmp_path)
        artifact_path = tmp_path / "shadow_t1_v1.json"
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        payload["acceptance"] = {"passed": False, "decision": "rejected"}
        artifact_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError, match="offline acceptance"):
            await run_shadow_inference(
                session,
                prediction_run_id=prediction_run_id,
                artifact_id=artifact_id,
                persist=True,
            )


@pytest.mark.asyncio
async def test_shadow_rejects_run_without_immutable_quality_gate(shadow_env):
    SessionLocal, tmp_path = shadow_env
    async with SessionLocal() as session:
        prediction_run_id, artifact_id = await _seed_shadow_scope(session, tmp_path)
        await session.execute(
            update(PromotionPredictionRun)
            .where(PromotionPredictionRun.id == prediction_run_id)
            .values(gate_passed=None)
        )
        await session.commit()

        with pytest.raises(ValueError, match="passed immutable data-quality gate"):
            await run_shadow_inference(
                session,
                prediction_run_id=prediction_run_id,
                artifact_id=artifact_id,
                persist=True,
            )


@pytest.mark.asyncio
async def test_shadow_run_is_same_pool_append_only_and_idempotent(shadow_env):
    SessionLocal, tmp_path = shadow_env
    async with SessionLocal() as session:
        prediction_run_id, artifact_id = await _seed_shadow_scope(session, tmp_path)
        first = await run_shadow_inference(
            session,
            prediction_run_id=prediction_run_id,
            artifact_id=artifact_id,
            persist=True,
        )
        second = await run_shadow_inference(
            session,
            prediction_run_id=prediction_run_id,
            artifact_id=artifact_id,
            persist=True,
        )
        run_count = await session.scalar(select(func.count()).select_from(PromotionShadowRun))
        prediction_count = await session.scalar(
            select(func.count()).select_from(PromotionShadowPrediction)
        )

        evaluation = await evaluate_shadow_artifact(
            session,
            artifact_id=artifact_id,
            target_board=1,
            snapshot_context="promotion_2000",
            persist=True,
            minimum_kline_rows=1,
        )
        evaluation_count = await session.scalar(
            select(func.count()).select_from(PromotionShadowEvaluation)
        )

    assert first["created"] is True
    assert second["created"] is False
    assert first["payload_hash"] == second["payload_hash"]
    assert first["candidate_count"] == 4
    assert run_count == 1
    assert prediction_count == 4
    assert sorted(item["challenger_rank_position"] for item in first["predictions"]) == [1, 2, 3, 4]
    assert evaluation["positive_count"] == 1
    assert evaluation["decision"] == "collecting"
    bootstrap = evaluation["metrics"]["paired_bootstrap"]
    assert bootstrap["method"] == "paired_trade_day_bootstrap"
    assert bootstrap["trade_day_count"] == 1
    assert bootstrap["iterations"] == 500
    assert any(
        check["name"] == "paired_bootstrap_days" and not check["passed"]
        for check in evaluation["acceptance"]["checks"]
    )
    assert evaluation["acceptance"]["policy"]["minimum_outcome_kline_rows"] == 1
    assert evaluation["acceptance"]["policy"]["minimum_outcome_kline_completeness"] == 0.95
    assert evaluation_count == 1
    assert evaluation["production_unchanged"] is True


@pytest.mark.asyncio
async def test_shadow_evaluation_rejects_partial_kline_outcome_universe(shadow_env):
    SessionLocal, tmp_path = shadow_env
    async with SessionLocal() as session:
        prediction_run_id, artifact_id = await _seed_shadow_scope(session, tmp_path)
        await run_shadow_inference(
            session,
            prediction_run_id=prediction_run_id,
            artifact_id=artifact_id,
            persist=True,
        )
        await session.execute(
            delete(StockKline).where(
                StockKline.trade_date == date(2026, 8, 28),
                StockKline.code != "000000",
            )
        )
        await session.commit()

        evaluation = await evaluate_shadow_artifact(
            session,
            artifact_id=artifact_id,
            target_board=1,
            snapshot_context="promotion_2000",
            persist=False,
            minimum_kline_rows=1,
        )

    assert evaluation["decision"] == "collecting"
    assert evaluation["reason"] == "no quality-complete next-session outcomes"
    incomplete = next(
        item
        for item in evaluation["excluded_runs"]
        if item["reason"] == "authoritative_outcome_quality_incomplete"
    )
    assert incomplete["kline_count"] == 1
    assert incomplete["recent_kline_peak"] == 4
    assert incomplete["required_kline_count"] == 4
    assert incomplete["kline_completeness"] == 0.25


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_kind", ["completed", "failed", "running", "empty", "other_target"])
async def test_shadow_evaluation_cannot_cherry_pick_superseded_official_retry(
    shadow_env, retry_kind,
):
    SessionLocal, tmp_path = shadow_env
    async with SessionLocal() as session:
        prediction_run_id, artifact_id = await _seed_shadow_scope(session, tmp_path)
        await run_shadow_inference(
            session,
            prediction_run_id=prediction_run_id,
            artifact_id=artifact_id,
            persist=True,
        )
        retry_candidates = [
            {
                "code": f"00000{index}",
                "name": f"重试候选{index}",
                "target_board": 1,
                "candidate_route": "mainline_spread_start",
                "probability": 0.11 + index * 0.02,
                "probability_factors": {
                    "route_score": float(3 - index),
                    "prediction_snapshot_source": "schedule",
                    "prediction_snapshot_context": "promotion_2000",
                    "prediction_snapshot_recorded_at": "2026-08-27T20:05:00",
                    "prediction_snapshot_batch_key": "shadow-test-20260827-retry",
                    "prediction_ranked_selected": True,
                    "prediction_ranked_position": index + 1,
                    "prediction_recall_ranked_position": index + 1,
                },
            }
            for index in range(4)
        ]
        identity = PromotionModelIdentity(
            runtime_mode=PromotionRuntimeMode.LEGACY,
            champion_model_version="shadow_champion_v1",
            challenger_model_version=None,
            feature_version="legacy_test_features",
            data_version="legacy_test_data",
        )
        retry = await append_prediction_run(
            session,
            retry_candidates,
            {1: date(2026, 8, 27)},
            identity=identity,
            snapshot_source="schedule",
            snapshot_context="promotion_2000",
            quality_gate={"gate_passed": True},
        )
        assert retry is not None and retry.run_id != prediction_run_id
        # Isolated malformed/latest-run fixtures: never filter these out and
        # silently validate an older favorable snapshot.
        if retry_kind in {"failed", "running"}:
            await session.execute(update(PromotionPredictionRun).where(
                PromotionPredictionRun.id == retry.run_id).values(status=retry_kind))
        elif retry_kind == "empty":
            await session.execute(delete(PromotionPredictionSnapshot).where(
                PromotionPredictionSnapshot.run_id == retry.run_id))
        elif retry_kind == "other_target":
            await session.execute(update(PromotionPredictionSnapshot).where(
                PromotionPredictionSnapshot.run_id == retry.run_id).values(target_board=2))
        await session.commit()

        evaluation = await evaluate_shadow_artifact(
            session,
            artifact_id=artifact_id,
            target_board=1,
            snapshot_context="promotion_2000",
            persist=True,
            minimum_kline_rows=1,
        )

    assert evaluation["decision"] == "collecting"
    assert "latest official retries" in evaluation["reason"]
    assert evaluation["excluded_runs"][0]["latest_official_prediction_run_id"] == retry.run_id


@pytest.mark.asyncio
async def test_manual_approval_requires_gate_then_overlay_can_rollback(shadow_env, monkeypatch):
    SessionLocal, tmp_path = shadow_env
    async with SessionLocal() as session:
        prediction_run_id, artifact_id = await _seed_shadow_scope(session, tmp_path)
        await run_shadow_inference(
            session,
            prediction_run_id=prediction_run_id,
            artifact_id=artifact_id,
            persist=True,
        )
        monkeypatch.setattr(settings, "PROMOTION_SHADOW_MIN_KLINE_ROWS", 1)
        monkeypatch.setattr(settings, "PROMOTION_DEPLOYED_OVERLAY_ENABLED", True)
        # This lifecycle test has only four candidates, so both Top12 lists are
        # necessarily identical. Relax only that orthogonal rank-gain check here;
        # deployment evidence still has to match the exact active policy object.
        monkeypatch.setitem(
            SHADOW_ACCEPTANCE_POLICY, "minimum_top12_precision_delta", 0.0
        )

        def passing_acceptance(_metrics, **kwargs):
            return {
                "passed": True,
                "decision": "shadow_eligible",
                "policy": {
                    **DEFAULT_ACCEPTANCE_POLICY,
                    **(kwargs.get("policy") or {}),
                },
                "checks": [
                    {"name": "completed_folds", "passed": True},
                    {"name": "validation_trade_days", "passed": True},
                    {"name": "validation_positives", "passed": True},
                ],
                "note": "test gate",
            }

        monkeypatch.setattr(
            "app.promotion.shadow.evaluate_challenger_acceptance", passing_acceptance
        )
        with pytest.raises(ValueError, match="confirmation_phrase"):
            await approve_challenger(
                session,
                artifact_id=artifact_id,
                target_board=1,
                snapshot_context="promotion_2000",
                operator="tester",
                reason="通过影子证据后执行人工晋级验证",
                confirmation_phrase="wrong",
                operation_id="approve-shadow-test-wrong",
                expected_current_event_id=None,
            )

        approved = await approve_challenger(
            session,
            artifact_id=artifact_id,
            target_board=1,
            snapshot_context="promotion_2000",
            operator="tester",
            reason="通过影子证据后执行人工晋级验证",
            confirmation_phrase="APPROVE_CHAMPION",
            operation_id="approve-shadow-test-success",
            expected_current_event_id=None,
        )
        replayed_approval = await approve_challenger(
            session,
            artifact_id=artifact_id,
            target_board=1,
            snapshot_context="promotion_2000",
            operator="tester",
            reason="通过影子证据后执行人工晋级验证",
            confirmation_phrase="APPROVE_CHAMPION",
            operation_id="approve-shadow-test-success",
            expected_current_event_id=None,
        )
        with pytest.raises(ValueError, match="compare-and-set failed"):
            await approve_challenger(
                session,
                artifact_id=artifact_id,
                target_board=1,
                snapshot_context="promotion_2000",
                operator="tester",
                reason="使用过期事件版本尝试重复晋级应被拒绝",
                confirmation_phrase="APPROVE_CHAMPION",
                operation_id="approve-shadow-test-stale-cas",
                expected_current_event_id=None,
            )
        state, _artifact, _event = await current_deployment(session, target_board=1)
        candidates = [
            {
                "code": "000000",
                "target_board": 1,
                "candidate_route": "mainline_spread_start",
                "probability": 0.12,
                "probability_factors": {
                    "route_score": 2.0,
                    "market_regime": "individual_maintrend",
                    "market_regime_snapshot_id": 1,
                    "market_regime_as_of_at": "2026-08-29T15:10:00",
                    "market_regime_version": "test_regime_v1",
                    "market_regime_data_version": "test_regime_data_v1",
                },
                "sub_probabilities": {},
            }
        ]
        missing_regime_candidates = [
            {
                **candidates[0],
                "probability_factors": {"route_score": 2.0},
            }
        ]
        missing_regime_output, missing_regime_meta = (
            await apply_active_promotion_overlay(
                session,
                missing_regime_candidates,
                target_board=1,
                trade_date=date(2026, 8, 29),
                snapshot_context="promotion_2000",
            )
        )
        overlaid, overlay_meta = await apply_active_promotion_overlay(
            session,
            candidates,
            target_board=1,
            trade_date=date(2026, 8, 29),
            snapshot_context="promotion_2000",
        )
        artifact_path = tmp_path / "shadow_t1_v1.json"
        tampered_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        tampered_payload["model"]["classifier"]["coefficients"] = [9.9]
        artifact_path.write_text(json.dumps(tampered_payload), encoding="utf-8")
        tampered_output, tampered_meta = await apply_active_promotion_overlay(
            session,
            candidates,
            target_board=1,
            trade_date=date(2026, 8, 29),
            snapshot_context="promotion_2000",
        )
        tampered_state, _artifact, _event = await current_deployment(
            session, target_board=1
        )
        rolled_back = await rollback_challenger(
            session,
            target_board=1,
            operator="tester",
            reason="验证完成后回滚到原冠军版本保证安全",
            confirmation_phrase="ROLLBACK_CHAMPION",
            operation_id="rollback-shadow-test-success",
            expected_current_event_id=approved["operation_event_id"],
        )
        event_count = await session.scalar(
            select(func.count()).select_from(PromotionDeploymentEvent)
        )

    assert approved["status"] == "approved"
    assert replayed_approval["idempotent_replay"] is True
    assert replayed_approval["operation_event_id"] == approved["operation_event_id"]
    assert state["active"] is True
    assert state["active_model_version"] == "shadow_t1_v1"
    assert missing_regime_output == missing_regime_candidates
    assert missing_regime_meta["applied"] is False
    assert missing_regime_meta["reason"] == "regime_snapshot_unavailable_fail_closed"
    assert overlay_meta["applied"] is True
    assert overlaid[0]["probability"] != candidates[0]["probability"]
    assert overlaid[0]["probability_factors"]["prediction_model_version"] == "shadow_t1_v1"
    assert tampered_meta["applied"] is False
    assert tampered_meta["reason"] == "deployment_integrity_failed_closed_to_legacy"
    assert tampered_output == candidates
    assert tampered_state["active"] is True
    assert tampered_state["effective_active"] is False
    assert "integrity_error" in tampered_state
    assert rolled_back["deployment"]["active"] is False
    assert event_count == 2


@pytest.mark.asyncio
async def test_multilevel_rollback_chain_is_ordered_and_idempotent(
    shadow_env, monkeypatch
):
    SessionLocal, _tmp_path = shadow_env

    async def accept_evidence(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.promotion.deployment._validate_deployment_evidence", accept_evidence
    )
    async with SessionLocal() as session:
        artifacts = []
        for suffix in ("a", "b", "c"):
            artifact = PromotionModelArtifact(
                model_version=f"chain-{suffix}",
                feature_version=FEATURE_VERSION,
                data_version="chain-data",
                algorithm="test",
                status="shadow_eligible",
                artifact_uri=f"/tmp/chain-{suffix}.json",
                params_json="{}",
                metrics_json="{}",
            )
            session.add(artifact)
            artifacts.append(artifact)
        await session.flush()
        previous_event = None
        previous_artifact = None
        for artifact in artifacts:
            event = PromotionDeploymentEvent(
                event_key=f"seed-{artifact.model_version}",
                target_board=1,
                action="approve",
                deployment_mode="probability_overlay",
                artifact_id=artifact.id,
                from_model_version=(
                    previous_artifact.model_version
                    if previous_artifact is not None
                    else "legacy"
                ),
                to_model_version=artifact.model_version,
                operator="tester",
                reason="构造多层人工部署链验证回滚前驱与幂等语义",
                confirmation_phrase="APPROVE_CHAMPION",
                metadata_json=json.dumps(
                    {
                        "previous_event_id": (
                            previous_event.id if previous_event is not None else None
                        ),
                        "from_artifact_id": (
                            previous_artifact.id
                            if previous_artifact is not None
                            else None
                        ),
                        "artifact_sha256": f"sha-{artifact.model_version}",
                    }
                ),
            )
            session.add(event)
            await session.flush()
            previous_event = event
            previous_artifact = artifact
        await session.commit()

        first = await rollback_challenger(
            session,
            target_board=1,
            operator="tester",
            reason="从 C 回滚到 B 并保留下一层前驱",
            confirmation_phrase="ROLLBACK_CHAMPION",
            operation_id="rollback-chain-c-to-b",
            expected_current_event_id=previous_event.id,
        )
        first_replay = await rollback_challenger(
            session,
            target_board=1,
            operator="tester",
            reason="从 C 回滚到 B 并保留下一层前驱",
            confirmation_phrase="ROLLBACK_CHAMPION",
            operation_id="rollback-chain-c-to-b",
            expected_current_event_id=previous_event.id,
        )
        second = await rollback_challenger(
            session,
            target_board=1,
            operator="tester",
            reason="从 B 回滚到 A 并保留 Legacy 前驱",
            confirmation_phrase="ROLLBACK_CHAMPION",
            operation_id="rollback-chain-b-to-a",
            expected_current_event_id=first["operation_event_id"],
        )
        third = await rollback_challenger(
            session,
            target_board=1,
            operator="tester",
            reason="从 A 回滚到受治理的 Legacy Champion",
            confirmation_phrase="ROLLBACK_CHAMPION",
            operation_id="rollback-chain-a-to-legacy",
            expected_current_event_id=second["operation_event_id"],
        )
        event_count = await session.scalar(
            select(func.count()).select_from(PromotionDeploymentEvent)
        )

    assert first["deployment"]["effective_model_version"] == "chain-b"
    assert first_replay["idempotent_replay"] is True
    assert second["deployment"]["effective_model_version"] == "chain-a"
    assert third["deployment"]["active"] is False
    assert event_count == 6


@pytest.mark.asyncio
async def test_model_lab_shadow_read_api_exposes_audited_state(shadow_env, monkeypatch):
    SessionLocal, tmp_path = shadow_env
    async with SessionLocal() as session:
        prediction_run_id, artifact_id = await _seed_shadow_scope(session, tmp_path)

    monkeypatch.setattr(settings, "PROMOTION_SHADOW_MIN_KLINE_ROWS", 1)
    app = FastAPI()
    app.include_router(model_lab.router, prefix="/api/v1/model-lab")

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        inference = await client.post(
            "/api/v1/model-lab/shadow/run",
            json={
                "prediction_run_id": prediction_run_id,
                "artifact_id": artifact_id,
                "persist": True,
            },
        )
        evaluation = await client.post(
            "/api/v1/model-lab/shadow/evaluate",
            json={
                "artifact_id": artifact_id,
                "target_board": 1,
                "snapshot_context": "promotion_2000",
                "persist": True,
            },
        )
        runs = await client.get("/api/v1/model-lab/shadow-runs")
        evaluations = await client.get("/api/v1/model-lab/shadow-evaluations")
        deployments = await client.get("/api/v1/model-lab/deployments")
        identity = await client.get("/api/v1/model-lab/identity")
        governance_body = {
            "target_board": 2,
            "reason": "验证生产模型治理写接口必须经过服务端令牌授权",
            "confirmation_phrase": "ROLLBACK_CHAMPION",
            "operation_id": "governance-auth-test-rollback",
            "expected_current_event_id": None,
        }
        monkeypatch.setattr(settings, "PROMOTION_GOVERNANCE_TOKEN", "")
        disabled_write = await client.post(
            "/api/v1/model-lab/deployments/rollback", json=governance_body
        )
        monkeypatch.setattr(settings, "PROMOTION_GOVERNANCE_TOKEN", "test-secret")
        forbidden_write = await client.post(
            "/api/v1/model-lab/deployments/rollback",
            json=governance_body,
            headers={"X-Claw-Governance-Token": "wrong"},
        )
        authorized_write = await client.post(
            "/api/v1/model-lab/deployments/rollback",
            json=governance_body,
            headers={"X-Claw-Governance-Token": "test-secret"},
        )

    assert inference.status_code == 200
    assert inference.json()["production_unchanged"] is True
    assert evaluation.status_code == 200
    assert evaluation.json()["decision"] == "collecting"
    assert runs.status_code == 200 and runs.json()["count"] == 1
    assert evaluations.status_code == 200 and evaluations.json()["count"] == 1
    assert deployments.status_code == 200
    assert len(deployments.json()["lanes"]) == 2
    assert identity.status_code == 200
    assert identity.json()["automatic_promotion"] is False
    assert disabled_write.status_code == 503
    assert forbidden_write.status_code == 403
    assert authorized_write.status_code == 409
