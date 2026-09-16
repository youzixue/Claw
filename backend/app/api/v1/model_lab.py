"""Promotion model registry, immutable run ledger, and experiment API."""

from __future__ import annotations

import json
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.governance_auth import require_promotion_governance_principal
from app.db.session import get_db
from app.models.promotion import (
    PromotionExperiment,
    PromotionModelArtifact,
    PromotionPredictionRun,
    PromotionPredictionSnapshot,
    PromotionShadowEvaluation,
    PromotionShadowPrediction,
    PromotionShadowRun,
    PromotionTrainingRun,
)
from app.promotion.deployment import (
    approve_challenger,
    list_deployment_state,
    rollback_challenger,
)
from app.promotion.ledger import ensure_prediction_ledger_storage
from app.promotion.modeling.training import train_promotion_challenger
from app.promotion.shadow import evaluate_shadow_artifact, run_shadow_inference
from app.promotion.versioning import get_promotion_model_identity


router = APIRouter()


class PromotionTrainingRequest(BaseModel):
    target_board: int = Field(default=1, ge=1, le=2)
    dataset_source: str = "historical_panel"
    start_date: date | None = None
    end_date: date | None = None
    snapshot_context: str = "promotion_2000"
    historical_lookback_days: int = Field(default=120, ge=80, le=1500)
    historical_candidate_limit: int = Field(default=450, ge=50, le=1500)
    initial_train_days: int = Field(default=60, ge=10, le=1000)
    validation_days: int = Field(default=10, ge=1, le=60)
    step_days: int = Field(default=10, ge=1, le=60)
    calibration_days: int = Field(default=10, ge=3, le=60)
    persist: bool = False


class PromotionShadowRunRequest(BaseModel):
    prediction_run_id: int = Field(ge=1)
    artifact_id: int = Field(ge=1)
    persist: bool = True


class PromotionShadowEvaluationRequest(BaseModel):
    artifact_id: int = Field(ge=1)
    target_board: int = Field(default=1, ge=1, le=2)
    snapshot_context: str = "promotion_2000"
    persist: bool = True


class PromotionApprovalRequest(BaseModel):
    artifact_id: int = Field(ge=1)
    target_board: int = Field(default=1, ge=1, le=2)
    snapshot_context: str = "promotion_2000"
    operator: str | None = None  # compatibility only; server derives the principal
    reason: str = Field(min_length=10, max_length=2000)
    confirmation_phrase: str
    operation_id: str = Field(min_length=8, max_length=200)
    expected_current_event_id: int | None = Field(...)


class PromotionRollbackRequest(BaseModel):
    target_board: int = Field(default=1, ge=1, le=2)
    operator: str | None = None  # compatibility only; server derives the principal
    reason: str = Field(min_length=10, max_length=2000)
    confirmation_phrase: str
    operation_id: str = Field(min_length=8, max_length=200)
    expected_current_event_id: int | None = Field(...)


def _loads(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _run_payload(run: PromotionPredictionRun) -> dict:
    return {
        "id": run.id,
        "run_key": run.run_key,
        "snapshot_batch_key": run.snapshot_batch_key,
        "reference_trade_date": run.reference_trade_date.isoformat(),
        "as_of_at": run.as_of_at.isoformat(timespec="seconds"),
        "snapshot_source": run.snapshot_source,
        "snapshot_context": run.snapshot_context,
        "model_version": run.model_version,
        "feature_version": run.feature_version,
        "data_version": run.data_version,
        "runtime_mode": run.runtime_mode,
        "status": run.status,
        "gate_passed": run.gate_passed,
        "candidate_count": run.candidate_count,
        "ranked_count": run.ranked_count,
        "actionable_count": run.actionable_count,
        "payload_hash": run.payload_hash,
        "metadata": _loads(run.metadata_json),
        "created_at": run.created_at.isoformat(timespec="seconds"),
        "completed_at": (
            run.completed_at.isoformat(timespec="seconds") if run.completed_at else None
        ),
    }


def _snapshot_payload(snapshot: PromotionPredictionSnapshot, *, include_features: bool) -> dict:
    payload = {
        "id": snapshot.id,
        "run_id": snapshot.run_id,
        "legacy_record_id": snapshot.legacy_record_id,
        "code": snapshot.code,
        "name": snapshot.name,
        "target_board": snapshot.target_board,
        "prediction_trade_date": snapshot.prediction_trade_date.isoformat(),
        "horizon_days": snapshot.horizon_days,
        "candidate_route": snapshot.candidate_route,
        "learning_bucket": snapshot.learning_bucket,
        "rank_scope": snapshot.rank_scope,
        "pool_rank": snapshot.pool_rank,
        "rank_position": snapshot.rank_position,
        "recall_rank_position": snapshot.recall_rank_position,
        "raw_probability": snapshot.raw_probability,
        "calibrated_probability": snapshot.calibrated_probability,
        "confidence_level": snapshot.confidence_level,
        "signal_status": snapshot.signal_status,
        "trade_gate_passed": bool(snapshot.trade_gate_passed),
        "actionable": bool(snapshot.actionable),
        "watch_only": bool(snapshot.watch_only),
        "reason": _loads(snapshot.reason_json),
        "created_at": snapshot.created_at.isoformat(timespec="seconds"),
    }
    if include_features:
        payload["features"] = _loads(snapshot.features_json)
    return payload


@router.get("/identity")
async def model_identity(db: AsyncSession = Depends(get_db)):
    identity = get_promotion_model_identity().as_payload()
    deployment = await list_deployment_state(db)
    return {
        **identity,
        "deployments": deployment["lanes"],
        "automatic_promotion": False,
    }


@router.get("/runs")
async def prediction_runs(
    trade_date: date | None = None,
    snapshot_context: str = "",
    model_version: str = "",
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    await ensure_prediction_ledger_storage(db)
    statement = select(PromotionPredictionRun)
    if trade_date is not None:
        statement = statement.where(PromotionPredictionRun.reference_trade_date == trade_date)
    if snapshot_context.strip():
        statement = statement.where(
            PromotionPredictionRun.snapshot_context == snapshot_context.strip()
        )
    if model_version.strip():
        statement = statement.where(PromotionPredictionRun.model_version == model_version.strip())
    rows = list(
        (
            await db.execute(
                statement.order_by(desc(PromotionPredictionRun.as_of_at)).limit(
                    max(1, min(int(limit), 200))
                )
            )
        ).scalars()
    )
    return {"count": len(rows), "runs": [_run_payload(row) for row in rows]}


@router.get("/runs/{run_id}")
async def prediction_run_detail(
    run_id: int,
    include_features: bool = False,
    db: AsyncSession = Depends(get_db),
):
    await ensure_prediction_ledger_storage(db)
    run = await db.get(PromotionPredictionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="prediction run not found")
    snapshots = list(
        (
            await db.execute(
                select(PromotionPredictionSnapshot)
                .where(PromotionPredictionSnapshot.run_id == run_id)
                .order_by(
                    PromotionPredictionSnapshot.target_board,
                    PromotionPredictionSnapshot.rank_position.is_(None),
                    PromotionPredictionSnapshot.rank_position,
                    PromotionPredictionSnapshot.pool_rank,
                )
            )
        ).scalars()
    )
    return {
        "run": _run_payload(run),
        "snapshots": [
            _snapshot_payload(snapshot, include_features=include_features)
            for snapshot in snapshots
        ],
    }


@router.post("/train")
async def train_challenger(
    request: PromotionTrainingRequest,
    db: AsyncSession = Depends(get_db),
):
    """Run leakage-safe walk-forward training; persistence never activates production."""

    try:
        return await train_promotion_challenger(
            db,
            target_board=request.target_board,
            dataset_source=request.dataset_source,
            start_date=request.start_date,
            end_date=request.end_date,
            snapshot_context=request.snapshot_context,
            historical_lookback_days=request.historical_lookback_days,
            historical_candidate_limit=request.historical_candidate_limit,
            initial_train_days=request.initial_train_days,
            validation_days=request.validation_days,
            step_days=request.step_days,
            calibration_days=request.calibration_days,
            persist=request.persist,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/training-runs")
async def training_runs(
    target_board: int | None = None,
    limit: int = 30,
    db: AsyncSession = Depends(get_db),
):
    await ensure_prediction_ledger_storage(db)
    statement = select(PromotionTrainingRun)
    if target_board in {1, 2}:
        statement = statement.where(PromotionTrainingRun.target_board == target_board)
    rows = list(
        (
            await db.execute(
                statement.order_by(desc(PromotionTrainingRun.started_at)).limit(
                    max(1, min(int(limit), 100))
                )
            )
        ).scalars()
    )
    return {
        "count": len(rows),
        "training_runs": [
            {
                "id": row.id,
                "training_key": row.training_key,
                "target_board": row.target_board,
                "status": row.status,
                "model_version": row.model_version,
                "feature_version": row.feature_version,
                "data_version": row.data_version,
                "config": _loads(row.config_json),
                "dataset": _loads(row.dataset_json),
                "metrics": _loads(row.metrics_json),
                "acceptance": _loads(row.acceptance_json),
                "artifact_id": row.artifact_id,
                "error_message": row.error_message,
                "started_at": row.started_at,
                "completed_at": row.completed_at,
            }
            for row in rows
        ],
    }


@router.get("/artifacts")
async def model_artifacts(db: AsyncSession = Depends(get_db)):
    await ensure_prediction_ledger_storage(db)
    rows = list(
        (
            await db.execute(
                select(PromotionModelArtifact).order_by(desc(PromotionModelArtifact.created_at))
            )
        ).scalars()
    )
    return {
        "count": len(rows),
        "artifacts": [
            {
                "id": row.id,
                "model_version": row.model_version,
                "feature_version": row.feature_version,
                "data_version": row.data_version,
                "algorithm": row.algorithm,
                "status": row.status,
                "artifact_uri": row.artifact_uri,
                "params": _loads(row.params_json),
                "metrics": _loads(row.metrics_json),
                "training_start_date": row.training_start_date,
                "training_end_date": row.training_end_date,
                "validation_start_date": row.validation_start_date,
                "validation_end_date": row.validation_end_date,
                "code_commit": row.code_commit,
                "created_at": row.created_at,
                "activated_at": row.activated_at,
                "retired_at": row.retired_at,
            }
            for row in rows
        ],
    }


@router.get("/experiments")
async def experiments(db: AsyncSession = Depends(get_db)):
    await ensure_prediction_ledger_storage(db)
    rows = list(
        (
            await db.execute(
                select(PromotionExperiment).order_by(desc(PromotionExperiment.created_at))
            )
        ).scalars()
    )
    return {
        "count": len(rows),
        "experiments": [
            {
                "id": row.id,
                "experiment_key": row.experiment_key,
                "name": row.name,
                "champion_model_version": row.champion_model_version,
                "challenger_model_version": row.challenger_model_version,
                "status": row.status,
                "allocation_mode": row.allocation_mode,
                "acceptance": _loads(row.acceptance_json),
                "metrics": _loads(row.metrics_json),
                "decision": row.decision,
                "decision_reason": row.decision_reason,
                "started_at": row.started_at,
                "ended_at": row.ended_at,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ],
    }


@router.post("/shadow/run")
async def run_shadow(
    request: PromotionShadowRunRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await run_shadow_inference(
            db,
            prediction_run_id=request.prediction_run_id,
            artifact_id=request.artifact_id,
            persist=request.persist,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/shadow/evaluate")
async def evaluate_shadow(
    request: PromotionShadowEvaluationRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await evaluate_shadow_artifact(
            db,
            artifact_id=request.artifact_id,
            target_board=request.target_board,
            snapshot_context=request.snapshot_context,
            persist=request.persist,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/shadow-runs")
async def shadow_runs(
    artifact_id: int | None = None,
    target_board: int | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    await ensure_prediction_ledger_storage(db)
    statement = select(PromotionShadowRun)
    if artifact_id is not None:
        statement = statement.where(PromotionShadowRun.artifact_id == artifact_id)
    if target_board in {1, 2}:
        statement = statement.where(PromotionShadowRun.target_board == target_board)
    rows = list(
        (
            await db.scalars(
                statement.order_by(
                    desc(PromotionShadowRun.reference_trade_date),
                    desc(PromotionShadowRun.id),
                ).limit(max(1, min(int(limit), 300)))
            )
        ).all()
    )
    return {
        "count": len(rows),
        "shadow_runs": [
            {
                "id": row.id,
                "shadow_key": row.shadow_key,
                "prediction_run_id": row.prediction_run_id,
                "artifact_id": row.artifact_id,
                "target_board": row.target_board,
                "snapshot_context": row.snapshot_context,
                "reference_trade_date": row.reference_trade_date,
                "as_of_at": row.as_of_at,
                "champion_model_version": row.champion_model_version,
                "challenger_model_version": row.challenger_model_version,
                "status": row.status,
                "candidate_count": row.candidate_count,
                "metadata": _loads(row.metadata_json),
                "created_at": row.created_at,
            }
            for row in rows
        ],
    }


@router.get("/shadow-runs/{shadow_run_id}")
async def shadow_run_detail(
    shadow_run_id: int,
    db: AsyncSession = Depends(get_db),
):
    await ensure_prediction_ledger_storage(db)
    run = await db.get(PromotionShadowRun, shadow_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="shadow run not found")
    rows = list(
        (
            await db.scalars(
                select(PromotionShadowPrediction)
                .where(PromotionShadowPrediction.shadow_run_id == run.id)
                .order_by(PromotionShadowPrediction.challenger_rank_position)
            )
        ).all()
    )
    return {
        "run": {
            "id": run.id,
            "prediction_run_id": run.prediction_run_id,
            "artifact_id": run.artifact_id,
            "target_board": run.target_board,
            "snapshot_context": run.snapshot_context,
            "reference_trade_date": run.reference_trade_date,
            "champion_model_version": run.champion_model_version,
            "challenger_model_version": run.challenger_model_version,
            "candidate_count": run.candidate_count,
            "metadata": _loads(run.metadata_json),
        },
        "predictions": [
            {
                "id": row.id,
                "prediction_snapshot_id": row.prediction_snapshot_id,
                "code": row.code,
                "name": row.name,
                "market_regime": row.market_regime,
                "champion_probability": row.champion_probability,
                "challenger_raw_probability": row.challenger_raw_probability,
                "challenger_probability": row.challenger_probability,
                "champion_rank_position": row.champion_rank_position,
                "challenger_rank_position": row.challenger_rank_position,
            }
            for row in rows
        ],
    }


@router.get("/shadow-evaluations")
async def shadow_evaluations(
    artifact_id: int | None = None,
    target_board: int | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    await ensure_prediction_ledger_storage(db)
    statement = select(PromotionShadowEvaluation)
    if artifact_id is not None:
        statement = statement.where(PromotionShadowEvaluation.artifact_id == artifact_id)
    if target_board in {1, 2}:
        statement = statement.where(PromotionShadowEvaluation.target_board == target_board)
    rows = list(
        (
            await db.scalars(
                statement.order_by(desc(PromotionShadowEvaluation.created_at)).limit(
                    max(1, min(int(limit), 300))
                )
            )
        ).all()
    )
    return {
        "count": len(rows),
        "evaluations": [
            {
                "id": row.id,
                "evaluation_key": row.evaluation_key,
                "artifact_id": row.artifact_id,
                "target_board": row.target_board,
                "snapshot_context": row.snapshot_context,
                "challenger_model_version": row.challenger_model_version,
                "label_version": row.label_version,
                "outcome_end_date": row.outcome_end_date,
                "evaluated_shadow_run_count": row.evaluated_shadow_run_count,
                "trade_day_count": row.trade_day_count,
                "sample_count": row.sample_count,
                "positive_count": row.positive_count,
                "decision": row.decision,
                "metrics": _loads(row.metrics_json),
                "acceptance": _loads(row.acceptance_json),
                "metadata": _loads(row.metadata_json),
                "created_at": row.created_at,
            }
            for row in rows
        ],
    }


@router.get("/deployments")
async def deployments(db: AsyncSession = Depends(get_db)):
    return await list_deployment_state(db)


@router.post("/deployments/approve")
async def approve_deployment(
    request: PromotionApprovalRequest,
    db: AsyncSession = Depends(get_db),
    principal: str = Depends(require_promotion_governance_principal),
):
    try:
        return await approve_challenger(
            db,
            artifact_id=request.artifact_id,
            target_board=request.target_board,
            snapshot_context=request.snapshot_context,
            operator=principal,
            reason=request.reason,
            confirmation_phrase=request.confirmation_phrase,
            operation_id=request.operation_id,
            expected_current_event_id=request.expected_current_event_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/deployments/rollback")
async def rollback_deployment(
    request: PromotionRollbackRequest,
    db: AsyncSession = Depends(get_db),
    principal: str = Depends(require_promotion_governance_principal),
):
    try:
        return await rollback_challenger(
            db,
            target_board=request.target_board,
            operator=principal,
            reason=request.reason,
            confirmation_phrase=request.confirmation_phrase,
            operation_id=request.operation_id,
            expected_current_event_id=request.expected_current_event_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
