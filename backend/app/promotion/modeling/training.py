"""End-to-end challenger training, evaluation, artifact registration and shadow setup."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.promotion import (
    PromotionExperiment,
    PromotionModelArtifact,
    PromotionTrainingRun,
)
from app.promotion.ledger import ensure_prediction_ledger_storage
from app.promotion.modeling.acceptance import evaluate_challenger_acceptance
from app.promotion.modeling.ledger_dataset import build_ledger_training_dataset
from app.promotion.modeling.features import FEATURE_VERSION
from app.promotion.modeling.historical_dataset import build_historical_panel_dataset
from app.promotion.modeling.walk_forward import (
    fit_temporally_calibrated_model,
    walk_forward_evaluate,
)
from app.promotion.versioning import get_promotion_model_identity


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True, separators=(",", ":"))


def _artifact_directory(value: str | Path | None) -> Path:
    configured = Path(value or settings.PROMOTION_ARTIFACT_DIR).expanduser()
    if not configured.is_absolute():
        configured = Path.cwd() / configured
    return configured.resolve()


def _public_walk_forward(payload: dict) -> dict:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"predictions", "baseline_probabilities", "labels", "trade_dates"}
    }


def _historical_universe_rank_metrics(evaluation: dict, diagnostics: dict) -> dict:
    """Translate in-pool hit counts to honest full-universe recall denominators."""

    validation_dates = set(evaluation.get("trade_dates") or [])
    daily = [
        item
        for item in diagnostics.get("daily", [])
        if item.get("trade_date") in validation_dates
    ]
    universe_positives = sum(int(item.get("universe_positive_count") or 0) for item in daily)
    candidate_positives = sum(
        int(item.get("candidate_positive_count") or 0) for item in daily
    )

    def rank_metrics(metrics: dict) -> dict:
        result = {}
        for key, item in (metrics.get("daily_rank") or {}).items():
            hits = int(item.get("hit_count") or 0)
            result[str(key)] = {
                "selected_count": int(item.get("selected_count") or 0),
                "hit_count": hits,
                "precision": item.get("precision"),
                "candidate_pool_recall": item.get("recall"),
                "full_universe_recall": round(
                    hits / universe_positives if universe_positives else 0.0,
                    6,
                ) if not sum(int(d.get("universe_unknown_count") or 0) for d in daily) else None,
                **({"selected_unknown_count": item["selected_unknown_count"]}
                   if "selected_unknown_count" in item else {}),
            }
        return result

    return {
        "validation_trade_day_count": len({item.get("trade_date") for item in daily}),
        "universe_positive_count": universe_positives,
        "candidate_positive_count": candidate_positives,
        "candidate_prefilter_recall": round(
            candidate_positives / universe_positives if universe_positives else 0.0,
            6,
        ) if not sum(int(d.get("universe_unknown_count") or 0) for d in daily) else None,
        "universe_unknown_count": sum(int(d.get("universe_unknown_count") or 0) for d in daily),
        "challenger": rank_metrics(evaluation.get("challenger_metrics") or {}),
        "champion_reference": rank_metrics(evaluation.get("champion_metrics") or {}),
        "note": (
            "candidate_pool_recall uses only positives surviving the historical prefilter; "
            "full_universe_recall uses every clean main-board positive on validation dates"
        ),
    }


def _model_version(target_board: int, data_version: str, artifact_core: dict) -> str:
    digest = hashlib.sha256(_json(artifact_core).encode("utf-8")).hexdigest()[:12]
    return (
        f"promotion_t{int(target_board)}_numpy_lr_"
        f"{datetime.now().strftime('%Y%m%d')}_{data_version[-8:]}_{digest}"
    )


def _write_artifact_atomic(directory: Path, model_version: str, payload: dict) -> Path:
    """Create one immutable content-addressed artifact without overwriting bytes.

    created_at is audit metadata and is deliberately excluded from the model
    version digest. A same-day deterministic retrain therefore reuses the first
    artifact bytes; any other payload difference under the same version is a hard
    collision instead of silently invalidating prior shadow/deployment hashes.
    """

    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{model_version}.json"

    def comparable(value: dict) -> str:
        normalized = dict(value)
        normalized.pop("created_at", None)
        return _json(normalized)

    def validate_existing() -> None:
        try:
            existing = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"existing artifact {destination} is unreadable or malformed"
            ) from exc
        if not isinstance(existing, dict) or comparable(existing) != comparable(payload):
            raise ValueError(
                f"immutable artifact collision for model_version {model_version}"
            )

    if destination.exists():
        validate_existing()
        return destination

    temporary = directory / f".{model_version}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(_json(payload), encoding="utf-8")
    try:
        try:
            # Same-directory hard-link publication is atomic and never replaces
            # an artifact another worker may have published concurrently.
            os.link(temporary, destination)
        except FileExistsError:
            validate_existing()
    finally:
        temporary.unlink(missing_ok=True)
    return destination


async def train_promotion_challenger(
    db: AsyncSession,
    *,
    target_board: int,
    dataset_source: str = "prediction_snapshots",
    start_date: date | None = None,
    end_date: date | None = None,
    snapshot_context: str = "promotion_2000",
    historical_lookback_days: int = 250,
    historical_candidate_limit: int = 450,
    initial_train_days: int = 25,
    validation_days: int = 5,
    step_days: int = 5,
    calibration_days: int = 5,
    minimum_samples: int | None = None,
    minimum_trade_days: int | None = None,
    minimum_positives: int | None = None,
    persist: bool = False,
    artifact_dir: str | Path | None = None,
    as_of_at: datetime | None = None,
    train_window_days: int | None = None,
) -> dict:
    """Train and evaluate one target lane; never activates it as champion."""

    target_board = int(target_board)
    if target_board not in {1, 2}:
        raise ValueError("target_board must be 1 or 2")
    dataset_source = str(dataset_source or "prediction_snapshots").strip().lower()
    if dataset_source not in {"prediction_snapshots", "historical_panel"}:
        raise ValueError("dataset_source must be prediction_snapshots or historical_panel")
    minimum_samples = int(minimum_samples or settings.PROMOTION_TRAINING_MIN_SAMPLES)
    minimum_trade_days = int(
        minimum_trade_days or settings.PROMOTION_TRAINING_MIN_TRADE_DAYS
    )
    minimum_positives = int(
        minimum_positives or settings.PROMOTION_TRAINING_MIN_POSITIVES
    )
    resolved_as_of_at = as_of_at if as_of_at is not None else datetime.now()
    if not isinstance(resolved_as_of_at, datetime) or resolved_as_of_at.tzinfo is not None:
        raise ValueError("as_of_at must be a naive Asia/Shanghai clock")
    config = {
        "target_board": target_board,
        "as_of_at": resolved_as_of_at,
        "hist_coverage_policy": ("unverified_pretraining_only" if dataset_source == "historical_panel"
                                 else "required_materialization_v1"),
        "dataset_source": dataset_source,
        "start_date": start_date,
        "end_date": end_date,
        "snapshot_context": snapshot_context,
        "historical_lookback_days": historical_lookback_days,
        "historical_candidate_limit": historical_candidate_limit,
        "initial_train_days": initial_train_days,
        "validation_days": validation_days,
        "step_days": step_days,
        "calibration_days": calibration_days,
        "train_window_days": train_window_days,
        "minimum_samples": minimum_samples,
        "minimum_trade_days": minimum_trade_days,
        "minimum_positives": minimum_positives,
        "persist": persist,
    }
    training_row: PromotionTrainingRun | None = None
    training_run_id: int | None = None
    if persist:
        await ensure_prediction_ledger_storage(db)
        training_row = PromotionTrainingRun(
            training_key=f"train-{uuid.uuid4().hex}",
            target_board=target_board,
            status="running",
            feature_version=FEATURE_VERSION,
            config_json=_json(config),
            started_at=datetime.now(),
        )
        db.add(training_row)
        await db.commit()
        training_run_id = int(training_row.id)

    try:
        if dataset_source == "historical_panel":
            dataset = await build_historical_panel_dataset(
                db,
                target_board=target_board,
                lookback_trade_days=historical_lookback_days,
                candidate_limit_per_day=historical_candidate_limit,
                end_date=end_date,
            )
        else:
            dataset = await build_ledger_training_dataset(
                db,
                as_of_at=resolved_as_of_at,
                target_board=target_board,
                start_date=start_date,
                end_date=end_date,
                snapshot_context=snapshot_context,
            )
        sample_count = sum(row.label is not None for row in dataset.rows)
        trade_day_count = len({row.trade_date for row in dataset.rows if row.label is not None})
        positive_count = sum(row.label == 1 for row in dataset.rows)
        guard_checks = {
            "minimum_samples": {
                "actual": sample_count,
                "required": minimum_samples,
                "passed": sample_count >= minimum_samples,
            },
            "minimum_trade_days": {
                "actual": trade_day_count,
                "required": minimum_trade_days,
                "passed": trade_day_count >= minimum_trade_days,
            },
            "minimum_positives": {
                "actual": positive_count,
                "required": minimum_positives,
                "passed": positive_count >= minimum_positives,
            },
        }
        if not all(item["passed"] for item in guard_checks.values()):
            raise ValueError(f"training data guard failed: {guard_checks}")

        evaluation = walk_forward_evaluate(
            dataset.rows,
            initial_train_days=initial_train_days,
            validation_days=validation_days,
            step_days=step_days,
            calibration_days=calibration_days,
            allow_unmaterialized_pretraining=dataset_source == "historical_panel",
            allow_unknown_labels=dataset_source == "historical_panel",
            train_window_days=train_window_days,
        )
        if dataset_source == "historical_panel":
            evaluation["historical_universe_rank_metrics"] = (
                _historical_universe_rank_metrics(evaluation, dataset.diagnostics)
            )
        acceptance = evaluate_challenger_acceptance(evaluation)
        if dataset_source == "historical_panel":
            acceptance = {
                **acceptance,
                "passed": False,
                "decision": "pretraining_only",
                "note": (
                    "历史面板用于预训练和因子筛选；参考概率不是冻结的生产 Champion，"
                    "因此不能据此直接进入影子或生产。仍需在不可变预测快照上做时间外验收。"
                ),
            }
        final_model = fit_temporally_calibrated_model(
            dataset.rows, calibration_days=calibration_days,
            allow_unmaterialized_pretraining=dataset_source == "historical_panel",
            allow_unknown_labels=dataset_source == "historical_panel",
            train_window_days=train_window_days,
        )
        public_evaluation = _public_walk_forward(evaluation)
        artifact_core = {
            "target_board": target_board,
            "feature_version": FEATURE_VERSION,
            "data_version": dataset.data_version,
            "model": final_model.payload(),
            "walk_forward": public_evaluation,
            "acceptance": acceptance,
            "training_config": {
                key: value for key, value in config.items() if key != "persist"
            },
            "dataset_diagnostics": dataset.diagnostics,
        }
        model_version = _model_version(target_board, dataset.data_version, artifact_core)
        artifact_payload = {
            "schema_version": "promotion_model_artifact_v1",
            "model_version": model_version,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            **artifact_core,
        }
        response = {
            "status": "completed",
            "persisted": persist,
            "target_board": target_board,
            "dataset_source": dataset_source,
            "comparison_baseline": (
                "historical_prefilter_reference"
                if dataset_source == "historical_panel"
                else "frozen_production_prediction"
            ),
            "model_version": model_version,
            "feature_version": FEATURE_VERSION,
            "data_version": dataset.data_version,
            "dataset": {
                **dataset.diagnostics,
                "start_date": dataset.start_date,
                "end_date": dataset.end_date,
                "guards": guard_checks,
            },
            "walk_forward": public_evaluation,
            "acceptance": acceptance,
            "artifact_uri": None,
            "production_unchanged": True,
        }

        if persist:
            directory = _artifact_directory(artifact_dir)
            destination = _write_artifact_atomic(directory, model_version, artifact_payload)
            existing_artifact = await db.scalar(
                select(PromotionModelArtifact).where(
                    PromotionModelArtifact.model_version == model_version
                )
            )
            expected_status = (
                "pretrained"
                if dataset_source == "historical_panel"
                else "shadow_eligible"
                if acceptance["passed"]
                else "rejected"
            )
            completed_folds = [
                fold for fold in evaluation["folds"] if fold.get("status") == "completed"
            ]
            validation_start_date = (
                date.fromisoformat(completed_folds[0]["validation_start_date"])
                if completed_folds
                else None
            )
            validation_end_date = (
                date.fromisoformat(completed_folds[-1]["validation_end_date"])
                if completed_folds
                else None
            )
            registry_values = {
                "feature_version": FEATURE_VERSION,
                "data_version": dataset.data_version,
                "algorithm": "numpy_logistic_platt",
                "artifact_uri": str(destination),
                "params_json": _json(final_model.payload()),
                "metrics_json": _json(public_evaluation),
                "training_start_date": (date.fromisoformat(final_model.fit_start_date)
                                        if train_window_days is not None else dataset.start_date),
                "training_end_date": date.fromisoformat(final_model.fit_end_date),
                "validation_start_date": validation_start_date,
                "validation_end_date": validation_end_date,
            }
            if existing_artifact is not None:
                mismatches = {
                    field: {"existing": getattr(existing_artifact, field), "expected": value}
                    for field, value in registry_values.items()
                    if getattr(existing_artifact, field) != value
                }
                allowed_statuses = (
                    {"shadow_eligible", "approved", "active"}
                    if expected_status == "shadow_eligible"
                    else {expected_status}
                )
                if existing_artifact.status not in allowed_statuses:
                    mismatches["status"] = {
                        "existing": existing_artifact.status,
                        "expected": sorted(allowed_statuses),
                    }
                if mismatches:
                    raise ValueError(
                        f"immutable artifact registry collision for {model_version}: {mismatches}"
                    )
                artifact = existing_artifact
            else:
                artifact = PromotionModelArtifact(
                    model_version=model_version,
                    status=expected_status,
                    created_at=datetime.now(),
                    **registry_values,
                )
                db.add(artifact)
            await db.flush()

            if acceptance["passed"]:
                identity = get_promotion_model_identity()
                experiment_key = f"shadow-{model_version}"
                experiment = await db.scalar(
                    select(PromotionExperiment).where(
                        PromotionExperiment.experiment_key == experiment_key
                    )
                )
                if experiment is None:
                    db.add(
                        PromotionExperiment(
                            experiment_key=experiment_key,
                            name=f"T{target_board} challenger shadow validation",
                            champion_model_version=identity.active_model_version,
                            challenger_model_version=model_version,
                            status="ready",
                            allocation_mode="shadow",
                            acceptance_json=_json(acceptance),
                            metrics_json=_json(public_evaluation),
                            created_at=datetime.now(),
                            updated_at=datetime.now(),
                        )
                    )

            assert training_row is not None
            training_row.status = "completed"
            training_row.model_version = model_version
            training_row.data_version = dataset.data_version
            training_row.dataset_json = _json(response["dataset"])
            training_row.metrics_json = _json(public_evaluation)
            training_row.acceptance_json = _json(acceptance)
            training_row.artifact_id = artifact.id
            training_row.completed_at = datetime.now()
            artifact_id = int(artifact.id)
            await db.commit()
            response["training_run_id"] = training_run_id
            response["artifact_id"] = artifact_id
            response["artifact_uri"] = str(destination)
        return response
    except Exception as exc:
        if persist and training_run_id is not None:
            await db.rollback()
            persisted_row = await db.get(PromotionTrainingRun, training_run_id)
            if persisted_row is not None:
                persisted_row.status = "failed"
                persisted_row.error_message = str(exc)[:4000]
                persisted_row.completed_at = datetime.now()
                await db.commit()
        raise
