"""Append-only manual promotion/rollback and guarded production probability overlay."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime
from typing import Any

import numpy as np
from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.promotion import (
    PromotionDeploymentEvent,
    PromotionModelArtifact,
    PromotionShadowEvaluation,
    PromotionShadowRun,
)
from app.promotion.labels import PROMOTION_LABEL_VERSION
from app.promotion.ledger import ensure_prediction_ledger_storage
from app.promotion.modeling.acceptance import (
    DEFAULT_ACCEPTANCE_POLICY,
    SHADOW_ACCEPTANCE_POLICY,
)
from app.promotion.modeling.features import (
    FEATURE_VERSION,
    FeatureRow,
    extract_point_in_time_features,
)
from app.promotion.modeling.inference import load_promotion_artifact
from app.promotion.regime import REGIME_LABELS
from app.promotion.shadow import SHADOW_EVALUATION_VERSION, evaluate_shadow_artifact
from app.promotion.versioning import get_promotion_model_identity


APPROVE_CONFIRMATION_PHRASE = "APPROVE_CHAMPION"
ROLLBACK_CONFIRMATION_PHRASE = "ROLLBACK_CHAMPION"
_DEPLOYMENT_LOCKS = {1: asyncio.Lock(), 2: asyncio.Lock()}


def _operation_event_key(action: str, target_board: int, operation_id: str) -> tuple[str, str]:
    normalized = str(operation_id or "").strip()
    if len(normalized) < 8 or len(normalized) > 200:
        raise ValueError("operation_id must contain 8 to 200 characters")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:48]
    return f"{action}-{int(target_board)}-{digest}", normalized


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    )


def _loads(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _probability(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    if not np.isfinite(parsed):
        parsed = 0.0
    return float(np.clip(parsed, 1e-7, 1.0 - 1e-7))


def _event_payload(
    event: PromotionDeploymentEvent | None,
    artifact: PromotionModelArtifact | None,
) -> dict:
    legacy_version = get_promotion_model_identity().active_model_version
    if event is None:
        return {
            "active": False,
            "target_board": None,
            "artifact_id": None,
            "active_model_version": legacy_version,
            "deployment_mode": "legacy",
            "execution_enabled": bool(settings.PROMOTION_DEPLOYED_OVERLAY_ENABLED),
            "latest_event": None,
        }
    active = artifact is not None
    return {
        "active": active,
        "target_board": event.target_board,
        "artifact_id": artifact.id if artifact else None,
        "active_model_version": artifact.model_version if artifact else legacy_version,
        "deployment_mode": event.deployment_mode if active else "legacy",
        "execution_enabled": bool(settings.PROMOTION_DEPLOYED_OVERLAY_ENABLED),
        "latest_event": {
            "id": event.id,
            "event_key": event.event_key,
            "action": event.action,
            "artifact_id": event.artifact_id,
            "evidence_evaluation_id": event.evidence_evaluation_id,
            "from_model_version": event.from_model_version,
            "to_model_version": event.to_model_version,
            "operator": event.operator,
            "reason": event.reason,
            "metadata": _loads(event.metadata_json),
            "created_at": event.created_at.isoformat(timespec="seconds"),
        },
    }


async def _validate_deployment_evidence(
    db: AsyncSession,
    *,
    event: PromotionDeploymentEvent,
    artifact: PromotionModelArtifact,
    target_board: int,
):
    """Validate immutable bytes and the exact current shadow-policy evidence."""

    loaded = load_promotion_artifact(
        str(artifact.artifact_uri or ""),
        expected_model_version=artifact.model_version,
        expected_feature_version=FEATURE_VERSION,
        expected_target_board=target_board,
    )
    expected_sha256 = str(_loads(event.metadata_json).get("artifact_sha256") or "")
    if (
        artifact.feature_version != loaded.feature_version
        or artifact.data_version != loaded.data_version
        or not expected_sha256
        or expected_sha256 != loaded.artifact_sha256
    ):
        raise ValueError("registry/event hash does not match artifact bytes")
    if event.evidence_evaluation_id is None:
        raise ValueError("deployment event has no current shadow evidence")
    evidence = await db.get(
        PromotionShadowEvaluation,
        int(event.evidence_evaluation_id),
    )
    if (
        evidence is None
        or int(evidence.artifact_id) != int(artifact.id)
        or int(evidence.target_board) != int(target_board)
        or evidence.label_version != PROMOTION_LABEL_VERSION
        or evidence.decision != "manual_review_eligible"
    ):
        raise ValueError("deployment shadow evidence is missing or obsolete")
    evidence_acceptance = _loads(evidence.acceptance_json)
    evidence_metadata = _loads(evidence.metadata_json)
    required_policy = {
        **DEFAULT_ACCEPTANCE_POLICY,
        **SHADOW_ACCEPTANCE_POLICY,
        "minimum_outcome_kline_rows": max(
            int(settings.PROMOTION_SHADOW_MIN_KLINE_ROWS), 1
        ),
        "minimum_outcome_kline_completeness": min(
            max(float(settings.PROMOTION_SHADOW_MIN_KLINE_COMPLETENESS), 0.0),
            1.0,
        ),
    }
    if (
        not bool(evidence_acceptance.get("passed"))
        or evidence_acceptance.get("policy") != required_policy
        or evidence_metadata.get("evaluation_version")
        != SHADOW_EVALUATION_VERSION
    ):
        raise ValueError("deployment shadow policy evidence is obsolete")
    return loaded


async def current_deployment(
    db: AsyncSession,
    *,
    target_board: int,
) -> tuple[dict, PromotionModelArtifact | None, PromotionDeploymentEvent | None]:
    await ensure_prediction_ledger_storage(db)
    target = int(target_board)
    if target not in {1, 2}:
        raise ValueError("target_board must be 1 or 2")
    event = await db.scalar(
        select(PromotionDeploymentEvent)
        .where(PromotionDeploymentEvent.target_board == target)
        .order_by(desc(PromotionDeploymentEvent.id))
        .limit(1)
    )
    artifact = (
        await db.get(PromotionModelArtifact, int(event.artifact_id))
        if event is not None and event.artifact_id is not None
        else None
    )
    payload = _event_payload(event, artifact)
    payload["target_board"] = target
    integrity_error = None
    if event is not None and event.artifact_id is not None and artifact is None:
        integrity_error = "deployment artifact is missing; legacy fallback is active"
    elif event is not None and artifact is not None:
        try:
            await _validate_deployment_evidence(
                db,
                event=event,
                artifact=artifact,
                target_board=target,
            )
        except Exception as exc:
            integrity_error = f"deployment artifact validation failed: {exc}"
    payload["integrity_ok"] = integrity_error is None
    payload["effective_active"] = bool(payload.get("active")) and integrity_error is None
    if integrity_error:
        payload["integrity_error"] = integrity_error
        payload["effective_model_version"] = get_promotion_model_identity().active_model_version
    else:
        payload["effective_model_version"] = payload["active_model_version"]
    return payload, artifact, event


async def list_deployment_state(db: AsyncSession) -> dict:
    lanes = []
    for target in (1, 2):
        payload, _artifact, _event = await current_deployment(db, target_board=target)
        lanes.append(payload)
    events = list(
        (
            await db.scalars(
                select(PromotionDeploymentEvent)
                .order_by(desc(PromotionDeploymentEvent.id))
                .limit(100)
            )
        ).all()
    )
    return {
        "lanes": lanes,
        "events": [
            {
                "id": row.id,
                "event_key": row.event_key,
                "target_board": row.target_board,
                "action": row.action,
                "deployment_mode": row.deployment_mode,
                "artifact_id": row.artifact_id,
                "evidence_evaluation_id": row.evidence_evaluation_id,
                "from_model_version": row.from_model_version,
                "to_model_version": row.to_model_version,
                "operator": row.operator,
                "reason": row.reason,
                "metadata": _loads(row.metadata_json),
                "created_at": row.created_at.isoformat(timespec="seconds"),
            }
            for row in events
        ],
        "automatic_promotion": False,
    }


def _validate_manual_fields(operator: str, reason: str) -> tuple[str, str]:
    normalized_operator = str(operator or "").strip()
    normalized_reason = str(reason or "").strip()
    if len(normalized_operator) < 2:
        raise ValueError("operator must contain at least 2 characters")
    if len(normalized_reason) < 10:
        raise ValueError("reason must contain at least 10 characters")
    return normalized_operator[:80], normalized_reason


async def approve_challenger(
    db: AsyncSession,
    *,
    artifact_id: int,
    target_board: int,
    snapshot_context: str,
    operator: str,
    reason: str,
    confirmation_phrase: str,
    operation_id: str,
    expected_current_event_id: int | None,
) -> dict:
    """Serialize one lane so idempotency and compare-and-set remain authoritative."""

    target = int(target_board)
    if target not in _DEPLOYMENT_LOCKS:
        raise ValueError("target_board must be 1 or 2")
    async with _DEPLOYMENT_LOCKS[target]:
        return await _approve_challenger_locked(
            db,
            artifact_id=artifact_id,
            target_board=target,
            snapshot_context=snapshot_context,
            operator=operator,
            reason=reason,
            confirmation_phrase=confirmation_phrase,
            operation_id=operation_id,
            expected_current_event_id=expected_current_event_id,
        )


async def _approve_challenger_locked(
    db: AsyncSession,
    *,
    artifact_id: int,
    target_board: int,
    snapshot_context: str,
    operator: str,
    reason: str,
    confirmation_phrase: str,
    operation_id: str,
    expected_current_event_id: int | None,
) -> dict:
    """Append an approval only after the latest cumulative shadow gate passes."""

    if str(confirmation_phrase or "").strip() != APPROVE_CONFIRMATION_PHRASE:
        raise ValueError(f"confirmation_phrase must be {APPROVE_CONFIRMATION_PHRASE}")
    operator, reason = _validate_manual_fields(operator, reason)
    target = int(target_board)
    event_key, normalized_operation_id = _operation_event_key(
        "approve", target, operation_id
    )
    existing_event = await db.scalar(
        select(PromotionDeploymentEvent).where(
            PromotionDeploymentEvent.event_key == event_key
        )
    )
    if existing_event is not None:
        deployment, _artifact, _event = await current_deployment(
            db, target_board=target
        )
        return {
            "status": "approved",
            "deployment": deployment,
            "production_changed": bool(settings.PROMOTION_DEPLOYED_OVERLAY_ENABLED),
            "idempotent_replay": True,
            "operation_event_id": existing_event.id,
        }
    current, current_artifact, current_event = await current_deployment(
        db, target_board=target
    )
    actual_current_event_id = current_event.id if current_event is not None else None
    if actual_current_event_id != expected_current_event_id:
        raise ValueError(
            "deployment compare-and-set failed: "
            f"expected current event {expected_current_event_id}, "
            f"actual {actual_current_event_id}"
        )
    artifact = await db.get(PromotionModelArtifact, int(artifact_id))
    if artifact is None:
        raise ValueError("model artifact not found")
    if artifact.status not in {"shadow_eligible", "approved", "active"}:
        raise ValueError("artifact has not passed frozen-snapshot offline acceptance")
    loaded = load_promotion_artifact(
        str(artifact.artifact_uri or ""),
        expected_model_version=artifact.model_version,
        expected_feature_version=FEATURE_VERSION,
        expected_target_board=target,
    )
    if (
        artifact.feature_version != loaded.feature_version
        or artifact.data_version != loaded.data_version
    ):
        raise ValueError("artifact registry versions do not match the immutable JSON payload")
    if not bool(loaded.offline_acceptance.get("passed")) or str(
        loaded.offline_acceptance.get("decision") or ""
    ) != "shadow_eligible":
        raise ValueError("artifact JSON has no passed offline shadow-eligibility evidence")
    if str(loaded.training_config.get("dataset_source") or "") != "prediction_snapshots":
        raise ValueError("historical pretraining artifacts can never be manually promoted")

    evaluation_payload = await evaluate_shadow_artifact(
        db,
        artifact_id=artifact.id,
        target_board=target,
        snapshot_context=str(snapshot_context or "").strip(),
        persist=True,
    )
    if evaluation_payload.get("decision") != "manual_review_eligible" or not (
        evaluation_payload.get("acceptance") or {}
    ).get("passed"):
        raise ValueError(
            "latest cumulative shadow evidence is not eligible for manual promotion"
        )
    evaluation = await db.get(
        PromotionShadowEvaluation, int(evaluation_payload["id"])
    )
    if evaluation is None:
        raise ValueError("shadow evaluation evidence is missing")
    evaluation_metadata = _loads(evaluation.metadata_json)
    included_run_ids = [
        int(value)
        for value in evaluation_metadata.get("included_shadow_run_ids") or []
        if str(value).isdigit()
    ]
    evidence_runs = list(
        (
            await db.scalars(
                select(PromotionShadowRun).where(
                    PromotionShadowRun.id.in_(included_run_ids)
                )
            )
        ).all()
    ) if included_run_ids else []
    evidence_hashes = {
        str((_loads(row.metadata_json).get("artifact_sha256") or ""))
        for row in evidence_runs
    }
    if (
        len(evidence_runs) != len(set(included_run_ids))
        or not evidence_hashes
        or evidence_hashes != {loaded.artifact_sha256}
    ):
        raise ValueError(
            "current artifact bytes do not match every shadow run used as approval evidence"
        )

    if (
        bool(current.get("effective_active"))
        and current_artifact is not None
        and current_artifact.id == artifact.id
    ):
        raise ValueError("this artifact is already the active manual deployment")
    effective_current_artifact = (
        current_artifact if bool(current.get("effective_active")) else None
    )
    event = PromotionDeploymentEvent(
        event_key=event_key,
        target_board=target,
        action="approve",
        deployment_mode="probability_overlay",
        artifact_id=artifact.id,
        evidence_evaluation_id=evaluation.id,
        from_model_version=str(current["effective_model_version"]),
        to_model_version=artifact.model_version,
        operator=operator,
        reason=reason,
        confirmation_phrase=APPROVE_CONFIRMATION_PHRASE,
        metadata_json=_json(
            {
                "operation_id": normalized_operation_id,
                "previous_event_id": (
                    current_event.id if current_event is not None else None
                ),
                "from_artifact_id": (
                    effective_current_artifact.id
                    if effective_current_artifact is not None
                    else None
                ),
                "shadow_evaluation_key": evaluation.evaluation_key,
                "artifact_sha256": loaded.artifact_sha256,
                "snapshot_context": evaluation.snapshot_context,
                "execution_enabled": bool(settings.PROMOTION_DEPLOYED_OVERLAY_ENABLED),
                "automatic": False,
                "rollback_available": True,
            }
        ),
        created_at=datetime.now(),
    )
    db.add(event)
    await db.commit()
    payload, _artifact, _event = await current_deployment(db, target_board=target)
    return {
        "status": "approved",
        "deployment": payload,
        "production_changed": bool(settings.PROMOTION_DEPLOYED_OVERLAY_ENABLED),
        "idempotent_replay": False,
        "operation_event_id": event.id,
        "warning": "审批只覆盖概率排序，不绕过候选召回或交易执行闸门。",
    }


async def rollback_challenger(
    db: AsyncSession,
    *,
    target_board: int,
    operator: str,
    reason: str,
    confirmation_phrase: str,
    operation_id: str,
    expected_current_event_id: int | None,
) -> dict:
    target = int(target_board)
    if target not in _DEPLOYMENT_LOCKS:
        raise ValueError("target_board must be 1 or 2")
    async with _DEPLOYMENT_LOCKS[target]:
        return await _rollback_challenger_locked(
            db,
            target_board=target,
            operator=operator,
            reason=reason,
            confirmation_phrase=confirmation_phrase,
            operation_id=operation_id,
            expected_current_event_id=expected_current_event_id,
        )


async def _rollback_challenger_locked(
    db: AsyncSession,
    *,
    target_board: int,
    operator: str,
    reason: str,
    confirmation_phrase: str,
    operation_id: str,
    expected_current_event_id: int | None,
) -> dict:
    if str(confirmation_phrase or "").strip() != ROLLBACK_CONFIRMATION_PHRASE:
        raise ValueError(f"confirmation_phrase must be {ROLLBACK_CONFIRMATION_PHRASE}")
    operator, reason = _validate_manual_fields(operator, reason)
    target = int(target_board)
    event_key, normalized_operation_id = _operation_event_key(
        "rollback", target, operation_id
    )
    existing_event = await db.scalar(
        select(PromotionDeploymentEvent).where(
            PromotionDeploymentEvent.event_key == event_key
        )
    )
    if existing_event is not None:
        deployment, _artifact, _event = await current_deployment(
            db, target_board=target
        )
        return {
            "status": "rolled_back",
            "deployment": deployment,
            "production_changed": bool(settings.PROMOTION_DEPLOYED_OVERLAY_ENABLED),
            "idempotent_replay": True,
            "operation_event_id": existing_event.id,
        }
    current, current_artifact, current_event = await current_deployment(
        db, target_board=target
    )
    actual_current_event_id = current_event.id if current_event is not None else None
    if actual_current_event_id != expected_current_event_id:
        raise ValueError(
            "deployment compare-and-set failed: "
            f"expected current event {expected_current_event_id}, "
            f"actual {actual_current_event_id}"
        )
    if current_artifact is None or current_event is None:
        raise ValueError("target lane is already on the legacy champion")
    current_metadata = _loads(current_event.metadata_json)
    previous_artifact_id = current_metadata.get("from_artifact_id")
    previous_event_id = current_metadata.get("previous_event_id")
    previous_artifact = (
        await db.get(PromotionModelArtifact, int(previous_artifact_id))
        if previous_artifact_id
        else None
    )
    previous_state_event = (
        await db.get(PromotionDeploymentEvent, int(previous_event_id))
        if previous_event_id
        else None
    )
    if previous_artifact is not None and previous_state_event is None:
        # Backward-compatible recovery for events created before explicit chaining.
        previous_state_event = await db.scalar(
            select(PromotionDeploymentEvent)
            .where(
                PromotionDeploymentEvent.target_board == target,
                PromotionDeploymentEvent.artifact_id == previous_artifact.id,
                PromotionDeploymentEvent.id < current_event.id,
            )
            .order_by(desc(PromotionDeploymentEvent.id))
            .limit(1)
        )
    legacy_version = get_promotion_model_identity().active_model_version
    restore_validation_error = None
    if previous_artifact is not None:
        if (
            previous_state_event is None
            or previous_state_event.artifact_id != previous_artifact.id
        ):
            restore_validation_error = "previous artifact has no exact predecessor evidence"
        else:
            try:
                await _validate_deployment_evidence(
                    db,
                    event=previous_state_event,
                    artifact=previous_artifact,
                    target_board=target,
                )
            except Exception as exc:
                restore_validation_error = str(exc)
        if restore_validation_error:
            # A rollback must never reactivate stale-policy or tampered bytes.
            # Falling all the way back to the governed Legacy Champion is safer.
            previous_artifact = None
            previous_state_event = None
    previous_state_metadata = (
        _loads(previous_state_event.metadata_json)
        if previous_state_event is not None
        else {}
    )
    to_model_version = (
        previous_artifact.model_version if previous_artifact is not None else legacy_version
    )
    restored_sha256 = (
        str(previous_state_metadata.get("artifact_sha256") or "")
        if previous_state_event is not None
        else ""
    )
    event = PromotionDeploymentEvent(
        event_key=event_key,
        target_board=target,
        action="rollback",
        deployment_mode="probability_overlay" if previous_artifact else "legacy",
        artifact_id=previous_artifact.id if previous_artifact else None,
        evidence_evaluation_id=(
            previous_state_event.evidence_evaluation_id
            if previous_state_event is not None
            else None
        ),
        from_model_version=current_artifact.model_version,
        to_model_version=to_model_version,
        operator=operator,
        reason=reason,
        confirmation_phrase=ROLLBACK_CONFIRMATION_PHRASE,
        metadata_json=_json(
            {
                "operation_id": normalized_operation_id,
                "rolled_back_event_id": current_event.id,
                "restored_state_event_id": (
                    previous_state_event.id if previous_state_event is not None else None
                ),
                "previous_event_id": previous_state_metadata.get("previous_event_id"),
                "from_artifact_id": previous_state_metadata.get("from_artifact_id"),
                "restored_artifact_id": previous_artifact.id if previous_artifact else None,
                "restored_evidence_evaluation_id": (
                    previous_state_event.evidence_evaluation_id
                    if previous_state_event is not None
                    else None
                ),
                "artifact_sha256": restored_sha256 or None,
                "restore_validation_error": restore_validation_error,
                "automatic": False,
            }
        ),
        created_at=datetime.now(),
    )
    db.add(event)
    await db.commit()
    payload, _artifact, _event = await current_deployment(db, target_board=target)
    return {
        "status": "rolled_back",
        "deployment": payload,
        "production_changed": bool(settings.PROMOTION_DEPLOYED_OVERLAY_ENABLED),
        "idempotent_replay": False,
        "operation_event_id": event.id,
    }


def _market_regime_for_overlay(
    *,
    trade_date: date,
    candidates: list[dict],
) -> tuple[str, dict]:
    """Require the exact regime snapshot frozen into every production candidate."""

    evidence = []
    for item in candidates:
        factors = dict(item.get("probability_factors") or {})
        regime = str(factors.get("market_regime") or "").strip()
        snapshot_id = factors.get("market_regime_snapshot_id")
        regime_version = str(factors.get("market_regime_version") or "").strip()
        data_version = str(factors.get("market_regime_data_version") or "").strip()
        as_of_value = str(factors.get("market_regime_as_of_at") or "").strip()
        try:
            as_of_at = datetime.fromisoformat(as_of_value)
        except ValueError:
            as_of_at = None
        if (
            regime not in REGIME_LABELS
            or snapshot_id is None
            or not regime_version
            or not data_version
            or as_of_at is None
            or as_of_at.date() > trade_date
        ):
            return "unknown", {
                "valid": False,
                "reason": "missing_or_future_frozen_regime",
                "candidate_code": str(item.get("code") or ""),
            }
        evidence.append(
            (
                regime,
                str(snapshot_id),
                as_of_at.isoformat(timespec="seconds"),
                regime_version,
                data_version,
            )
        )
    unique = set(evidence)
    if len(unique) != 1:
        return "unknown", {
            "valid": False,
            "reason": "inconsistent_frozen_regime_evidence",
            "evidence_count": len(unique),
        }
    regime, snapshot_id, as_of_at, regime_version, data_version = evidence[0]
    return regime, {
        "valid": True,
        "source": "frozen_candidate_features",
        "snapshot_id": snapshot_id,
        "as_of_at": as_of_at,
        "regime_version": regime_version,
        "data_version": data_version,
    }


async def apply_active_promotion_overlay(
    db: AsyncSession,
    candidates: list[dict],
    *,
    target_board: int,
    trade_date: date,
    snapshot_context: str,
) -> tuple[list[dict], dict]:
    """Apply the latest manually approved probability overlay or fail to legacy."""

    target = int(target_board)
    deployment, artifact, event = await current_deployment(db, target_board=target)
    if not candidates:
        return candidates, {**deployment, "applied": False, "reason": "empty_candidate_set"}
    if not settings.PROMOTION_DEPLOYED_OVERLAY_ENABLED:
        return candidates, {**deployment, "applied": False, "reason": "execution_kill_switch"}
    if deployment.get("active") and not deployment.get("effective_active"):
        return candidates, {
            **deployment,
            "applied": False,
            "reason": "deployment_integrity_failed_closed_to_legacy",
        }
    if artifact is None or event is None:
        return candidates, {**deployment, "applied": False, "reason": "no_manual_approval"}
    try:
        loaded = load_promotion_artifact(
            str(artifact.artifact_uri or ""),
            expected_model_version=artifact.model_version,
            expected_feature_version=FEATURE_VERSION,
            expected_target_board=target,
        )
        if (
            artifact.feature_version != loaded.feature_version
            or artifact.data_version != loaded.data_version
        ):
            raise ValueError("artifact registry versions do not match the immutable JSON payload")
        approved_sha256 = str(_loads(event.metadata_json).get("artifact_sha256") or "")
        if not approved_sha256 or approved_sha256 != loaded.artifact_sha256:
            raise ValueError("deployed artifact bytes differ from the manually approved evidence")
        configured_context = str(
            loaded.training_config.get("snapshot_context") or ""
        ).strip()
        if configured_context and configured_context != str(snapshot_context or "").strip():
            return candidates, {
                **deployment,
                "applied": False,
                "reason": "snapshot_context_mismatch",
                "required_snapshot_context": configured_context,
            }
        if trade_date <= date.fromisoformat(loaded.calibration_end_date):
            raise ValueError("deployment artifact is not out-of-time for this trade date")
        regime, regime_evidence = _market_regime_for_overlay(
            trade_date=trade_date,
            candidates=candidates,
        )
        if regime == "unknown":
            return candidates, {
                **deployment,
                "applied": False,
                "reason": "regime_snapshot_unavailable_fail_closed",
                "regime_evidence": regime_evidence,
            }
        rows = [
            FeatureRow(
                code=str(item.get("code") or "").strip(),
                trade_date=trade_date.isoformat(),
                target_board=target,
                label=0,
                baseline_probability=_probability(item.get("probability")),
                candidate_route=str(item.get("candidate_route") or ""),
                values=extract_point_in_time_features(
                    dict(item.get("probability_factors") or {})
                ),
                market_regime=regime,
            )
            for item in candidates
        ]
        raw, calibrated = loaded.predict(rows)
        updated: list[dict] = []
        for index, item in enumerate(candidates):
            baseline = rows[index].baseline_probability
            probability = float(calibrated[index])
            factors = dict(item.get("probability_factors") or {})
            factors.update(
                {
                    "prediction_model_version": artifact.model_version,
                    "prediction_feature_version": artifact.feature_version,
                    "prediction_data_version": artifact.data_version,
                    "prediction_calibration_version": "platt_out_of_time_tail",
                    "prediction_runtime_mode": "deployed_probability_overlay",
                    "deployment_event_id": event.id,
                    "deployment_evaluation_id": event.evidence_evaluation_id,
                    "legacy_champion_probability": baseline,
                    "challenger_raw_probability": float(raw[index]),
                    "market_regime": regime,
                }
            )
            sub_probabilities = dict(item.get("sub_probabilities") or {})
            if target == 1:
                sub_probabilities["limit_up"] = probability
            copied = {
                **item,
                "legacy_champion_probability": baseline,
                "raw_probability": float(raw[index]),
                "probability": probability,
                "limit_up_probability": probability,
                "model_adjustment": probability - baseline,
                "probability_source": "manually_approved_challenger_overlay",
                "production_model_version": artifact.model_version,
                "probability_factors": factors,
                "sub_probabilities": sub_probabilities,
            }
            updated.append(copied)
        return updated, {
            **deployment,
            "applied": True,
            "candidate_count": len(updated),
            "market_regime": regime,
            "regime_evidence": regime_evidence,
            "claim_boundary": "probability/ranking overlay only; recall and trade gates remain independent",
        }
    except Exception as exc:
        logger.error(
            "人工晋级概率覆盖层校验失败，已回退 Legacy Champion: "
            f"target={target}, artifact={artifact.id}, error={exc}"
        )
        return candidates, {
            **deployment,
            "applied": False,
            "reason": "artifact_validation_failed_closed_to_legacy",
            "error": str(exc),
        }
