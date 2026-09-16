"""Leakage-safe Champion/Challenger shadow execution and cumulative evaluation."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.core.trade_calendar import is_official_closed_day
from app.models.promotion import (
    PromotionModelArtifact,
    PromotionPredictionRun,
    PromotionPredictionSnapshot,
    PromotionShadowEvaluation,
    PromotionShadowPrediction,
    PromotionShadowRun,
)
from app.models.governance import TradeCalendarModel, DataQualityRun
from app.models.regime import MarketRegimeSnapshot
from app.models.stock import LimitUpPool, StockKline
from app.promotion.labels import PROMOTION_LABEL_VERSION, promotion_event_label
from app.promotion.ledger import ensure_prediction_ledger_storage
from app.promotion.modeling.acceptance import (
    SHADOW_ACCEPTANCE_POLICY,
    evaluate_challenger_acceptance,
)
from app.promotion.modeling.features import (
    FEATURE_VERSION,
    FeatureRow,
    extract_point_in_time_features,
)
from app.promotion.modeling.inference import LoadedPromotionArtifact, load_promotion_artifact
from app.promotion.modeling.metrics import average_precision, classification_metrics
from app.promotion.modeling.walk_forward import regime_sliced_metrics
from app.promotion.regime import REGIME_LABELS
from app.promotion.identity_evidence import (
    PROFILE as IDENTITY_PROFILE, prepare_identity_index, identity_pair_gate,
)
from app.promotion.outcome_materials import (
    PROFILE as MATERIAL_PROFILE, prepare_outcome_index, outcome_pair_gate,
)
from app.promotion.outcome_evidence import (
    OUTCOME_EVIDENCE_VERSION, next_recorded_trade_day, formal_outcome_bar_error,
    FORWARD_SHADOW_VERSION, forward_shadow_clock_evidence, _FORMAL_CLOSE_SOURCES,
    _is_completed_outcome_date, require_paired_material_rows,
)


SHADOW_LABEL_VERSION = PROMOTION_LABEL_VERSION
SHADOW_EVALUATION_VERSION = "promotion_shadow_evaluation_v9_sealed_outcomes"
_ALLOWED_ARTIFACT_STATUSES = {"shadow_eligible", "approved", "active"}


def _shadow_now() -> datetime:
    """Actual scoring receipt clock; separate from the frozen prediction cutoff."""
    return datetime.now()


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    )


def _loads(value: str | None, default=None) -> dict:
    fallback = {} if default is None else default
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return parsed if isinstance(parsed, dict) else fallback


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _probability(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    if not np.isfinite(parsed):
        parsed = 0.0
    return float(np.clip(parsed, 1e-7, 1.0 - 1e-7))


def _parse_iso_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise ValueError(f"artifact {field} is not a valid ISO date") from exc


async def _regime_for_run(
    db: AsyncSession,
    run: PromotionPredictionRun,
    snapshots: list[PromotionPredictionSnapshot],
) -> tuple[str, dict]:
    explicit = Counter()
    for snapshot in snapshots:
        factors = _loads(snapshot.features_json, {})
        value = str(factors.get("market_regime") or "").strip()
        if value in REGIME_LABELS:
            explicit[value] += 1
    if explicit:
        regime = explicit.most_common(1)[0][0]
        return regime, {"source": "frozen_candidate_features", "counts": dict(explicit)}

    row = await db.scalar(
        select(MarketRegimeSnapshot)
        .where(
            MarketRegimeSnapshot.trade_date == run.reference_trade_date,
            MarketRegimeSnapshot.snapshot_context == "postmarket",
            MarketRegimeSnapshot.created_at <= run.as_of_at,
            MarketRegimeSnapshot.as_of_at <= run.as_of_at,
        )
        .order_by(desc(MarketRegimeSnapshot.created_at), desc(MarketRegimeSnapshot.id))
        .limit(1)
    )
    if row is None or row.primary_regime not in REGIME_LABELS:
        return "unknown", {"source": "missing", "regime_snapshot_id": None}
    return row.primary_regime, {
        "source": "point_in_time_regime_snapshot",
        "regime_snapshot_id": row.id,
        "regime_snapshot_created_at": row.created_at,
        "created_after_prediction_run": bool(row.created_at > run.as_of_at),
        "claim_boundary": (
            "regime uses the same reference session's close data; it is never an outcome label"
        ),
    }


def _validate_shadow_scope(
    run: PromotionPredictionRun,
    artifact: PromotionModelArtifact,
    loaded: LoadedPromotionArtifact,
) -> None:
    if run.status != "completed" or run.snapshot_source != "schedule":
        raise ValueError("shadow inference requires a completed official schedule run")
    if run.gate_passed is not True:
        raise ValueError("prediction run lacks a passed immutable data-quality gate")
    if artifact.status not in _ALLOWED_ARTIFACT_STATUSES:
        raise ValueError(
            f"artifact status {artifact.status!r} is not eligible for shadow inference"
        )
    if not bool(loaded.offline_acceptance.get("passed")) or str(
        loaded.offline_acceptance.get("decision") or ""
    ) != "shadow_eligible":
        raise ValueError(
            "artifact JSON does not contain a passed frozen-snapshot offline acceptance"
        )
    if (
        artifact.feature_version != loaded.feature_version
        or artifact.data_version != loaded.data_version
    ):
        raise ValueError("artifact registry versions do not match the immutable JSON payload")
    configured_context = str(loaded.training_config.get("snapshot_context") or "").strip()
    dataset_source = str(loaded.training_config.get("dataset_source") or "").strip()
    if dataset_source and dataset_source != "prediction_snapshots":
        raise ValueError("historical-panel pretraining artifacts cannot enter shadow execution")
    if configured_context and configured_context != run.snapshot_context:
        raise ValueError(
            "prediction run context does not match the artifact's frozen-snapshot contract"
        )
    fit_end = _parse_iso_date(loaded.fit_end_date, "fit_end_date")
    calibration_end = _parse_iso_date(
        loaded.calibration_end_date, "calibration_end_date"
    )
    evidence_end = max(fit_end, calibration_end)
    if run.reference_trade_date <= evidence_end:
        raise ValueError(
            "shadow run is not out-of-time: prediction date must be after artifact evidence"
        )
    if run.as_of_at.date() > run.reference_trade_date:
        raise ValueError("prediction run cutoff is later than its declared reference date")


def _shadow_run_payload(
    run: PromotionShadowRun,
    predictions: list[PromotionShadowPrediction] | None = None,
    *,
    created: bool | None = None,
) -> dict:
    payload = {
        "id": run.id,
        "shadow_key": run.shadow_key,
        "prediction_run_id": run.prediction_run_id,
        "artifact_id": run.artifact_id,
        "target_board": run.target_board,
        "snapshot_context": run.snapshot_context,
        "reference_trade_date": run.reference_trade_date.isoformat(),
        "as_of_at": run.as_of_at.isoformat(timespec="seconds"),
        "champion_model_version": run.champion_model_version,
        "challenger_model_version": run.challenger_model_version,
        "feature_version": run.feature_version,
        "data_version": run.data_version,
        "status": run.status,
        "candidate_count": run.candidate_count,
        "payload_hash": run.payload_hash,
        "metadata": _loads(run.metadata_json, {}),
        "created_at": run.created_at.isoformat(timespec="seconds"),
        "completed_at": run.completed_at.isoformat(timespec="seconds"),
        "production_unchanged": True,
    }
    if created is not None:
        payload["created"] = created
    if predictions is not None:
        payload["predictions"] = [
            {
                "id": row.id,
                "prediction_snapshot_id": row.prediction_snapshot_id,
                "code": row.code,
                "name": row.name,
                "target_board": row.target_board,
                "prediction_trade_date": row.prediction_trade_date.isoformat(),
                "candidate_route": row.candidate_route,
                "market_regime": row.market_regime,
                "champion_probability": row.champion_probability,
                "challenger_raw_probability": row.challenger_raw_probability,
                "challenger_probability": row.challenger_probability,
                "champion_rank_position": row.champion_rank_position,
                "challenger_rank_position": row.challenger_rank_position,
                "features_hash": row.features_hash,
            }
            for row in predictions
        ]
    return payload


async def run_shadow_inference(
    db: AsyncSession,
    *,
    prediction_run_id: int,
    artifact_id: int,
    persist: bool = True,
    quality_gate: dict | None = None,
) -> dict:
    """Score exactly the frozen Champion candidate set; never alters its output."""

    if persist:
        await ensure_prediction_ledger_storage(db)
    run = await db.get(PromotionPredictionRun, int(prediction_run_id))
    artifact = await db.get(PromotionModelArtifact, int(artifact_id))
    if run is None:
        raise ValueError("prediction run not found")
    if artifact is None:
        raise ValueError("model artifact not found")
    loaded = load_promotion_artifact(
        str(artifact.artifact_uri or ""),
        expected_model_version=artifact.model_version,
        expected_feature_version=FEATURE_VERSION,
    )
    _validate_shadow_scope(run, artifact, loaded)
    if quality_gate is not None and not bool(quality_gate.get("gate_passed")):
        raise ValueError("scheduler data-quality gate did not pass; shadow run was skipped")

    snapshots = list(
        (
            await db.scalars(
                select(PromotionPredictionSnapshot)
                .where(
                    PromotionPredictionSnapshot.run_id == run.id,
                    PromotionPredictionSnapshot.target_board == loaded.target_board,
                )
                .order_by(PromotionPredictionSnapshot.id)
            )
        ).all()
    )
    if not snapshots:
        raise ValueError("prediction run has no candidates for the artifact target lane")
    if any(row.prediction_trade_date != run.reference_trade_date for row in snapshots):
        raise ValueError("prediction run contains inconsistent target-lane trade dates")
    frozen_champion_versions = {
        str(
            _loads(snapshot.features_json, {}).get("prediction_model_version")
            or run.model_version
        )
        for snapshot in snapshots
    }
    if frozen_champion_versions == {artifact.model_version}:
        raise ValueError(
            "artifact is already the frozen Champion for this target lane; self-shadow skipped"
        )

    regime, regime_evidence = await _regime_for_run(db, run, snapshots)
    rows: list[FeatureRow] = []
    feature_payloads: list[dict] = []
    rank_contracts: list[dict] = []
    for snapshot in snapshots:
        frozen = _loads(snapshot.features_json, {})
        features = extract_point_in_time_features(frozen)
        rows.append(
            FeatureRow(
                code=snapshot.code,
                trade_date=snapshot.prediction_trade_date.isoformat(),
                target_board=snapshot.target_board,
                label=0,
                baseline_probability=_probability(snapshot.calibrated_probability),
                candidate_route=snapshot.candidate_route,
                values=features,
                market_regime=regime,
                hist_materialization=frozen.get("hist_materialization"),
                feature_as_of_at=run.as_of_at,
            )
        )
        feature_payloads.append(features)
        rank_contracts.append(
            {
                "version": str(frozen.get("prediction_rank_contract_version") or ""),
                "complete": bool(frozen.get("prediction_rank_contract_complete")),
                "eligible": bool(frozen.get("prediction_rank_eligible")),
                "eligible_count": int(frozen.get("prediction_rank_eligible_count") or 0),
                "formal_limit": int(frozen.get("prediction_ranked_limit") or 0),
                "recall_limit": int(frozen.get("prediction_recall_ranked_limit") or 0),
            }
        )

    raw_probabilities, probabilities = loaded.predict(rows)
    rank_order = sorted(
        range(len(rows)),
        key=lambda index: (
            -float(probabilities[index]),
            rows[index].code,
            int(snapshots[index].id),
        ),
    )
    challenger_rank = {index: rank + 1 for rank, index in enumerate(rank_order)}
    canonical = [
        {
            "prediction_snapshot_id": snapshots[index].id,
            "code": rows[index].code,
            "champion_probability": round(rows[index].baseline_probability, 12),
            "challenger_raw_probability": round(float(raw_probabilities[index]), 12),
            "challenger_probability": round(float(probabilities[index]), 12),
            "challenger_rank_position": challenger_rank[index],
            "rank_eligible": rank_contracts[index]["eligible"],
            "rank_contract_version": rank_contracts[index]["version"],
            "features_hash": _digest(feature_payloads[index]),
        }
        for index in range(len(rows))
    ]
    payload_hash = _digest(canonical)
    shadow_key = _digest(
        {
            "prediction_run_id": run.id,
            "artifact_id": artifact.id,
            "target_board": loaded.target_board,
            "artifact_sha256": loaded.artifact_sha256,
            "payload_hash": payload_hash,
        }
    )

    existing = await db.scalar(
        select(PromotionShadowRun).where(
            PromotionShadowRun.prediction_run_id == run.id,
            PromotionShadowRun.artifact_id == artifact.id,
            PromotionShadowRun.target_board == loaded.target_board,
        )
    )
    if existing is not None:
        if (existing.payload_hash != payload_hash or existing.shadow_key != shadow_key
                or _loads(existing.metadata_json, {}).get("artifact_sha256") != loaded.artifact_sha256):
            raise ValueError(
                "an existing immutable shadow run has a different payload; register a new artifact version"
            )
        predictions = list(
            (
                await db.scalars(
                    select(PromotionShadowPrediction)
                    .where(PromotionShadowPrediction.shadow_run_id == existing.id)
                    .order_by(PromotionShadowPrediction.challenger_rank_position)
                )
            ).all()
        )
        return _shadow_run_payload(existing, predictions, created=False)

    now = _shadow_now()
    effective_quality_gate = {
        "source": (
            "prediction_run"
            if run.gate_passed is True
            else "scheduler_audit"
            if quality_gate is not None
            else "unknown"
        ),
        "gate_passed": (
            True
            if run.gate_passed is True
            else bool(quality_gate.get("gate_passed"))
            if quality_gate is not None
            else None
        ),
        "status": quality_gate.get("status") if quality_gate else None,
        "run_id": quality_gate.get("run_id") if quality_gate else None,
        "blocking_count": quality_gate.get("blocking_count") if quality_gate else None,
    }
    metadata = {
        "artifact_uri": loaded.artifact_path,
        "artifact_sha256": loaded.artifact_sha256,
        "feature_count": loaded.feature_count,
        "regime": regime_evidence,
        "frozen_market_regime": regime,
        "input_quality_gate": effective_quality_gate,
        "same_candidate_set": True,
        "rank_contract_coverage": round(
            sum(
                bool(item.get("complete"))
                and item.get("version") == "promotion_rank_contract_v1"
                for item in rank_contracts
            )
            / len(rank_contracts),
            6,
        ),
        "rank_eligible_count": sum(bool(item.get("eligible")) for item in rank_contracts),
        "out_of_time_after": max(loaded.fit_end_date, loaded.calibration_end_date),
        "persistence_mode": "append_only" if persist else "dry_run",
    }
    if not persist:
        return {
            "id": None,
            "shadow_key": shadow_key,
            "prediction_run_id": run.id,
            "artifact_id": artifact.id,
            "target_board": loaded.target_board,
            "snapshot_context": run.snapshot_context,
            "reference_trade_date": run.reference_trade_date.isoformat(),
            "champion_model_version": run.model_version,
            "challenger_model_version": artifact.model_version,
            "candidate_count": len(rows),
            "payload_hash": payload_hash,
            "metadata": metadata,
            "predictions": canonical,
            "created": False,
            "persisted": False,
            "production_unchanged": True,
        }

    shadow_run = PromotionShadowRun(
        shadow_key=shadow_key,
        prediction_run_id=run.id,
        artifact_id=artifact.id,
        target_board=loaded.target_board,
        snapshot_context=run.snapshot_context,
        reference_trade_date=run.reference_trade_date,
        as_of_at=run.as_of_at,
        champion_model_version=run.model_version,
        challenger_model_version=artifact.model_version,
        feature_version=artifact.feature_version,
        data_version=artifact.data_version,
        status="completed",
        candidate_count=len(rows),
        payload_hash=payload_hash,
        metadata_json=_json(metadata),
        created_at=now,
        completed_at=now,
    )
    db.add(shadow_run)
    await db.flush()
    prediction_rows: list[PromotionShadowPrediction] = []
    for index, snapshot in enumerate(snapshots):
        row = PromotionShadowPrediction(
            shadow_run_id=shadow_run.id,
            prediction_snapshot_id=snapshot.id,
            code=snapshot.code,
            name=snapshot.name,
            target_board=snapshot.target_board,
            prediction_trade_date=snapshot.prediction_trade_date,
            candidate_route=snapshot.candidate_route,
            market_regime=regime,
            champion_probability=rows[index].baseline_probability,
            challenger_raw_probability=float(raw_probabilities[index]),
            challenger_probability=float(probabilities[index]),
            champion_rank_position=snapshot.rank_position,
            challenger_rank_position=challenger_rank[index],
            features_hash=_digest(feature_payloads[index]),
            metadata_json=_json(
                {
                    "rank_scope": snapshot.rank_scope,
                    "champion_pool_rank": snapshot.pool_rank,
                    "champion_recall_rank_position": snapshot.recall_rank_position,
                    "champion_actionable": bool(snapshot.actionable),
                    "rank_contract": rank_contracts[index],
                    "claim_boundary": "paired score only; no order was sent and production was unchanged",
                }
            ),
            created_at=now,
        )
        db.add(row)
        prediction_rows.append(row)
    await db.commit()
    return _shadow_run_payload(shadow_run, prediction_rows, created=True)


def _evaluation_payload(row: PromotionShadowEvaluation, *, created: bool | None = None) -> dict:
    payload = {
        "id": row.id,
        "evaluation_key": row.evaluation_key,
        "artifact_id": row.artifact_id,
        "trigger_shadow_run_id": row.trigger_shadow_run_id,
        "target_board": row.target_board,
        "snapshot_context": row.snapshot_context,
        "challenger_model_version": row.challenger_model_version,
        "label_version": row.label_version,
        "outcome_end_date": row.outcome_end_date.isoformat(),
        "evaluated_shadow_run_count": row.evaluated_shadow_run_count,
        "trade_day_count": row.trade_day_count,
        "sample_count": row.sample_count,
        "positive_count": row.positive_count,
        "decision": row.decision,
        "metrics": _loads(row.metrics_json, {}),
        "acceptance": _loads(row.acceptance_json, {}),
        "metadata": _loads(row.metadata_json, {}),
        "created_at": row.created_at.isoformat(timespec="seconds"),
        "production_unchanged": True,
    }
    if created is not None:
        payload["created"] = created
    return payload


def _rank_contract_check(trade_day: str, day_rows: list[dict]) -> dict:
    contracts = [item.get("rank_contract") or {} for item in day_rows]
    versions = {str(item.get("version") or "") for item in contracts}
    eligible_counts = {int(item.get("eligible_count") or 0) for item in contracts}
    formal_limits = {int(item.get("formal_limit") or 0) for item in contracts}
    recall_limits = {int(item.get("recall_limit") or 0) for item in contracts}
    eligible_rows = [item for item in day_rows if bool(item.get("rank_eligible"))]
    formal_positions = [
        int(item["champion_rank_position"])
        for item in day_rows
        if item.get("champion_rank_position") is not None
    ]
    recall_positions = [
        int(item["champion_recall_rank_position"])
        for item in day_rows
        if item.get("champion_recall_rank_position") is not None
    ]
    formal_limit = next(iter(formal_limits), 0)
    recall_limit = next(iter(recall_limits), 0)
    expected_eligible_count = next(iter(eligible_counts), -1)
    expected_formal = list(range(1, min(formal_limit, len(eligible_rows)) + 1))
    expected_recall = list(range(1, min(recall_limit, len(eligible_rows)) + 1))
    ranked_rows_are_eligible = all(
        bool(item.get("rank_eligible"))
        for item in day_rows
        if item.get("champion_rank_position") is not None
        or item.get("champion_recall_rank_position") is not None
    )
    formal_keys = {
        (item["code"], item["prediction_id"])
        for item in day_rows
        if item.get("champion_rank_position") is not None
    }
    recall_keys = {
        (item["code"], item["prediction_id"])
        for item in day_rows
        if item.get("champion_recall_rank_position") is not None
    }
    checks = {
        "version": versions == {"promotion_rank_contract_v1"},
        "complete": all(bool(item.get("complete")) for item in contracts),
        "eligible_count": len(eligible_counts) == 1
        and expected_eligible_count == len(eligible_rows),
        "limits": len(formal_limits) == 1
        and formal_limit > 0
        and len(recall_limits) == 1
        and recall_limit >= formal_limit,
        "formal_positions": sorted(formal_positions) == expected_formal,
        "recall_positions": sorted(recall_positions) == expected_recall,
        "ranked_rows_are_eligible": ranked_rows_are_eligible,
        "formal_is_recall_prefix": formal_keys.issubset(recall_keys),
    }
    return {
        "trade_date": trade_day,
        "passed": all(checks.values()),
        "checks": checks,
        "candidate_count": len(day_rows),
        "eligible_count": len(eligible_rows),
        "formal_limit": formal_limit,
        "recall_limit": recall_limit,
    }


def _daily_probability_rank_metrics(
    observations: list[dict],
    *,
    probability_key: str,
) -> dict[str, dict]:
    """Rank only the frozen production-eligible set; misses stay in the denominator."""

    groups: dict[str, list[dict]] = defaultdict(list)
    for item in observations:
        groups[item["prediction_trade_date"]].append(item)
    total_positives = sum(int(item["label"]) for item in observations)
    base_rate = total_positives / len(observations) if observations else 0.0
    result: dict[str, dict] = {}
    for k in (5, 12, 30):
        selected_count = 0
        hit_count = 0
        daily = []
        for trade_day in sorted(groups):
            day_rows = groups[trade_day]
            eligible = [item for item in day_rows if bool(item.get("rank_eligible"))]
            eligible.sort(
                key=lambda item: (
                    -float(item[probability_key]),
                    str(item["code"]),
                    int(item["prediction_id"]),
                )
            )
            selected = eligible[: min(k, len(eligible))]
            hits = sum(int(item["label"]) for item in selected)
            positives = sum(int(item["label"]) for item in day_rows)
            selected_count += len(selected)
            hit_count += hits
            daily.append(
                {
                    "trade_date": trade_day,
                    "candidate_count": len(day_rows),
                    "eligible_count": len(eligible),
                    "selected_count": len(selected),
                    "hit_count": hits,
                    "positive_count": positives,
                }
            )
        precision = hit_count / selected_count if selected_count else 0.0
        recall = hit_count / total_positives if total_positives else 0.0
        result[str(k)] = {
            "k": k,
            "trade_day_count": len(groups),
            "selected_count": selected_count,
            "hit_count": hit_count,
            "positive_count": total_positives,
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "lift": round(precision / base_rate, 6) if base_rate else 0.0,
            "daily": daily,
        }
    return result


def _official_champion_rank_metrics(observations: list[dict]) -> dict:
    """Replay only an explicit, complete frozen Champion rank contract."""

    groups: dict[str, list[dict]] = defaultdict(list)
    for item in observations:
        groups[item["prediction_trade_date"]].append(item)
    contract_checks = [
        _rank_contract_check(trade_day, groups[trade_day])
        for trade_day in sorted(groups)
    ]
    total_positives = sum(int(item["label"]) for item in observations)
    base_rate = total_positives / len(observations) if observations else 0.0
    result: dict[str, dict] = {}
    for k in (5, 12, 30):
        selected_count = 0
        hit_count = 0
        daily = []
        for trade_day in sorted(groups):
            day_rows = groups[trade_day]
            rank_key = (
                "champion_rank_position"
                if k <= 12
                else "champion_recall_rank_position"
            )
            selected = [
                item
                for item in day_rows
                if item.get(rank_key) is not None and int(item[rank_key]) <= k
            ]
            hits = sum(int(item["label"]) for item in selected)
            positives = sum(int(item["label"]) for item in day_rows)
            selected_count += len(selected)
            hit_count += hits
            daily.append(
                {
                    "trade_date": trade_day,
                    "candidate_count": len(day_rows),
                    "eligible_count": sum(
                        bool(item.get("rank_eligible")) for item in day_rows
                    ),
                    "selected_count": len(selected),
                    "hit_count": hits,
                    "positive_count": positives,
                }
            )
        precision = hit_count / selected_count if selected_count else 0.0
        recall = hit_count / total_positives if total_positives else 0.0
        result[str(k)] = {
            "k": k,
            "trade_day_count": len(groups),
            "selected_count": selected_count,
            "hit_count": hit_count,
            "positive_count": total_positives,
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "lift": round(precision / base_rate, 6) if base_rate else 0.0,
            "daily": daily,
        }
    return {
        "daily_rank": result,
        "metadata_coverage": round(
            sum(bool(item["passed"]) for item in contract_checks)
            / len(contract_checks),
            6,
        )
        if contract_checks
        else 0.0,
        "contract_checks": contract_checks,
    }


def _paired_day_bootstrap(
    observations: list[dict],
    *,
    iterations: int = 500,
) -> dict:
    """Deterministic paired bootstrap with whole trading days as resampling blocks."""

    groups: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(observations):
        groups[item["prediction_trade_date"]].append(index)
    trade_days = sorted(groups)
    if not trade_days:
        return {
            "method": "paired_trade_day_bootstrap",
            "iterations": 0,
            "trade_day_count": 0,
        }

    labels = np.asarray([item["label"] for item in observations], dtype=float)
    champion = np.asarray(
        [item["champion_probability"] for item in observations], dtype=float
    )
    challenger = np.asarray(
        [item["challenger_probability"] for item in observations], dtype=float
    )
    day_indexes = {
        trade_day: np.asarray(groups[trade_day], dtype=int)
        for trade_day in trade_days
    }
    daily_rank: dict[str, tuple[int, int, int, int]] = {}
    for trade_day in trade_days:
        indexes = day_indexes[trade_day]
        challenger_order = sorted(
            [
                index
                for index in indexes.tolist()
                if bool(observations[index].get("rank_eligible"))
            ],
            key=lambda index: (
                -float(challenger[index]),
                str(observations[index]["code"]),
                int(observations[index]["prediction_id"]),
            ),
        )
        challenger_selected = challenger_order[: min(12, len(challenger_order))]
        official_selected = [
            index
            for index in indexes
            if observations[index].get("champion_rank_position") is not None
            and int(observations[index]["champion_rank_position"]) <= 12
        ]
        daily_rank[trade_day] = (
            int(np.sum(labels[challenger_selected])),
            len(challenger_selected),
            int(np.sum(labels[official_selected])),
            len(official_selected),
        )

    seed_payload = [
        {
            "prediction_id": item["prediction_id"],
            "trade_date": item["prediction_trade_date"],
            "label": item["label"],
            "champion_probability": round(float(item["champion_probability"]), 12),
            "challenger_probability": round(float(item["challenger_probability"]), 12),
            "champion_rank_position": item.get("champion_rank_position"),
            "rank_eligible": bool(item.get("rank_eligible")),
        }
        for item in observations
    ]
    seed = int(_digest(seed_payload)[:16], 16) % (2**32)
    rng = np.random.default_rng(seed)
    ap_deltas: list[float] = []
    brier_improvements: list[float] = []
    top12_deltas: list[float] = []
    normalized_iterations = max(int(iterations), 100)
    for _ in range(normalized_iterations):
        sampled = rng.choice(trade_days, size=len(trade_days), replace=True).tolist()
        indexes = np.concatenate([day_indexes[trade_day] for trade_day in sampled])
        sample_labels = labels[indexes]
        sample_champion = champion[indexes]
        sample_challenger = challenger[indexes]
        ap_deltas.append(
            average_precision(sample_labels, sample_challenger)
            - average_precision(sample_labels, sample_champion)
        )
        brier_improvements.append(
            float(np.mean((sample_champion - sample_labels) ** 2))
            - float(np.mean((sample_challenger - sample_labels) ** 2))
        )
        challenger_hits = champion_hits = 0
        challenger_count = champion_count = 0
        for trade_day in sampled:
            challenger_day_hits, challenger_day_count, champion_day_hits, champion_day_count = (
                daily_rank[trade_day]
            )
            challenger_hits += challenger_day_hits
            challenger_count += challenger_day_count
            champion_hits += champion_day_hits
            champion_count += champion_day_count
        challenger_precision = (
            challenger_hits / challenger_count if challenger_count else 0.0
        )
        champion_precision = champion_hits / champion_count if champion_count else 0.0
        top12_deltas.append(challenger_precision - champion_precision)

    def interval(values: list[float]) -> dict:
        array = np.asarray(values, dtype=float)
        lower, median, upper = np.quantile(array, [0.05, 0.50, 0.95])
        return {
            "lower": round(float(lower), 6),
            "median": round(float(median), 6),
            "upper": round(float(upper), 6),
        }

    return {
        "method": "paired_trade_day_bootstrap",
        "confidence_level": 0.90,
        "iterations": normalized_iterations,
        "trade_day_count": len(trade_days),
        "seed": seed,
        "average_precision_delta": interval(ap_deltas),
        "brier_improvement": interval(brier_improvements),
        "top12_precision_delta": interval(top12_deltas),
        "positive_direction": "challenger_better",
    }


def _shadow_payload_matches(run, predictions) -> bool:
    """Rebuild only the original owned score-hash leaves, not live ORM objects."""
    canonical = []
    frozen_regime = _loads(run.metadata_json, {}).get("frozen_market_regime")
    if not isinstance(frozen_regime, str) or not frozen_regime:
        return False
    for item in sorted(predictions, key=lambda row: row.prediction_snapshot_id):
        probabilities = (item.champion_probability, item.challenger_raw_probability,
                         item.challenger_probability)
        if (item.target_board != run.target_board or item.prediction_trade_date != run.reference_trade_date
                or item.market_regime != frozen_regime
                or any(not isinstance(value, (float, int)) or isinstance(value, bool)
                       or not np.isfinite(value) or not 0 <= value <= 1 for value in probabilities)):
            return False
        rank = _loads(item.metadata_json, {}).get("rank_contract")
        if not isinstance(rank, dict):
            return False
        canonical.append({
            "prediction_snapshot_id": item.prediction_snapshot_id, "code": item.code,
            "champion_probability": round(item.champion_probability, 12),
            "challenger_raw_probability": round(item.challenger_raw_probability, 12),
            "challenger_probability": round(item.challenger_probability, 12),
            "challenger_rank_position": item.challenger_rank_position,
            "rank_eligible": bool(rank.get("eligible")),
            "rank_contract_version": str(rank.get("version") or ""),
            "features_hash": item.features_hash,
        })
    return _digest(canonical) == run.payload_hash


async def evaluate_shadow_artifact(
    db: AsyncSession,
    *,
    artifact_id: int,
    target_board: int | None = None,
    snapshot_context: str | None = None,
    persist: bool = True,
    minimum_kline_rows: int | None = None,
    now: datetime | None = None,
) -> dict:
    """Evaluate cumulative settled shadow observations on authoritative sessions."""

    if persist:
        await ensure_prediction_ledger_storage(db)
    artifact = await db.get(PromotionModelArtifact, int(artifact_id))
    if artifact is None:
        raise ValueError("model artifact not found")
    loaded = load_promotion_artifact(
        str(artifact.artifact_uri or ""),
        expected_model_version=artifact.model_version,
        expected_feature_version=FEATURE_VERSION,
        expected_target_board=target_board,
    )
    if (artifact.feature_version != loaded.feature_version or artifact.data_version != loaded.data_version
            or loaded.offline_acceptance.get("passed") is not True
            or loaded.offline_acceptance.get("decision") != "shadow_eligible"
            or loaded.training_config.get("dataset_source") != "prediction_snapshots"):
        raise ValueError("shadow evaluation requires the matching frozen-snapshot accepted artifact")
    target = int(target_board or loaded.target_board)
    statement = select(PromotionShadowRun).where(
        PromotionShadowRun.artifact_id == artifact.id,
        PromotionShadowRun.target_board == target,
        PromotionShadowRun.status == "completed",
    )
    normalized_context = str(snapshot_context or "").strip()
    if normalized_context:
        statement = statement.where(
            PromotionShadowRun.snapshot_context == normalized_context
        )
    runs = list(
        (
            await db.scalars(
                statement.order_by(
                    PromotionShadowRun.reference_trade_date,
                    PromotionShadowRun.as_of_at,
                    PromotionShadowRun.id,
                )
            )
        ).all()
    )
    if not runs:
        return {
            "status": "collecting",
            "decision": "collecting",
            "artifact_id": artifact.id,
            "target_board": target,
            "reason": "no completed shadow runs",
            "production_unchanged": True,
        }
    contexts = sorted({row.snapshot_context for row in runs})
    if not normalized_context:
        if len(contexts) != 1:
            raise ValueError("snapshot_context is required when shadow runs use multiple contexts")
        normalized_context = contexts[0]

    # Score only a shadow attached to the latest immutable *official* retry.
    # Looking only at shadow rows would allow an operator to omit a later
    # production retry and cherry-pick an earlier candidate set. Do NOT prefilter
    # official status or join a target lane: failed/empty/lane-missing latest
    # batches must block older evidence, just as in the immutable ledger builder.
    latest_by_date: dict[date, PromotionShadowRun] = {}
    for run in runs:
        previous = latest_by_date.get(run.reference_trade_date)
        if previous is None or (run.as_of_at, int(run.id)) > (
            previous.as_of_at,
            int(previous.id),
        ):
            latest_by_date[run.reference_trade_date] = run
    official_runs = list(
        (
            await db.scalars(
                select(PromotionPredictionRun)
                .where(
                    PromotionPredictionRun.snapshot_source == "schedule",
                    PromotionPredictionRun.snapshot_context == normalized_context,
                    PromotionPredictionRun.reference_trade_date.in_(latest_by_date),
                )
                .order_by(
                    PromotionPredictionRun.reference_trade_date,
                    PromotionPredictionRun.as_of_at,
                    PromotionPredictionRun.id,
                )
            )
        ).unique().all()
    )
    latest_official_by_date: dict[date, PromotionPredictionRun] = {}
    for official_run in official_runs:
        latest_official_by_date[official_run.reference_trade_date] = official_run
    retry_excluded = []
    selected_runs = []
    for trade_day in sorted(latest_by_date):
        shadow_run = latest_by_date[trade_day]
        official_run = latest_official_by_date.get(trade_day)
        if official_run is None or official_run.id != shadow_run.prediction_run_id:
            retry_excluded.append(
                {
                    "shadow_run_id": shadow_run.id,
                    "prediction_trade_date": trade_day,
                    "reason": "superseded_by_newer_official_run",
                    "shadow_prediction_run_id": shadow_run.prediction_run_id,
                    "latest_official_prediction_run_id": (
                        official_run.id if official_run is not None else None
                    ),
                }
            )
            continue
        run_metadata = _loads(shadow_run.metadata_json, {})
        if (run_metadata.get("artifact_sha256") != loaded.artifact_sha256
                or shadow_run.challenger_model_version != loaded.model_version
                or shadow_run.feature_version != loaded.feature_version
                or shadow_run.data_version != loaded.data_version
                or (loaded.training_config.get("snapshot_context")
                    and loaded.training_config["snapshot_context"] != shadow_run.snapshot_context)):
            raise ValueError("immutable shadow evidence does not match artifact bytes or scope; register a new artifact")
        selected_runs.append(shadow_run)
    if not selected_runs:
        return {
            "status": "collecting",
            "decision": "collecting",
            "artifact_id": artifact.id,
            "target_board": target,
            "snapshot_context": normalized_context,
            "reason": "latest official retries have no matching shadow evidence",
            "excluded_runs": retry_excluded,
            "production_unchanged": True,
        }
    evaluation_at = now or datetime.now()
    calendar = dict((await db.execute(select(
        TradeCalendarModel.trade_date, TradeCalendarModel.is_trade_day,
    ).where(
        TradeCalendarModel.trade_date >= min(run.reference_trade_date for run in selected_runs),
        TradeCalendarModel.trade_date <= evaluation_at.date(),
    ))).all())
    minimum_rows = max(
        int(
            settings.PROMOTION_SHADOW_MIN_KLINE_ROWS
            if minimum_kline_rows is None
            else minimum_kline_rows
        ),
        1,
    )
    minimum_completeness = min(
        max(float(settings.PROMOTION_SHADOW_MIN_KLINE_COMPLETENESS), 0.0),
        1.0,
    )
    # Reuse the new ledger builder's exact read-only audit contract. Import here
    # because that builder also reuses our completed-session clock predicate.
    from app.promotion.modeling.ledger_dataset import _quality_complete
    truth_audits = list((await db.scalars(select(DataQualityRun).where(
        DataQualityRun.trade_date >= min(run.reference_trade_date for run in selected_runs),
        DataQualityRun.trade_date <= evaluation_at.date(),
    ))).all())
    outcomes: dict[int, date] = {}
    timing_evidence = {}
    quality_evidence = {}
    excluded: list[dict] = list(retry_excluded)
    for run in selected_runs:
        outcome_date, calendar_reason = next_recorded_trade_day(
            run.reference_trade_date, calendar, through=evaluation_at.date(),
        )
        if outcome_date is None:
            excluded.append({"shadow_run_id": run.id, "reason": calendar_reason})
            continue
        if not _is_completed_outcome_date(outcome_date, now=now):
            excluded.append(
                {
                    "shadow_run_id": run.id,
                    "outcome_trade_date": outcome_date,
                    "reason": "outcome_session_not_closed",
                    "required_cutoff": "15:10",
                }
            )
            continue
        official = latest_official_by_date[run.reference_trade_date]
        timing = forward_shadow_clock_evidence(
            prediction_as_of=official.as_of_at, prediction_created=official.created_at,
            prediction_completed=official.completed_at, artifact_created=artifact.created_at,
            shadow_created=run.created_at, shadow_completed=run.completed_at,
            outcome_date=outcome_date, evaluation_at=evaluation_at,
        )
        if (run.as_of_at != official.as_of_at or run.reference_trade_date != official.reference_trade_date
                or run.snapshot_context != official.snapshot_context or official.gate_passed is not True
                or official.status != "completed"):
            timing = {**timing, "passed": False, "reason": "forward_source_identity_or_quality_unproven"}
        if not timing["passed"]:
            excluded.append({"shadow_run_id": run.id, "reason": "forward_timing_unverified",
                             "timing": timing})
            continue
        timing_evidence[run.id] = timing
        kline_count = int(
            await db.scalar(
                select(func.count()).select_from(StockKline).where(
                    StockKline.trade_date == outcome_date,
                    StockKline.source.in_(_FORMAL_CLOSE_SOURCES),
                )
            )
            or 0
        )
        recent_kline_counts = [
            int(row[1] or 0)
            for row in (
                await db.execute(
                    select(StockKline.trade_date, func.count())
                    .where(
                        StockKline.trade_date <= outcome_date,
                        StockKline.trade_date >= outcome_date - timedelta(days=60),
                        StockKline.source.in_(_FORMAL_CLOSE_SOURCES),
                    )
                    .group_by(StockKline.trade_date)
                )
            ).all()
        ]
        recent_kline_peak = max(recent_kline_counts, default=kline_count)
        required_kline_count = max(
            minimum_rows,
            int(np.ceil(recent_kline_peak * minimum_completeness)),
        )
        kline_completeness = (
            kline_count / recent_kline_peak if recent_kline_peak else 0.0
        )
        limit_count = int(
            await db.scalar(
                select(func.count()).select_from(LimitUpPool).where(
                    LimitUpPool.trade_date == outcome_date,
                    LimitUpPool.quarantined.is_(False),
                )
            )
            or 0
        )
        if kline_count < required_kline_count or limit_count <= 0:
            excluded.append(
                {
                    "shadow_run_id": run.id,
                    "outcome_trade_date": outcome_date,
                    "reason": "authoritative_outcome_quality_incomplete",
                    "kline_count": kline_count,
                    "recent_kline_peak": recent_kline_peak,
                    "kline_completeness": round(kline_completeness, 6),
                    "required_kline_count": required_kline_count,
                    "required_kline_completeness": minimum_completeness,
                    "limit_up_count": limit_count,
                }
            )
            continue
        prediction_counts = {}
        for name, model in (("stock_kline", StockKline), ("limit_up_pool", LimitUpPool)):
            filters = [model.trade_date == run.reference_trade_date]
            filters.append(model.source.in_(_FORMAL_CLOSE_SOURCES) if model is StockKline
                           else model.quarantined.is_(False))
            prediction_counts[name] = int(await db.scalar(select(func.count()).select_from(model).where(*filters)) or 0)
        outcome_counts = {"stock_kline": kline_count, "limit_up_pool": limit_count}
        if (prediction_counts["stock_kline"] < required_kline_count
                or not _quality_complete(truth_audits, run.reference_trade_date, official.as_of_at,
                                         prediction_counts, minimum_completeness)
                or not _quality_complete(truth_audits, outcome_date, evaluation_at,
                                         outcome_counts, minimum_completeness)):
            excluded.append({"shadow_run_id": run.id, "reason": "recorded_truth_audit_incomplete",
                             "outcome_trade_date": outcome_date})
            continue
        quality_evidence[run.id] = [
            {"id": audit.id, "day": audit.trade_date.isoformat(), "status": audit.status,
             "started_at": audit.started_at, "completed_at": audit.completed_at,
             "summary_hash": _digest(audit.summary_json)}
            for audit in truth_audits if audit.trade_date in {run.reference_trade_date, outcome_date}
            and audit.completed_at is not None
            and audit.completed_at <= (official.as_of_at if audit.trade_date == run.reference_trade_date else evaluation_at)
        ]
        outcomes[run.id] = outcome_date
    if not outcomes:
        return {
            "status": "collecting",
            "decision": "collecting",
            "artifact_id": artifact.id,
            "target_board": target,
            "snapshot_context": normalized_context,
            "reason": "no quality-complete next-session outcomes",
            "excluded_runs": excluded,
            "production_unchanged": True,
        }

    eligible_runs = [run for run in selected_runs if run.id in outcomes]
    run_ids = [run.id for run in eligible_runs]
    predictions = list(
        (
            await db.scalars(
                select(PromotionShadowPrediction)
                .where(PromotionShadowPrediction.shadow_run_id.in_(run_ids))
                .order_by(
                    PromotionShadowPrediction.prediction_trade_date,
                    PromotionShadowPrediction.code,
                )
            )
        ).all()
    )
    event_dates = set(outcomes.values()) | {
        run.reference_trade_date for run in eligible_runs
    }
    limit_events = {
        (str(code or "").strip(), trade_day)
        for code, trade_day in (
            await db.execute(
                select(LimitUpPool.code, LimitUpPool.trade_date).where(
                    LimitUpPool.trade_date.in_(event_dates),
                    LimitUpPool.quarantined.is_(False),
                )
            )
        ).all()
    }
    material = list((await db.scalars(select(StockKline).where(
        StockKline.trade_date.in_(event_dates),
        StockKline.code.in_({p.code for p in predictions}),
    ))).all())
    bar_by_identity = {(bar.code, bar.trade_date): bar for bar in material}
    quarantined = set((await db.execute(select(LimitUpPool.code, LimitUpPool.trade_date).where(
        LimitUpPool.trade_date.in_(event_dates), LimitUpPool.quarantined.is_(True),
    ))).all())
    predictions_by_run = defaultdict(list)
    for prediction in predictions:
        predictions_by_run[prediction.shadow_run_id].append(prediction)
    # One immutable identity read at the evaluation cutoff, shared by every run.
    # Only owned codes/clocks reach the pure gate; no Session/ORM reaches its worker.
    try:
        identity_index = await prepare_identity_index(known_cutoff=evaluation_at)
    except Exception:
        identity_index = None  # Read/parser failure cannot authorize research.
    identity_evidence = {}
    try:
        outcome_index = await prepare_outcome_index(known_cutoff=evaluation_at)
    except Exception:
        outcome_index = None
    material_evidence, sealed_rows = {}, {}
    bad_runs = set()
    for run in eligible_runs:
        paired = predictions_by_run[run.id]
        reasons = []
        if len(paired) != run.candidate_count or len({p.code for p in paired}) != len(paired):
            reasons.append("incomplete_paired_candidate_set")
        if not _shadow_payload_matches(run, paired):
            reasons.append("immutable_paired_payload_mismatch")
        for prediction in paired:
            if (not isinstance(prediction.created_at, datetime) or prediction.created_at.tzinfo is not None
                    or not run.created_at <= prediction.created_at <= run.completed_at):
                reasons.append("paired_score_receipt_clock_invalid")
            before = bar_by_identity.get((prediction.code, run.reference_trade_date))
            after = bar_by_identity.get((prediction.code, outcomes[run.id]))
            error = formal_outcome_bar_error(before, after)
            if (prediction.code, run.reference_trade_date) in quarantined or (prediction.code, outcomes[run.id]) in quarantined:
                error = "candidate_limit_event_quarantined"
            if error:
                reasons.append(error)
        try:
            if identity_index is None:
                raise ValueError("identity index unavailable")
            identity_gate = identity_pair_gate(
                identity_index, codes=tuple(p.code for p in paired),
                prediction_at=latest_official_by_date[run.reference_trade_date].as_of_at,
                outcome_day=outcomes[run.id], evaluation_as_of=evaluation_at,
            )
            if (not isinstance(identity_gate, dict) or identity_gate.get("profile") != IDENTITY_PROFILE
                    or not isinstance(identity_gate.get("evidence_hash"), str)
                    or len(identity_gate["evidence_hash"]) != 64
                    or any(c not in "0123456789abcdef" for c in identity_gate["evidence_hash"])):
                raise ValueError("identity gate contract invalid")
            if identity_gate.get("passed") is True:
                per_code = identity_gate.get("per_code")
                if (not isinstance(per_code, list) or len(per_code) != len(paired)
                        or any(not isinstance(item, dict) or item.get("status") != "verified"
                               or item.get("reasons") != [] for item in per_code)):
                    raise ValueError("identity paired denominator invalid")
                verified_codes = [item.get("code") for item in per_code]
                if (identity_gate.get("reasons") != []
                        or set(verified_codes) != {p.code for p in paired}):
                    raise ValueError("identity paired denominator invalid")
            # Query wall-clock diagnostics are not material changes; keep only
            # stable gate evidence in the evaluation key (source known clocks stay
            # bound by original receipt hashes). Metadata already records evaluation_at.
            identity_gate = {key: identity_gate.get(key) for key in
                             ("passed", "profile", "reasons", "per_code", "evidence_hash")}
        except Exception:
            identity_gate = {"passed": False, "profile": IDENTITY_PROFILE,
                "reasons": ["identity_gate_unavailable"], "per_code": [],
                "evidence_hash": _digest({"profile": IDENTITY_PROFILE, "unavailable": True,
                    "codes": [p.code for p in paired], "prediction_run_id": run.prediction_run_id,
                    "prediction_at": latest_official_by_date[run.reference_trade_date].as_of_at,
                    "outcome_day": outcomes[run.id]})}
        identity_evidence[run.id] = identity_gate
        if identity_gate.get("passed") is not True:
            reasons.append("candidate_identity_unverified")
        # Project read-time leaves before crossing to the worker; never pass ORM.
        read_time_rows = [{"code": p.code,
            "prediction_limit_up": (p.code, run.reference_trade_date) in limit_events,
            "outcome_limit_up": (p.code, outcomes[run.id]) in limit_events,
            "before_close": getattr(bar_by_identity.get((p.code, run.reference_trade_date)), "close", None),
            "after_close": getattr(bar_by_identity.get((p.code, outcomes[run.id])), "close", None),
            "after_prev_close": getattr(bar_by_identity.get((p.code, outcomes[run.id])), "prev_close", None)}
            for p in paired]
        material_gate = {"passed": False, "profile": MATERIAL_PROFILE,
            "reasons": ["outcome_material_gate_unavailable"], "per_code": [],
            "evidence_hash": _digest({"unavailable": True, "prediction_run_id": run.prediction_run_id,
                "prediction_day": run.reference_trade_date, "outcome_day": outcomes[run.id]})}
        try:
            if outcome_index is None:
                raise ValueError("outcome_material_index_unavailable")
            returned = await asyncio.to_thread(outcome_pair_gate, outcome_index,
                codes=tuple(p.code for p in paired), prediction_day=run.reference_trade_date,
                prediction_at=latest_official_by_date[run.reference_trade_date].as_of_at,
                outcome_day=outcomes[run.id], evaluation_as_of=evaluation_at)
            if type(returned) is not dict:
                raise ValueError("outcome_material_gate_contract_invalid")
            material_gate = {k: returned.get(k) for k in ("passed", "profile", "reasons", "per_code", "evidence_hash")}
            sealed_rows[run.id] = require_paired_material_rows(material_gate,
                profile=MATERIAL_PROFILE, read_time_rows=read_time_rows)
            material_gate["consumer_passed"] = True
        except Exception as exc:
            material_gate["consumer_passed"] = False
            material_gate["consumer_reason"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            reasons.append("sealed_outcome_material_unverified")
        material_gate["read_time_hash"] = _digest(read_time_rows)
        material_evidence[run.id] = material_gate
        if reasons:
            bad_runs.add(run.id)
            excluded.append({"shadow_run_id": run.id, "reason": "candidate_outcome_quality_unknown",
                             "details": sorted(set(reasons)), "identity_evidence": identity_gate,
                             "outcome_material_evidence": material_gate})
    # Never improve the paired denominator by dropping only suspended/unknown names.
    eligible_runs = [run for run in eligible_runs if run.id not in bad_runs]
    run_ids = [run.id for run in eligible_runs]
    outcomes = {key: day for key, day in outcomes.items() if key in run_ids}
    if not eligible_runs:
        return {"status": "collecting", "decision": "collecting", "artifact_id": artifact.id,
                "target_board": target, "snapshot_context": normalized_context,
                "reason": "no quality-complete paired candidate outcomes",
                "identity_profile": IDENTITY_PROFILE, "identity_coverage": 0.0,
                "outcome_material_profile": MATERIAL_PROFILE,
                "outcome_material_coverage": (sum(e.get("consumer_passed") is True for e in material_evidence.values())
                                              / len(material_evidence) if material_evidence else 0.0),
                "excluded_runs": excluded, "production_unchanged": True}
    run_by_id = {run.id: run for run in eligible_runs}
    observations: list[dict] = []
    for prediction in predictions:
        if prediction.shadow_run_id not in run_by_id:
            continue
        run = run_by_id[prediction.shadow_run_id]
        outcome_date = outcomes[run.id]
        sealed = sealed_rows[run.id][prediction.code]
        outcome_hit = sealed["outcome_limit_up"]
        prediction_day_hit = sealed["prediction_limit_up"]
        label = promotion_event_label(
            target_board=target,
            outcome_limit_up=outcome_hit,
            prediction_day_limit_up=prediction_day_hit,
        )
        prediction_metadata = _loads(prediction.metadata_json, {})
        observations.append(
            {
                "shadow_run_id": run.id,
                "prediction_id": prediction.id,
                "code": prediction.code,
                "prediction_trade_date": prediction.prediction_trade_date.isoformat(),
                "outcome_trade_date": outcome_date.isoformat(),
                "forward_timing": timing_evidence[run.id],
                "truth_quality_audits": quality_evidence[run.id],
                "identity_evidence_hash": identity_evidence[run.id]["evidence_hash"],
                "outcome_material_evidence_hash": material_evidence[run.id]["evidence_hash"],
                "outcome_material": {
                    "contract": OUTCOME_EVIDENCE_VERSION,
                    "before": {key: getattr(bar_by_identity[(prediction.code, run.reference_trade_date)], key)
                               for key in ("close", "volume", "source")},
                    "after": {key: getattr(bar_by_identity[(prediction.code, outcome_date)], key)
                              for key in ("close", "prev_close", "volume", "source")},
                    "prediction_day_limit_up": prediction_day_hit,
                    "outcome_limit_up": outcome_hit,
                    "quarantined": False,
                },
                "label": label,
                "champion_probability": prediction.champion_probability,
                "challenger_probability": prediction.challenger_probability,
                "champion_rank_position": prediction.champion_rank_position,
                "champion_recall_rank_position": prediction_metadata.get(
                    "champion_recall_rank_position"
                ),
                "rank_contract": prediction_metadata.get("rank_contract") or {},
                "rank_eligible": bool(
                    (prediction_metadata.get("rank_contract") or {}).get("eligible")
                ),
                "market_regime": prediction.market_regime or "unknown",
            }
        )
    if not observations:
        raise ValueError("settled shadow runs contain no paired predictions")

    labels = np.asarray([item["label"] for item in observations], dtype=float)
    champion = np.asarray(
        [item["champion_probability"] for item in observations], dtype=float
    )
    challenger = np.asarray(
        [item["challenger_probability"] for item in observations], dtype=float
    )
    trade_dates = [item["prediction_trade_date"] for item in observations]
    regimes = [item["market_regime"] for item in observations]
    official_champion_rank = _official_champion_rank_metrics(observations)
    challenger_metrics = classification_metrics(labels, challenger, trade_dates)
    champion_metrics = classification_metrics(labels, champion, trade_dates)
    challenger_metrics["daily_rank"] = _daily_probability_rank_metrics(
        observations,
        probability_key="challenger_probability",
    )
    champion_metrics["daily_rank"] = official_champion_rank["daily_rank"]
    metrics = {
        "completed_fold_count": len(eligible_runs),
        "skipped_fold_count": len(excluded),
        "validation_trade_day_count": len(set(trade_dates)),
        "challenger_metrics": challenger_metrics,
        "champion_metrics": champion_metrics,
        "official_champion_rank_metrics": official_champion_rank,
        "regime_metrics": regime_sliced_metrics(
            labels, challenger, champion, trade_dates, regimes
        ),
        "paired_bootstrap": _paired_day_bootstrap(observations),
    }
    acceptance = evaluate_challenger_acceptance(
        metrics,
        policy={
            **SHADOW_ACCEPTANCE_POLICY,
            "minimum_outcome_kline_rows": minimum_rows,
            "minimum_outcome_kline_completeness": minimum_completeness,
        },
    )
    acceptance_policy = acceptance.get("policy") or {}
    official_top12 = official_champion_rank["daily_rank"]["12"]
    official_top30 = official_champion_rank["daily_rank"]["30"]
    challenger_top12 = metrics["challenger_metrics"]["daily_rank"]["12"]
    challenger_top30 = metrics["challenger_metrics"]["daily_rank"]["30"]
    official_top12_delta = float(challenger_top12.get("precision") or 0.0) - float(
        official_top12.get("precision") or 0.0
    )
    official_top30_delta = float(challenger_top30.get("recall") or 0.0) - float(
        official_top30.get("recall") or 0.0
    )
    official_rank_checks = [
        {
            "name": "official_rank_metadata_coverage",
            "passed": official_champion_rank["metadata_coverage"] >= 1.0,
            "actual": official_champion_rank["metadata_coverage"],
            "required": 1.0,
            "detail": "必须能重放每个冻结 Champion 的正式榜与宽召回排名",
        },
        {
            "name": "official_top12_precision",
            "passed": official_top12_delta
            >= float(acceptance_policy.get("minimum_top12_precision_delta", 0.0)),
            "actual": round(official_top12_delta, 6),
            "required": float(
                acceptance_policy.get("minimum_top12_precision_delta", 0.0)
            ),
            "detail": "挑战者 Top12 精度不得低于冻结生产正式榜",
        },
        {
            "name": "official_top30_recall",
            "passed": official_top30_delta
            >= float(acceptance_policy.get("minimum_top30_recall_delta", -0.02)),
            "actual": round(official_top30_delta, 6),
            "required": float(
                acceptance_policy.get("minimum_top30_recall_delta", -0.02)
            ),
            "detail": "挑战者 Top30 召回不得明显低于冻结生产宽召回榜",
        },
    ]
    acceptance = {
        **acceptance,
        "checks": [*(acceptance.get("checks") or []), *official_rank_checks],
        "passed": bool(acceptance.get("passed"))
        and all(item["passed"] for item in official_rank_checks),
    }
    quality_verified_run_ids = []
    for run in eligible_runs:
        run_metadata = _loads(run.metadata_json, {})
        if bool((run_metadata.get("input_quality_gate") or {}).get("gate_passed")):
            quality_verified_run_ids.append(run.id)
    input_quality_coverage = len(quality_verified_run_ids) / len(eligible_runs)
    input_quality_check = {
        "name": "input_quality_coverage",
        "passed": input_quality_coverage >= 1.0,
        "actual": round(input_quality_coverage, 6),
        "required": 1.0,
        "detail": "进入人工晋级的每个影子交易日都必须有通过的数据质量闸门",
    }
    incomplete_outcome_run_ids = [
        int(item["shadow_run_id"])
        for item in excluded
        if item.get("reason") in {"authoritative_outcome_quality_incomplete",
                                  "candidate_outcome_quality_unknown", "outcome_calendar_gap",
                                  "prediction_calendar_unknown", "calendar_conflict",
                                  "recorded_truth_audit_incomplete"}
    ]
    outcome_quality_denominator = len(eligible_runs) + len(incomplete_outcome_run_ids)
    outcome_quality_coverage = (
        len(eligible_runs) / outcome_quality_denominator
        if outcome_quality_denominator
        else 0.0
    )
    outcome_quality_check = {
        "name": "outcome_quality_coverage",
        "passed": outcome_quality_coverage >= 1.0,
        "actual": round(outcome_quality_coverage, 6),
        "required": 1.0,
        "detail": "已到结果日的影子运行必须全部具备完整权威K线和涨停池",
    }
    retry_denominator = len(selected_runs) + len(retry_excluded)
    official_retry_coverage = (
        len(selected_runs) / retry_denominator if retry_denominator else 0.0
    )
    official_retry_check = {
        "name": "official_retry_coverage",
        "passed": official_retry_coverage >= 1.0,
        "actual": round(official_retry_coverage, 6),
        "required": 1.0,
        "detail": "每个日期必须影子评分最新正式重试，不能挑选较早候选集",
    }
    late_scored_run_ids = [int(item["shadow_run_id"]) for item in excluded
                           if item.get("reason") == "forward_timing_unverified"]
    forward_denominator = len(eligible_runs) + len(late_scored_run_ids)
    forward_timing_coverage = len(eligible_runs) / forward_denominator if forward_denominator else 0.0
    forward_timing_check = {
        "name": "forward_timing_coverage", "passed": forward_timing_coverage >= 1.0,
        "actual": round(forward_timing_coverage, 6), "required": 1.0,
        "detail": "候选与模型注册、整批评分必须在真实T+1集合竞价前完成，事后回放不能凑前向天数",
    }
    material_unverified_run_ids = [run_id for run_id, evidence in material_evidence.items()
                                   if evidence.get("consumer_passed") is not True]
    material_coverage = (sum(e.get("consumer_passed") is True for e in material_evidence.values())
                         / len(material_evidence) if material_evidence else 0.0)
    material_check = {"name": "outcome_material_coverage", "passed": material_coverage >= 1.0,
        "actual": round(material_coverage, 6), "required": 1.0,
        "detail": "每批正式结局须绑定当时可见不可变材料并与当前读取事实一致，不以运维摘要或删股代替"}
    identity_unverified_run_ids = [run_id for run_id, evidence in identity_evidence.items()
                                   if evidence.get("passed") is not True]
    identity_denominator = len(eligible_runs) + len(identity_unverified_run_ids)
    identity_coverage = len(eligible_runs) / identity_denominator if identity_denominator else 0.0
    identity_check = {
        "name": "identity_coverage", "passed": identity_coverage >= 1.0,
        "actual": round(identity_coverage, 6), "required": 1.0,
        "detail": "每个已到期候选整批必须具备预测时已知身份及T+1全交易会话身份，不能用当前ST倒推或挑股",
    }
    acceptance = {
        **acceptance,
        "checks": [
            *(acceptance.get("checks") or []),
            identity_check,
            material_check,
            input_quality_check,
            outcome_quality_check,
            official_retry_check,
            forward_timing_check,
        ],
        "passed": bool(acceptance.get("passed"))
        and input_quality_check["passed"]
        and outcome_quality_check["passed"]
        and official_retry_check["passed"]
        and forward_timing_check["passed"]
        and identity_check["passed"]
        and material_check["passed"],
    }
    evidence_checks = {
        "completed_folds",
        "validation_trade_days",
        "validation_positives",
        "known_regime_coverage",
        "evaluable_regimes",
        "official_rank_metadata_coverage",
        "input_quality_coverage",
        "outcome_quality_coverage",
        "paired_bootstrap_days",
        "official_retry_coverage",
        "forward_timing_coverage",
        "identity_coverage",
        "outcome_material_coverage",
    }
    failed_checks = {
        str(item.get("name"))
        for item in acceptance.get("checks", [])
        if not item.get("passed")
    }
    if acceptance.get("passed"):
        decision = "manual_review_eligible"
    elif failed_checks & evidence_checks:
        decision = "collecting"
    else:
        decision = "shadow_rejected"
    acceptance = {
        **acceptance,
        "decision": decision,
        "passed": decision == "manual_review_eligible",
        "note": (
            "影子证据通过，仅可进入人工审批；系统不会自动替换 Champion。"
            if decision == "manual_review_eligible"
            else "证据仍在积累，不能晋级。"
            if decision == "collecting"
            else "样本充足但性能/稳定性闸门失败，拒绝晋级。"
        ),
    }
    evaluation_key = _digest(
        {
            "artifact_id": artifact.id,
            "target_board": target,
            "snapshot_context": normalized_context,
            "label_version": SHADOW_LABEL_VERSION,
            "evaluation_version": SHADOW_EVALUATION_VERSION,
            "acceptance_policy": acceptance.get("policy") or {},
            "observations": observations,
            "excluded_runs": excluded,
            "outcome_contract": OUTCOME_EVIDENCE_VERSION,
            "identity_profile": IDENTITY_PROFILE,
            "outcome_material_profile": MATERIAL_PROFILE,
            "outcome_material_evidence": material_evidence,
            "identity_evidence_hashes": {str(run_id): evidence["evidence_hash"]
                                         for run_id, evidence in identity_evidence.items()},
        }
    )
    existing = await db.scalar(
        select(PromotionShadowEvaluation).where(
            PromotionShadowEvaluation.evaluation_key == evaluation_key
        )
    )
    if existing is not None:
        return _evaluation_payload(existing, created=False)

    outcome_end = max(outcomes.values())
    metadata = {
        "same_candidate_set": True,
        "latest_retry_per_prediction_date": True,
        "minimum_kline_rows": minimum_rows,
        "minimum_kline_completeness": minimum_completeness,
        "identity_profile": IDENTITY_PROFILE,
        "outcome_material_profile": MATERIAL_PROFILE,
        "outcome_material_coverage": round(material_coverage, 6),
        "outcome_material_unverified_run_ids": material_unverified_run_ids,
        "outcome_material_evidence": {str(run_id): evidence for run_id, evidence in material_evidence.items()},
        "sealed_label_source": True,
        "identity_coverage": round(identity_coverage, 6),
        "identity_unverified_run_ids": identity_unverified_run_ids,
        "identity_evidence": {str(run_id): evidence for run_id, evidence in identity_evidence.items()},
        "forward_timing_contract": FORWARD_SHADOW_VERSION,
        "forward_timing_coverage": round(forward_timing_coverage, 6),
        "late_or_unproven_scored_run_ids": late_scored_run_ids,
        "included_shadow_run_ids": run_ids,
        "input_quality_verified_run_ids": quality_verified_run_ids,
        "input_quality_coverage": round(input_quality_coverage, 6),
        "outcome_quality_coverage": round(outcome_quality_coverage, 6),
        "official_retry_coverage": round(official_retry_coverage, 6),
        "incomplete_outcome_run_ids": incomplete_outcome_run_ids,
        "superseded_shadow_runs": retry_excluded,
        "excluded_runs": excluded,
        "observation_digest": _digest(observations),
        "label_source": SHADOW_LABEL_VERSION,
        "outcome_contract": OUTCOME_EVIDENCE_VERSION,
        "outcome_material_observed_at": evaluation_at.isoformat(),
        "historical_outcome_availability_verified": False,
        "evaluation_version": SHADOW_EVALUATION_VERSION,
        "claim_boundary": "observed next-session outcome, not causal attribution",
    }
    if not persist:
        return {
            "id": None,
            "evaluation_key": evaluation_key,
            "artifact_id": artifact.id,
            "target_board": target,
            "snapshot_context": normalized_context,
            "challenger_model_version": artifact.model_version,
            "label_version": SHADOW_LABEL_VERSION,
            "outcome_end_date": outcome_end.isoformat(),
            "evaluated_shadow_run_count": len(eligible_runs),
            "trade_day_count": len(set(trade_dates)),
            "sample_count": len(observations),
            "positive_count": int(np.sum(labels)),
            "decision": decision,
            "metrics": metrics,
            "acceptance": acceptance,
            "metadata": metadata,
            "created": False,
            "persisted": False,
            "production_unchanged": True,
        }

    row = PromotionShadowEvaluation(
        evaluation_key=evaluation_key,
        artifact_id=artifact.id,
        trigger_shadow_run_id=eligible_runs[-1].id,
        target_board=target,
        snapshot_context=normalized_context,
        challenger_model_version=artifact.model_version,
        label_version=SHADOW_LABEL_VERSION,
        outcome_end_date=outcome_end,
        evaluated_shadow_run_count=len(eligible_runs),
        trade_day_count=len(set(trade_dates)),
        sample_count=len(observations),
        positive_count=int(np.sum(labels)),
        decision=decision,
        metrics_json=_json(metrics),
        acceptance_json=_json(acceptance),
        metadata_json=_json(metadata),
        created_at=datetime.now(),
    )
    db.add(row)
    await db.commit()
    return _evaluation_payload(row, created=True)


async def run_eligible_shadows_for_prediction_run(
    db: AsyncSession,
    *,
    prediction_run_id: int,
    quality_gate: dict | None = None,
) -> dict:
    """Automation entrypoint: shadow all compatible artifacts, never promote."""

    if not settings.PROMOTION_SHADOW_AUTOMATION_ENABLED:
        return {"status": "disabled", "results": [], "production_unchanged": True}
    artifacts = list(
        (
            await db.scalars(
                select(PromotionModelArtifact)
                .where(PromotionModelArtifact.status == "shadow_eligible")
                .order_by(PromotionModelArtifact.id)
            )
        ).all()
    )
    results: list[dict] = []
    for artifact in artifacts:
        try:
            inference = await run_shadow_inference(
                db,
                prediction_run_id=prediction_run_id,
                artifact_id=artifact.id,
                persist=True,
                quality_gate=quality_gate,
            )
            evaluation = await evaluate_shadow_artifact(
                db,
                artifact_id=artifact.id,
                target_board=inference["target_board"],
                snapshot_context=inference["snapshot_context"],
                persist=True,
            )
            results.append(
                {
                    "artifact_id": artifact.id,
                    "status": "completed",
                    "shadow_run": inference,
                    "evaluation": evaluation,
                }
            )
        except Exception as exc:
            # One malformed challenger must not starve other independent shadow
            # artifacts. Roll back only the current uncommitted attempt; completed
            # append-only runs were committed by their own operation.
            await db.rollback()
            results.append(
                {
                    "artifact_id": artifact.id,
                    "status": "skipped" if isinstance(exc, ValueError) else "failed_closed",
                    "reason": str(exc),
                    "error_type": exc.__class__.__name__,
                }
            )
    return {
        "status": "completed",
        "prediction_run_id": prediction_run_id,
        "artifact_count": len(artifacts),
        "results": results,
        "production_unchanged": True,
    }
