"""Append-only promotion prediction ledger.

The legacy ``promotion_prediction_record`` table remains the compatibility view
used by current review code.  Every official schedule execution is additionally
written here without replacing earlier runs, so experiments and audits can
reconstruct exactly what the model knew and emitted at each cutoff.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.promotion import (
    PromotionDeploymentEvent,
    PromotionExperiment,
    PromotionModelArtifact,
    PromotionPredictionRun,
    PromotionPredictionSnapshot,
    PromotionShadowEvaluation,
    PromotionShadowPrediction,
    PromotionShadowRun,
    PromotionTrainingRun,
)
from app.promotion.route_contract import route_identity
from app.promotion.snapshot_identity import normalize_snapshot_source
from app.promotion.versioning import (
    PromotionModelIdentity, PromotionRuntimeMode, project_promotion_probability,
)


@dataclass(frozen=True, slots=True)
class LedgerAppendResult:
    run_id: int
    run_key: str
    snapshot_count: int
    created: bool

    def as_payload(self) -> dict:
        return asdict(self)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _effective_run_identity(
    prepared: list[tuple[Mapping[str, Any], date, dict]],
    identity: PromotionModelIdentity,
) -> tuple[PromotionModelIdentity, dict]:
    """Describe manually deployed per-lane overlays without hiding mixed versions."""

    components: dict[str, dict[str, set[str]]] = {}
    deployed = False
    for item, _prediction_date, factors in prepared:
        target = str(_int(item.get("target_board"), 1))
        lane = components.setdefault(
            target,
            {"model_versions": set(), "feature_versions": set(), "data_versions": set()},
        )
        lane["model_versions"].add(
            str(factors.get("prediction_model_version") or identity.active_model_version)
        )
        lane["feature_versions"].add(
            str(factors.get("prediction_feature_version") or identity.feature_version)
        )
        lane["data_versions"].add(
            str(factors.get("prediction_data_version") or identity.data_version)
        )
        deployed = deployed or str(factors.get("prediction_runtime_mode") or "").startswith(
            "deployed_"
        )
    public_components = {
        target: {key: sorted(values) for key, values in lane.items()}
        for target, lane in sorted(components.items())
    }
    if not deployed:
        return identity, public_components

    model_versions = sorted(
        {value for lane in components.values() for value in lane["model_versions"]}
    )
    feature_versions = sorted(
        {value for lane in components.values() for value in lane["feature_versions"]}
    )
    data_versions = sorted(
        {value for lane in components.values() for value in lane["data_versions"]}
    )

    def composite(prefix: str, values: list[str]) -> str:
        if len(values) == 1:
            return values[0][:80]
        return f"{prefix}_{_hash(values)[:20]}"

    return (
        PromotionModelIdentity(
            runtime_mode=PromotionRuntimeMode.DEPLOYED,
            champion_model_version=composite("promotion_composite", model_versions),
            challenger_model_version=None,
            feature_version=composite("promotion_features_composite", feature_versions),
            data_version=composite("promotion_data_composite", data_versions),
        ),
        public_components,
    )


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _candidate_identity(item: Mapping[str, Any], prediction_trade_date: date) -> dict:
    factors = dict(item.get("probability_factors") or {})
    return {
        "code": str(item.get("code") or "").strip(),
        "target_board": _int(item.get("target_board"), 1),
        "prediction_trade_date": prediction_trade_date.isoformat(),
        "candidate_route": str(item.get("candidate_route") or ""),
        "raw_probability": round(item["raw_probability"], 10),
        "calibrated_probability": round(item["probability"], 10),
        "pool_rank": _int(factors.get("prediction_pool_rank")),
        "rank_position": _int(factors.get("prediction_ranked_position")),
        "recall_rank_position": _int(factors.get("prediction_recall_ranked_position")),
        "rank_scope": str(factors.get("prediction_record_scope") or "pool_unranked"),
        "feature_hash": _hash(factors),
    }


async def ensure_prediction_ledger_storage(db: AsyncSession) -> None:
    """Create ledger tables when deployment migrations have not run yet."""

    tables = (
        PromotionModelArtifact.__table__,
        PromotionPredictionRun.__table__,
        PromotionPredictionSnapshot.__table__,
        PromotionTrainingRun.__table__,
        PromotionExperiment.__table__,
        PromotionShadowRun.__table__,
        PromotionShadowPrediction.__table__,
        PromotionShadowEvaluation.__table__,
        PromotionDeploymentEvent.__table__,
    )
    for table in tables:
        await db.run_sync(
            lambda sync_session, target=table: target.create(
                bind=sync_session.get_bind(), checkfirst=True
            )
        )


async def ensure_model_artifact(
    db: AsyncSession,
    identity: PromotionModelIdentity,
    *,
    algorithm: str = "legacy_rule_ensemble",
) -> PromotionModelArtifact:
    """Register the active model identity without silently changing evidence."""

    artifact = await db.scalar(
        select(PromotionModelArtifact).where(
            PromotionModelArtifact.model_version == identity.active_model_version
        )
    )
    if artifact is not None:
        return artifact
    artifact = PromotionModelArtifact(
        model_version=identity.active_model_version,
        feature_version=identity.feature_version,
        data_version=identity.data_version,
        algorithm=algorithm,
        status="champion",
        params_json="{}",
        metrics_json="{}",
        activated_at=datetime.now(),
    )
    db.add(artifact)
    await db.flush()
    return artifact


def _reason_payload(item: Mapping[str, Any]) -> dict:
    return {
        "candidate_route_label": item.get("candidate_route_label"),
        "main_probability_name": item.get("main_probability_name"),
        "reason": item.get("reason"),
        "reasons": item.get("reasons") or [],
        "risk_flags": item.get("risk_flags") or [],
        "watch_bucket": item.get("watch_bucket"),
        "watch_bucket_reason": item.get("watch_bucket_reason"),
        "time_horizon": item.get("time_horizon"),
        "time_horizon_reason": item.get("time_horizon_reason"),
    }


@dataclass(frozen=True, slots=True)
class ScheduleBatch:
    """Scheduler-owned attempt identity, independent of whether any row survives.

    All clocks use the application's local naive-time convention. This only
    identifies an attempt; it is not proof of complete market-universe coverage.
    """
    snapshot_context: str
    recorded_at: datetime

    def validate(self) -> None:
        if not isinstance(self.snapshot_context, str) or self.snapshot_context not in {
            "promotion_0925", "promotion_0935", "promotion_1000",
            "promotion_1030", "promotion_1305", "promotion_1400",
            "promotion_1430", "promotion_1510", "promotion_2000",
        }:
            raise ValueError("schedule_batch: unsupported_context")
        if (
            not isinstance(self.recorded_at, datetime)
            or self.recorded_at.tzinfo is not None
            or self.recorded_at > datetime.now()
        ):
            raise ValueError("schedule_batch: invalid_or_future_recorded_at")

    def validate_candidates(
        self, candidates: list[dict], trade_date_by_target: Mapping[int, date],
        *, snapshot_source: str,
    ) -> None:
        """A successful run may supersede a start marker only with matching inputs."""
        self.validate()
        if snapshot_source != "schedule":
            raise ValueError("schedule_batch: official_source_required")
        for target, trade_day in trade_date_by_target.items():
            if type(target) is not int or target not in (1, 2) or (
                type(trade_day) is not date or trade_day > self.recorded_at.date()
            ):
                raise ValueError("schedule_batch: invalid_target_trade_date")
        for item in candidates:
            factors = item.get("probability_factors") or {}
            if (
                factors.get("prediction_snapshot_source") != "schedule"
                or factors.get("prediction_snapshot_context") != self.snapshot_context
            ):
                raise ValueError("schedule_batch: candidate_context_mismatch")
            recorded_at = factors.get("prediction_snapshot_recorded_at")
            try:
                parsed = (
                    recorded_at if isinstance(recorded_at, datetime)
                    else datetime.fromisoformat(recorded_at)
                )
            except (ValueError, TypeError):
                raise ValueError("schedule_batch: invalid_candidate_clock") from None
            if parsed.tzinfo is not None or parsed not in (
                self.recorded_at, self.recorded_at.replace(microsecond=0),
            ):
                raise ValueError("schedule_batch: candidate_clock_mismatch")

    @property
    def batch_key(self) -> str:
        return f"schedule:{self.snapshot_context}:{self.recorded_at.isoformat()}"


async def append_blocked_prediction_run(
    db: AsyncSession,
    trade_date_by_target: Mapping[int, date],
    *,
    identity: PromotionModelIdentity,
    batch: ScheduleBatch,
    persistence: Mapping[str, Any],
    quality_gate: Mapping[str, Any] | None = None,
) -> LedgerAppendResult:
    """Append a zero-snapshot blocker, never a successful zero-universe claim."""
    batch.validate()
    for target, trade_day in trade_date_by_target.items():
        if type(target) is not int or target not in (1, 2) or (
            type(trade_day) is not date or trade_day > batch.recorded_at.date()
        ):
            raise ValueError("schedule_batch: invalid_target_trade_date")
    status = persistence.get("status")
    if status not in {
        "empty_unproven", "fully_filtered", "invalid_candidate_identity", "generation_pending",
        "invalid_candidate_route",
    }:
        raise ValueError("schedule_batch: unsupported_block_reason")
    counts = [persistence.get(key) for key in
              ("input_count", "recordable_count", "prepared_count")]
    if status == "generation_pending":
        # Before generation the denominator is unknown, NOT zero.
        if counts != [None, None, None]:
            raise ValueError("schedule_batch: pending_counts_must_be_unknown")
    else:
        if any(type(value) is not int or value < 0 for value in counts) or not (
            counts[0] >= counts[1] >= counts[2]
        ):
            raise ValueError("schedule_batch: invalid_counts")
        if (
            (status == "empty_unproven" and counts != [0, 0, 0])
            or (status == "fully_filtered" and not (counts[0] > 0 and counts[1:] == [0, 0]))
            or (status == "invalid_candidate_identity" and not counts[1] > counts[2])
            or (status == "invalid_candidate_route" and not (counts[0] > 0 and counts[1:] == [0, 0]))
        ):
            raise ValueError("schedule_batch: reason_count_mismatch")
    metadata = {
        "protocol": "promotion_batch_attempt_v1",
        "batch_keys": [batch.batch_key],
        "source_values": ["schedule"],
        "context_values": [batch.snapshot_context],
        "trade_date_by_target": {
            str(target): trade_day.isoformat()
            for target, trade_day in sorted(trade_date_by_target.items())
        },
        "universe_complete": None,
        "persistence": dict(persistence),
        "quality_gate": dict(quality_gate or {}),
    }
    if status == "invalid_candidate_route":
        metadata["persistence"]["candidate_validation_stage"] = "route_identity_before_recordability"
    payload_hash = _hash(metadata)
    run_key = _hash({
        "model_version": identity.active_model_version,
        "feature_version": identity.feature_version,
        "data_version": identity.data_version,
        "batch_key": batch.batch_key,
        "status": "blocked",
        "payload_hash": payload_hash,
    })
    await ensure_prediction_ledger_storage(db)
    existing = await db.scalar(
        select(PromotionPredictionRun).where(PromotionPredictionRun.run_key == run_key)
    )
    if existing is not None:
        return LedgerAppendResult(int(existing.id), run_key, 0, False)
    await ensure_model_artifact(db, identity)
    completed_at = datetime.now()
    run = PromotionPredictionRun(
        run_key=run_key,
        snapshot_batch_key=batch.batch_key,
        # The attempted scheduler session, not a stale candidate evidence date.
        reference_trade_date=batch.recorded_at.date(),
        as_of_at=batch.recorded_at,
        snapshot_source="schedule",
        snapshot_context=batch.snapshot_context,
        model_version=identity.active_model_version,
        feature_version=identity.feature_version,
        data_version=identity.data_version,
        runtime_mode=identity.runtime_mode.value,
        status="blocked",
        gate_passed=False,
        candidate_count=0,
        ranked_count=0,
        actionable_count=0,
        payload_hash=payload_hash,
        metadata_json=_json(metadata),
        created_at=completed_at,
        completed_at=completed_at,
    )
    db.add(run)
    await db.flush()
    return LedgerAppendResult(int(run.id), run_key, 0, True)


async def append_prediction_run(
    db: AsyncSession,
    candidates: list[dict],
    trade_date_by_target: Mapping[int, date],
    *,
    identity: PromotionModelIdentity,
    snapshot_source: str,
    snapshot_context: str,
    legacy_record_ids: Mapping[tuple[str, int, date, str, str, str], int] | None = None,
    quality_gate: Mapping[str, Any] | None = None,
    schedule_batch: ScheduleBatch | None = None,
) -> LedgerAppendResult | None:
    """Append one idempotent run and its candidate snapshots.

    Idempotency is limited to an identical model/batch/payload tuple. A later
    retry with a new recorded-at batch key is intentionally a new immutable run.
    """

    # Direct ledger callers have the same strict boundary as the compatibility
    # adapter. Validate the entire input before filtering or any database work.
    candidates = [project_promotion_probability(item) for item in candidates]
    if any(
        (
            normalize_snapshot_source(snapshot_source) == "schedule"
            or normalize_snapshot_source(
                (item.get("probability_factors") or {}).get("prediction_snapshot_source")
            ) == "schedule"
        )
        and route_identity(item.get("candidate_route")) not in {"known", "known_legacy"}
        for item in candidates
    ):
        raise ValueError("prediction_candidate: invalid_candidate_route")
    if schedule_batch is not None:
        schedule_batch.validate_candidates(
            candidates, trade_date_by_target, snapshot_source=snapshot_source,
        )
        if snapshot_context != schedule_batch.snapshot_context:
            raise ValueError("schedule_batch: run_context_mismatch")
    prepared: list[tuple[dict, date, dict]] = []
    for item in candidates:
        code = str(item.get("code") or "").strip()
        target_board = _int(item.get("target_board"), 1)
        prediction_trade_date = trade_date_by_target.get(target_board)
        if not code or type(prediction_trade_date) is not date:
            raise ValueError("prediction_candidate: missing_code_or_trade_date")
        prepared.append((item, prediction_trade_date, dict(item.get("probability_factors") or {})))
    if not prepared:
        return None

    await ensure_prediction_ledger_storage(db)
    identity, model_components = _effective_run_identity(prepared, identity)
    await ensure_model_artifact(
        db,
        identity,
        algorithm=(
            "deployed_probability_overlay"
            if identity.runtime_mode == PromotionRuntimeMode.DEPLOYED
            else "legacy_rule_ensemble"
        ),
    )

    contexts = {
        str(factors.get("prediction_snapshot_context") or snapshot_context or snapshot_source)
        for _item, _trade_date, factors in prepared
    }
    sources = {
        str(factors.get("prediction_snapshot_source") or snapshot_source)
        for _item, _trade_date, factors in prepared
    }
    batch_keys = sorted(
        {
            str(factors.get("prediction_snapshot_batch_key") or "")
            for _item, _trade_date, factors in prepared
            if str(factors.get("prediction_snapshot_batch_key") or "")
        }
    )
    recorded_times = sorted(
        parsed
        for _item, _trade_date, factors in prepared
        if (parsed := _parse_datetime(factors.get("prediction_snapshot_recorded_at")))
    )
    as_of_at = recorded_times[0] if recorded_times else datetime.now()
    normalized_source = next(iter(sources)) if len(sources) == 1 else "mixed"
    normalized_context = next(iter(contexts)) if len(contexts) == 1 else "mixed"
    if schedule_batch is not None:
        # Candidate display metadata has second precision; attempt ordering and
        # idempotency must retain the independently captured microsecond clock.
        as_of_at = schedule_batch.recorded_at
        batch_keys = [schedule_batch.batch_key]
    reference_trade_date = max(prediction_trade_date for _item, prediction_trade_date, _factors in prepared)

    canonical_candidates = sorted(
        (
            _candidate_identity(item, prediction_trade_date)
            for item, prediction_trade_date, _factors in prepared
        ),
        key=lambda item: (
            item["target_board"],
            item["prediction_trade_date"],
            item["code"],
            item["candidate_route"],
        ),
    )
    payload_hash = _hash(canonical_candidates)
    run_key = _hash(
        {
            "model_version": identity.active_model_version,
            "feature_version": identity.feature_version,
            "data_version": identity.data_version,
            "source": normalized_source,
            "context": normalized_context,
            "batch_keys": batch_keys,
            "payload_hash": payload_hash,
        }
    )
    existing = await db.scalar(
        select(PromotionPredictionRun).where(PromotionPredictionRun.run_key == run_key)
    )
    if existing is not None:
        return LedgerAppendResult(
            run_id=int(existing.id),
            run_key=run_key,
            snapshot_count=int(existing.candidate_count or 0),
            created=False,
        )

    ranked_count = sum(
        1 for _item, _date, factors in prepared if bool(factors.get("prediction_ranked_selected"))
    )
    actionable_count = sum(
        1 for _item, _date, factors in prepared if bool(factors.get("prediction_actionable"))
    )
    run = PromotionPredictionRun(
        run_key=run_key,
        snapshot_batch_key="|".join(batch_keys),
        reference_trade_date=reference_trade_date,
        as_of_at=as_of_at,
        snapshot_source=normalized_source,
        snapshot_context=normalized_context,
        model_version=identity.active_model_version,
        feature_version=identity.feature_version,
        data_version=identity.data_version,
        runtime_mode=identity.runtime_mode.value,
        status="completed",
        gate_passed=(bool(quality_gate.get("gate_passed")) if quality_gate is not None else None),
        candidate_count=len(prepared),
        ranked_count=ranked_count,
        actionable_count=actionable_count,
        payload_hash=payload_hash,
        metadata_json=_json(
            {
                "batch_keys": batch_keys,
                "source_values": sorted(sources),
                "context_values": sorted(contexts),
                "trade_date_by_target": {
                    str(target): trade_day.isoformat()
                    for target, trade_day in sorted(trade_date_by_target.items())
                },
                "model_components_by_target": model_components,
                "quality_gate": dict(quality_gate or {}),
            }
        ),
        created_at=as_of_at,
        completed_at=datetime.now(),
    )
    db.add(run)
    await db.flush()

    legacy_ids = legacy_record_ids or {}
    for item, prediction_trade_date, factors in prepared:
        code = str(item.get("code") or "").strip()
        target_board = _int(item.get("target_board"), 1)
        route = str(item.get("candidate_route") or "")
        source = str(factors.get("prediction_snapshot_source") or snapshot_source)
        context = str(factors.get("prediction_snapshot_context") or snapshot_context or source)
        lookup_key = (code, target_board, prediction_trade_date, route, source, context)
        identity_payload = _candidate_identity(item, prediction_trade_date)
        record_key = _hash(identity_payload)
        rank_position = _int(factors.get("prediction_ranked_position")) or None
        recall_position = _int(factors.get("prediction_recall_ranked_position")) or None
        pool_rank = _int(factors.get("prediction_pool_rank")) or None
        db.add(
            PromotionPredictionSnapshot(
                run_id=run.id,
                record_key=record_key,
                legacy_record_id=legacy_ids.get(lookup_key),
                code=code,
                name=str(item.get("name") or ""),
                target_board=target_board,
                prediction_trade_date=prediction_trade_date,
                horizon_days=1,
                candidate_route=route,
                learning_bucket=str(item.get("learning_bucket") or ""),
                rank_scope=str(factors.get("prediction_record_scope") or "pool_unranked"),
                pool_rank=pool_rank,
                rank_position=rank_position,
                recall_rank_position=recall_position,
                raw_probability=item["raw_probability"],
                calibrated_probability=item["probability"],
                confidence_level=str(item.get("confidence_level") or ""),
                signal_status=str(item.get("signal_status") or ""),
                trade_gate_passed=bool(factors.get("prediction_trade_gate_passed")),
                actionable=bool(factors.get("prediction_actionable")),
                watch_only=bool(factors.get("prediction_watch_only")),
                reason_json=_json(_reason_payload(item)),
                features_json=_json(factors),
                created_at=as_of_at,
            )
        )
    await db.flush()
    return LedgerAppendResult(
        run_id=int(run.id),
        run_key=run_key,
        snapshot_count=len(prepared),
        created=True,
    )
