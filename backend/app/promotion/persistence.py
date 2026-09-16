"""Compatibility persistence adapter plus immutable-ledger dual-write.

This module isolates database mutation from the legacy scoring API.  The caller
supplies policy callbacks (recordability, reason construction, bucket naming),
so feature/scoring code remains side-effect free and can be replaced gradually.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Callable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.signal import PromotionPredictionRecord
from app.promotion.route_contract import route_identity
from app.promotion.ledger import (
    LedgerAppendResult, ScheduleBatch, append_prediction_run, append_blocked_prediction_run,
)
from app.promotion.snapshot_identity import (
    normalize_snapshot_context,
    normalize_snapshot_source,
    parse_snapshot_recorded_at,
    snapshot_batch_key_from_factors,
)
from app.promotion.versioning import (
    PromotionModelIdentity,
    project_promotion_probability,
)


RecordablePolicy = Callable[[dict], bool]
ReasonBuilder = Callable[[dict], dict]
LearningBucketBuilder = Callable[[int, str], str]


@dataclass(frozen=True, slots=True)
class PredictionPersistenceResult:
    """Compatibility-row count plus the exact immutable run that was touched."""

    touched: int
    ledger: LedgerAppendResult | None = None
    status: str = "not_written"
    reason: str | None = None
    input_count: int = 0
    recordable_count: int = 0
    prepared_count: int = 0

    def as_payload(self) -> dict:
        """An appended blocker is NOT proof of a complete zero-candidate pool."""
        return {
            "status": self.status,
            "reason": self.reason,
            "input_count": self.input_count,
            "recordable_count": self.recordable_count,
            "prepared_count": self.prepared_count,
            "filtered_count": (
                None if self.status == "invalid_candidate_route"
                else self.input_count - self.recordable_count
            ),
            "invalid_identity_count": (
                None if self.status == "invalid_candidate_route"
                else self.recordable_count - self.prepared_count
            ),
            "touched": self.touched,
            "ledger_recorded": self.ledger is not None,
            "ledger": self.ledger.as_payload() if self.ledger is not None else None,
        }


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _loads(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _dumps(value) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "{}"


def _record_source(record: PromotionPredictionRecord, factors: dict) -> str:
    explicit = str(getattr(record, "snapshot_source", "") or "").strip()
    return normalize_snapshot_source(
        explicit
        if explicit and explicit != "legacy"
        else factors.get("prediction_snapshot_source")
    )


def _record_context(record: PromotionPredictionRecord, factors: dict) -> str:
    explicit = str(getattr(record, "snapshot_context", "") or "").strip().lower()
    if explicit and explicit != "legacy":
        return normalize_snapshot_context(explicit)
    source = _record_source(record, factors)
    return normalize_snapshot_context(
        factors.get("prediction_snapshot_context"), source=source
    )


def should_preserve_existing_prediction_record(
    record: PromotionPredictionRecord,
    incoming_factors: dict,
    snapshot_source: str,
) -> bool:
    if str(record.outcome_status or "pending") != "pending":
        return True
    existing_factors = _loads(record.factors_json)
    existing_source = _record_source(record, existing_factors)
    incoming_source = normalize_snapshot_source(snapshot_source)
    if existing_source == "schedule" and incoming_source != "schedule":
        return True
    existing_ranked_limit = _int(existing_factors.get("prediction_ranked_limit"))
    incoming_ranked_limit = _int(incoming_factors.get("prediction_ranked_limit"))
    return bool(
        existing_source == incoming_source == "page"
        and existing_ranked_limit > incoming_ranked_limit
        and existing_ranked_limit > 0
        and incoming_ranked_limit > 0
    )


async def ensure_legacy_prediction_storage(db: AsyncSession) -> None:
    await db.run_sync(
        lambda sync_session: PromotionPredictionRecord.__table__.create(
            bind=sync_session.get_bind(), checkfirst=True
        )
    )


async def record_promotion_predictions(
    db: AsyncSession,
    candidates: list[dict],
    trade_date_by_target: dict[int, date],
    *,
    snapshot_source: str,
    model_version: str,
    model_identity: PromotionModelIdentity,
    is_recordable: RecordablePolicy,
    reason_builder: ReasonBuilder,
    learning_bucket_builder: LearningBucketBuilder,
    quality_gate: Mapping[str, Any] | None = None,
    schedule_batch: ScheduleBatch | None = None,
) -> PredictionPersistenceResult:
    if schedule_batch is not None:
        schedule_batch.validate()
        if normalize_snapshot_source(snapshot_source) != "schedule":
            raise ValueError("schedule_batch: official_source_required")

    async def blocked(result: PredictionPersistenceResult) -> PredictionPersistenceResult:
        if schedule_batch is None:
            return result
        # Only an independently supplied official attempt may append a blocker.
        # Preserve old compatibility rows and every older immutable run.
        result = replace(result, reason=(result.reason or "").replace(
            "no run was appended.", "a blocking attempt was appended."
        ))
        ledger = await append_blocked_prediction_run(
            db, trade_date_by_target, identity=model_identity, batch=schedule_batch,
            persistence={
                "status": result.status, "reason": result.reason,
                "input_count": result.input_count,
                "recordable_count": result.recordable_count,
                "prepared_count": result.prepared_count,
            },
            quality_gate=quality_gate,
        )
        return replace(result, ledger=ledger)

    input_count = len(candidates)
    if not candidates:
        return await blocked(PredictionPersistenceResult(
            touched=0, status="empty_unproven",
            reason="No candidates supplied; empty universe is not proven and no run was appended.",
        ))
    # Validate the original batch BEFORE filtering, storage creation or deleting
    # pending compatibility rows. One bad candidate must not silently shrink the
    # denominator or replace a previous valid batch with a partial one.
    projected_candidates = [project_promotion_probability(item) for item in candidates]
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
        return await blocked(PredictionPersistenceResult(
            touched=0, status="invalid_candidate_route", input_count=input_count,
            reason="At least one official input has an invalid/unknown route; no run was appended.",
        ))
    candidates = [item for item in projected_candidates if is_recordable(item)]
    if not candidates:
        return await blocked(PredictionPersistenceResult(
            touched=0, status="fully_filtered", input_count=input_count,
            reason="Recordability policy rejected every input; no run was appended.",
        ))
    recordable_count = len(candidates)
    fallback_source = normalize_snapshot_source(snapshot_source)

    prepared: list[tuple[dict, tuple[str, int, date, str, str, str], dict]] = []
    for item in candidates:
        target_board = _int(item.get("target_board"), 1)
        prediction_trade_date = trade_date_by_target.get(target_board)
        route = str(item.get("candidate_route") or "")
        code = str(item.get("code") or "").strip()
        factors = dict(item.get("probability_factors") or {})
        source = normalize_snapshot_source(
            factors.get("prediction_snapshot_source") or fallback_source
        )
        context = normalize_snapshot_context(
            factors.get("prediction_snapshot_context"), source=source
        )
        if code and type(prediction_trade_date) is date:
            prepared.append(
                (
                    item,
                    (code, target_board, prediction_trade_date, route, source, context),
                    factors,
                )
            )
    if len(prepared) != recordable_count:
        return await blocked(PredictionPersistenceResult(
            touched=0, status="invalid_candidate_identity", input_count=input_count,
            recordable_count=recordable_count, prepared_count=len(prepared),
            reason="At least one recordable candidate lacks a code or target trade date; no run was appended.",
        ))
    if schedule_batch is not None:
        # Reject mismatched clocks/context before deleting compatibility rows.
        schedule_batch.validate_candidates(
            [item for item, _key, _factors in prepared],
            trade_date_by_target, snapshot_source=fallback_source,
        )
    await ensure_legacy_prediction_storage(db)

    dates = sorted({key[2] for _item, key, _factors in prepared})
    targets = sorted({key[1] for _item, key, _factors in prepared})
    existing_records = list(
        (
            await db.execute(
                select(PromotionPredictionRecord).where(
                    PromotionPredictionRecord.prediction_trade_date.in_(dates),
                    PromotionPredictionRecord.target_board.in_(targets),
                )
            )
        ).scalars()
    )

    # A retry replaces only its own pending compatibility context. Immutable
    # ledger siblings are appended below and are never deleted.
    incoming_schedule_scopes = {
        (key[1], key[2], key[4], key[5])
        for _item, key, _factors in prepared
        if key[4] == "schedule"
    }
    deleted_ids: set[int] = set()
    for record in existing_records:
        factors = _loads(record.factors_json)
        scope = (
            _int(record.target_board),
            record.prediction_trade_date,
            _record_source(record, factors),
            _record_context(record, factors),
        )
        if scope in incoming_schedule_scopes and str(record.outcome_status or "pending") == "pending":
            deleted_ids.add(_int(record.id))
            await db.delete(record)
    if deleted_ids:
        await db.flush()
        existing_records = [
            record for record in existing_records if _int(record.id) not in deleted_ids
        ]

    existing: dict[tuple[str, int, date, str, str, str], PromotionPredictionRecord] = {}
    schedule_base_keys: set[tuple[str, int, date, str]] = set()
    for record in existing_records:
        factors = _loads(record.factors_json)
        source = _record_source(record, factors)
        context = _record_context(record, factors) or source
        full_key = (
            str(record.code or "").strip(),
            _int(record.target_board),
            record.prediction_trade_date,
            str(record.candidate_route or ""),
            source,
            context,
        )
        existing[full_key] = record
        if source == "schedule":
            schedule_base_keys.add(full_key[:4])

    touched = 0
    for item, key, incoming_factors in prepared:
        code, target_board, prediction_trade_date, route, source, context = key
        if source != "schedule" and key[:4] in schedule_base_keys:
            continue
        record = existing.get(key)
        if record is not None and should_preserve_existing_prediction_record(
            record, incoming_factors, source
        ):
            continue
        if record is None:
            record = PromotionPredictionRecord(
                code=code,
                target_board=target_board,
                prediction_trade_date=prediction_trade_date,
                candidate_route=route,
                snapshot_source=source,
                snapshot_context=context,
                outcome_status="pending",
            )
            db.add(record)
            existing[key] = record
        touched += 1

        # Validated above; these legacy columns hold raw and the production
        # output respectively. The independent p_calibrated stays in evidence.
        raw_probability = item["raw_probability"]
        calibrated_probability = item["probability"]
        record.name = str(item.get("name") or "")
        record.horizon_days = 1
        record.predicted_probability = raw_probability
        record.calibrated_probability = calibrated_probability
        record.model_adjustment = _float(item.get("model_adjustment"))
        record.confidence_level = str(item.get("confidence_level") or "")
        record.learning_bucket = str(
            item.get("learning_bucket") or learning_bucket_builder(target_board, route)
        )
        record.signal_status = str(item.get("signal_status") or "")
        record.reason_snapshot = _dumps(reason_builder(item))
        record.factors_json = _dumps(incoming_factors)
        record.snapshot_source = source
        record.snapshot_context = context
        record.snapshot_batch_key = snapshot_batch_key_from_factors(incoming_factors)
        record.snapshot_recorded_at = parse_snapshot_recorded_at(
            incoming_factors.get("prediction_snapshot_recorded_at")
        )
        record.model_version = str(
            incoming_factors.get("prediction_model_version") or model_version
        )
    if touched:
        await db.flush()

    schedule_candidates = [
        item
        for item, _key, factors in prepared
        if normalize_snapshot_source(
            factors.get("prediction_snapshot_source") or fallback_source
        )
        == "schedule"
    ]
    ledger_result = None
    if schedule_candidates:
        legacy_record_ids = {
            key: _int(record.id)
            for key, record in existing.items()
            if getattr(record, "id", None) is not None
        }
        first_factors = schedule_candidates[0].get("probability_factors") or {}
        ledger_result = await append_prediction_run(
            db,
            schedule_candidates,
            trade_date_by_target,
            identity=model_identity,
            snapshot_source="schedule",
            snapshot_context=str(
                first_factors.get("prediction_snapshot_context") or "schedule"
            ),
            legacy_record_ids=legacy_record_ids,
            quality_gate=quality_gate,
            schedule_batch=schedule_batch,
        )
    return PredictionPersistenceResult(
        touched=touched, ledger=ledger_result,
        status="recorded" if touched or ledger_result is not None else "preserved_existing",
        input_count=input_count, recordable_count=recordable_count,
        prepared_count=len(prepared),
    )
