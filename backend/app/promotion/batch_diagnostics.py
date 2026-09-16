"""Read-only expected-window diagnostics for the immutable prediction ledger.

Not a scheduler, a candidate selector, or a historical availability certificate.
No create_all/flush/commit, catch-up, model inference, or fallback to older runs.
The caller supplies the producer's existing windows and route contract.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from typing import Mapping, Sequence

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.governance import TradeCalendarModel
from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot

from app.promotion.route_contract import (
    KNOWN_CANDIDATE_ROUTES, ROUTE_CONTRACT_VERSION,
    persisted_contract_status, persisted_route_gate,
)

SCHEMA_VERSION = "promotion_formal_batch_diagnostics_v2"
MAX_ATTEMPTS = 500
_IDENTITY_FIELDS = ("model_version", "feature_version", "data_version", "runtime_mode")


def _object(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _clock(value) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value if isinstance(value, datetime) and value.tzinfo is None else None


def _iso(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else value


def _target_date(value) -> str | None:
    if type(value) is date:
        return value.isoformat()
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
            return value if value == parsed.isoformat() else None
        except ValueError:
            pass
    return None


def _route(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _strings(value) -> list[str]:
    return [x[:120] for x in value[:20] if isinstance(x, str)] if isinstance(value, list) else []


def _attempt(row, aggregates, *, trade_date, checked_at, start, end, expected_identity, routes, is_close_context):
    metadata = _object(row.get("metadata_json"))
    persistence = _object(metadata.get("persistence"))
    quality = _object(metadata.get("quality_gate"))
    gates = quality.get("route_gates")
    actual_identity = {key: row.get(key) for key in _IDENTITY_FIELDS}
    clocks = {key: _clock(row.get(key)) for key in ("as_of_at", "created_at", "completed_at")}
    issues = []
    if actual_identity != dict(expected_identity):
        issues.append("model_identity_mismatch")
    as_of, created, completed = (clocks[k] for k in ("as_of_at", "created_at", "completed_at"))
    if as_of is None or not start <= as_of < end:
        issues.append("attempt_outside_declared_session_window")
    if as_of is None or created is None or completed is None or not as_of <= created <= completed:
        issues.append("invalid_attempt_clock_order")
    elif completed > checked_at:
        issues.append("completion_not_visible_at_cutoff")

    # reference_trade_date is a candidate anchor in older mixed-lane runs. Do not
    # replace it with created_at.date(), or mistake an old anchor for a missing run.
    declared_dates = _object(metadata.get("trade_date_by_target"))
    if is_close_context and row.get("reference_trade_date") != trade_date:
        issues.append("close_reference_date_mismatch")
    if any(g["target_board"] not in (1, 2) for g in aggregates):
        issues.append("invalid_target_board")
    targets = []
    for target in (1, 2):
        group = [g for g in aggregates if g["target_board"] == target]
        normalized_dates = [_target_date(g.get("prediction_trade_date")) for g in group]
        dates = sorted({d for d in normalized_dates if d is not None})
        declared = _target_date(declared_dates.get(str(target)))
        target_issues = []
        if any(d is None for d in normalized_dates):
            target_issues.append("invalid_persisted_target_date")
        if str(target) in declared_dates and declared is None:
            target_issues.append("invalid_declared_target_date")
        if dates and (declared is None or dates != [declared]):
            target_issues.append("target_date_contract_mismatch")
        if any(d > trade_date.isoformat() for d in dates):
            target_issues.append("future_prediction_target_date")
        if is_close_context and dates and dates != [trade_date.isoformat()]:
            target_issues.append("close_target_date_mismatch")
        targets.append({
            "target_board": target, "declared_prediction_trade_date": declared,
            "persisted_prediction_trade_dates": dates,
            "snapshot_count": sum(g["snapshot_count"] for g in group),
            "status": "invalid" if target_issues else "present" if group else "absent_unproven",
            "issues": target_issues,
        })
        issues.extend(target_issues)
    actual_count = sum(g["snapshot_count"] for g in aggregates)
    if row.get("candidate_count") != actual_count:
        issues.append("snapshot_count_mismatch")
    if row.get("ranked_count") != sum(g["ranked_count"] for g in aggregates):
        issues.append("ranked_count_mismatch")
    if row.get("actionable_count") != sum(g["actionable_count"] for g in aggregates):
        issues.append("actionable_count_mismatch")

    route_diagnostics = []
    observed_routes = {_route(g.get("candidate_route")) for g in aggregates}
    if None in observed_routes:
        issues.append("missing_or_invalid_candidate_route")
    if any(r is not None and r not in KNOWN_CANDIDATE_ROUTES for r in observed_routes):
        issues.append("unknown_candidate_route")
    # Identity is today's read-only interpretation. Gate proof must come from
    # this run, not from today's required-route registry.
    contract_status = persisted_contract_status(quality)
    route_keys = sorted(set(routes) | (observed_routes - {None}))
    if None in observed_routes:
        route_keys.append(None)
    for route in route_keys:
        group = [g for g in aggregates if _route(g.get("candidate_route")) == route]
        evidence = persisted_route_gate(quality, route)
        route_diagnostics.append({
            "route": route, **evidence,
            "gate_required_for_diagnostics": bool(
                group or evidence["declared_in_quality_contract"]
                or contract_status != "legacy_unversioned"
            ),
            "snapshot_count": sum(g["snapshot_count"] for g in group),
            "ranked_count": sum(g["ranked_count"] for g in group),
            "actionable_count": sum(g["actionable_count"] for g in group),
            "candidate_presence": "present" if group else "absent_unproven",
            "execution_eligibility": "not_evaluated",
        })

    raw_status = row.get("status")
    if issues:
        state = "invalid_persisted_attempt"
    elif raw_status != "completed":
        state = "generation_pending_unresolved" if persistence.get("status") == "generation_pending" else "persisted_blocked"
    elif actual_count == 0:
        state = "completed_empty_unproven"
    elif any(r["gate_status"] == "unknown" and r["gate_required_for_diagnostics"] for r in route_diagnostics):
        state = "completed_route_gate_unknown"
    elif any(r["gate_status"] == "blocked" for r in route_diagnostics):
        state = "completed_route_blocked"
    elif row.get("gate_passed") is not True:
        state = "completed_batch_gate_not_passed"
    else:
        state = "completed"
    return {
        "id": row.get("id"), "run_key": row.get("run_key"),
        "snapshot_batch_key": row.get("snapshot_batch_key"),
        "snapshot_context": row.get("snapshot_context"),
        "reference_trade_date": _iso(row.get("reference_trade_date")),
        **{k: _iso(row.get(k)) for k in clocks},
        "model_identity": actual_identity, "status": raw_status, "diagnostic_status": state,
        "batch_gate_passed": row.get("gate_passed"),
        "route_contract_status": contract_status,
        "current_route_contract_version": ROUTE_CONTRACT_VERSION,
        "recorded_route_contract_version": (
            quality["route_contract"].get("version")
            if isinstance(quality.get("route_contract"), dict) else None
        ),
        "persistence_status": persistence.get("status"),
        "input_count": persistence.get("input_count"),  # pending is NULL, never 0
        "candidate_count": row.get("candidate_count"), "persisted_snapshot_count": actual_count,
        "ranked_count": row.get("ranked_count"), "actionable_count": row.get("actionable_count"),
        "targets": targets, "routes": route_diagnostics, "issues": sorted(set(issues)),
        "completion_clock_is_commit_receipt": False,
        "generation_may_still_be_running": state == "generation_pending_unresolved",
    }


def build_batch_diagnostics(
    *, trade_date: date, checked_at: datetime, calendar_is_trade_day: bool | None,
    expected_identity: Mapping[str, str], context_windows: Mapping,
    required_routes: Sequence[str], attempts: Sequence[Mapping], snapshot_aggregates: Sequence[Mapping],
    evidence_available: bool = True, evidence_error: str | None = None,
    canonical_close_contexts: Sequence[str] = (),
) -> dict:
    """Pure projection over SELECT results; no side effects and no wall clock calls."""
    if type(trade_date) is not date or not isinstance(checked_at, datetime) or _clock(checked_at) is None:
        raise ValueError("batch diagnostics require a date and local naive checked_at")
    if set(expected_identity) != set(_IDENTITY_FIELDS):
        raise ValueError("complete expected model identity required")
    by_run = defaultdict(list)
    for group in snapshot_aggregates:
        by_run[group["run_id"]].append(group)
    contexts = []
    for context, window in sorted(context_windows.items(), key=lambda item: item[1][0]):
        start = datetime.combine(trade_date, time(*window[0]))
        # Producer validates (hour, minute), inclusively. Thus 16:30:59 is still
        # inside its 16:30 end minute; do not invent an earlier expiry.
        end = datetime.combine(trade_date, time(*window[1])) + timedelta(minutes=1)
        phase = "not_due" if checked_at < start else "open" if checked_at < end else "expired"
        relevant = []
        for row in attempts:
            if row.get("snapshot_source") != "schedule" or row.get("snapshot_context") != context:
                continue
            as_of = _clock(row.get("as_of_at"))
            if row.get("reference_trade_date") == trade_date or (as_of and as_of.date() == trade_date):
                relevant.append(row)
        # Select before validation, including old model/blocked/empty attempts.
        # A failed latest retry must never reveal an earlier favorable success.
        relevant.sort(key=lambda r: (_clock(r.get("as_of_at")) or datetime.max, r.get("id", 0)))
        projected = [_attempt(r, by_run[r["id"]], trade_date=trade_date, checked_at=checked_at,
                     start=start, end=end, expected_identity=expected_identity, routes=required_routes,
                     is_close_context=context in canonical_close_contexts)
                     for r in relevant]
        latest = projected[-1] if projected else None
        if not evidence_available:
            state = "evidence_unavailable"
        elif calendar_is_trade_day is None:
            state = "calendar_unknown"
        elif calendar_is_trade_day is not True:
            state = "not_expected_closed_session"
        elif latest:
            state = latest["diagnostic_status"]
        else:
            state = {"not_due": "not_due", "open": "awaiting_persisted_attempt",
                     "expired": "missing_persisted_attempt"}[phase]
        contexts.append({
            "snapshot_context": context, "window_start": start.isoformat(),
            "window_end_exclusive": end.isoformat(), "window_phase": phase,
            "expected": calendar_is_trade_day, "status": state,
            "attempt_count": len(projected), "latest_attempt": latest, "attempts": projected,
            "missing_attempt_cause": "unknown" if not projected else None,
            "automatic_catchup_allowed": False, "fallback_to_older_run": False,
            "window_expired": phase == "expired",
        })
    status_counts = dict(Counter(item["status"] for item in contexts))
    warn_states = set(status_counts) - {"not_due", "completed", "not_expected_closed_session", "awaiting_persisted_attempt"}
    return {
        "schema_version": SCHEMA_VERSION, "scope": "persisted_formal_attempts_read_only",
        "trade_date": trade_date.isoformat(), "checked_at": checked_at.isoformat(),
        "attempt_match_basis": "formal_reference_date_or_as_of_session_not_created_date",
        "unsupported_context_attempt_ids": [r.get("id") for r in attempts
            if r.get("snapshot_source") == "schedule" and r.get("snapshot_context") not in context_windows],
        "expected_model_identity": dict(expected_identity),
        "expected_identity_scope": "caller_configuration_not_historical_deployment_certificate",
        "calendar_is_trade_day": calendar_is_trade_day,
        "evidence_available": evidence_available, "evidence_error": evidence_error,
        "status": "unavailable" if not evidence_available else "attention" if warn_states else "ok",
        "should_warn": bool(warn_states), "context_status_counts": status_counts,
        "contexts": contexts, "automatic_catchup_allowed": False,
        "historical_point_in_time_certified": False, "process_absence_proven": False,
        "business_success_not_inferred_from_scheduler": True,
        "candidate_or_execution_authorization": False,
    }


async def load_batch_diagnostics(
    db: AsyncSession, *, trade_date: date, checked_at: datetime, expected_identity: Mapping[str, str],
    context_windows: Mapping, required_routes: Sequence[str], officially_closed: bool = False,
    canonical_close_contexts: Sequence[str] = (),
) -> dict:
    """Use the caller's read transaction; never initialize storage or mutate it."""
    kwargs = dict(trade_date=trade_date, checked_at=checked_at, expected_identity=expected_identity,
                  context_windows=context_windows, required_routes=required_routes,
                  canonical_close_contexts=canonical_close_contexts)
    start = datetime.combine(trade_date, time.min)
    end = start + timedelta(days=1)
    run = PromotionPredictionRun
    snap = PromotionPredictionSnapshot
    try:
        with db.no_autoflush:
            calendar = await db.scalar(select(TradeCalendarModel.is_trade_day).where(
                TradeCalendarModel.trade_date == trade_date))
            if officially_closed:
                calendar = False
            result = await db.execute(select(
                run.id, run.run_key, run.snapshot_batch_key, run.reference_trade_date,
                run.as_of_at, run.created_at, run.completed_at, run.snapshot_source,
                run.snapshot_context, run.model_version, run.feature_version, run.data_version,
                run.runtime_mode, run.status, run.gate_passed, run.candidate_count, run.ranked_count,
                run.actionable_count, run.metadata_json,
            ).where(run.snapshot_source == "schedule", or_(
                run.reference_trade_date == trade_date,
                and_(run.as_of_at >= start, run.as_of_at < end),
            )).order_by(run.as_of_at.desc(), run.id.desc()).limit(MAX_ATTEMPTS + 1))
            attempts = [dict(row) for row in result.mappings()]
            if len(attempts) > MAX_ATTEMPTS:
                return build_batch_diagnostics(**kwargs, calendar_is_trade_day=calendar,
                    attempts=[], snapshot_aggregates=[], evidence_available=False,
                    evidence_error="attempt_limit_exceeded_not_a_complete_denominator")
            groups = []
            if attempts:
                result = await db.execute(select(
                    snap.run_id, snap.target_board, snap.prediction_trade_date, snap.candidate_route,
                    func.count().label("snapshot_count"),
                    func.sum(case((snap.rank_scope == "ranked", 1), else_=0)).label("ranked_count"),
                    func.sum(case((snap.actionable.is_(True), 1), else_=0)).label("actionable_count"),
                ).where(snap.run_id.in_([r["id"] for r in attempts])).group_by(
                    snap.run_id, snap.target_board, snap.prediction_trade_date, snap.candidate_route))
                groups = [dict(row) for row in result.mappings()]
        return build_batch_diagnostics(**kwargs, calendar_is_trade_day=calendar,
                                       attempts=attempts, snapshot_aggregates=groups)
    except (SQLAlchemyError, ValueError, TypeError):
        # Legacy malformed Date values can fail ORM result decoding before projection.
        # Missing migrations/read failures are unknown, never a complete empty day.
        # No rollback/DDL/repair here; caller owns the transaction.
        return build_batch_diagnostics(**kwargs, calendar_is_trade_day=None,
            attempts=[], snapshot_aggregates=[], evidence_available=False,
            evidence_error="ledger_read_unavailable")
