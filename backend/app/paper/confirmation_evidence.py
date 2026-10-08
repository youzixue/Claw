"""Observation-only evidence. Never consulted by trading or confirmation gates."""
import asyncio
import json
import logging
from time import perf_counter_ns
from uuid import uuid4
from datetime import datetime, time

from sqlalchemy import select
from app.models.paper import PaperAutoTradeLog

logger = logging.getLogger(__name__)


def confirmation_evidence(candidate: dict) -> dict:
    """Legacy source-specific execution_confirmation cannot prove any layer."""
    raw = candidate.get("confirmation_evidence")
    raw = raw if isinstance(raw, dict) else {}
    result = {
        key: raw.get(key) if raw.get(key) in ("true", "false") else "unknown"
        for key in (
            "historical_quote_path_confirmed", "current_setup_valid",
            "execution_permitted",
        )
    }
    order_result = raw.get("order_result")
    result["order_result"] = order_result if isinstance(order_result, str) and order_result else "unknown"
    result["historical_confirmed_at"] = raw.get("historical_confirmed_at")
    result["historical_log_id"] = raw.get("historical_log_id")
    result["observed_at"] = raw.get("observed_at")
    return result


async def historical_confirmation_evidence(
    db, *, account_id, trade_date, code, source, strategy_version, observed_at,
) -> dict:
    """Read only already-visible same-identity quote confirmations.

    This historical fact does not expire, but it never restores a candidate or
    grants readiness. A missing record is unknown, not proof of no confirmation.
    No API read-time enrichment: the caller freezes this on the new decision.
    """
    evidence = confirmation_evidence({})
    try:
        evidence["observed_at"] = observed_at.isoformat()
        if observed_at.date() != trade_date:
            return evidence
        # Existing account/date/code/created_at indexes remain available. Select
        # only evidence leaves, not full ORM entities or unrelated log payloads.
        stmt = select(
            PaperAutoTradeLog.id, PaperAutoTradeLog.created_at,
            PaperAutoTradeLog.candidate_json,
        ).where(
            PaperAutoTradeLog.account_id == account_id,
            PaperAutoTradeLog.trade_date == trade_date,
            PaperAutoTradeLog.code == code,
            PaperAutoTradeLog.source == source,
            PaperAutoTradeLog.strategy_version == strategy_version,
            PaperAutoTradeLog.action == "confirm_buy",
            PaperAutoTradeLog.decision == "quote_confirmed",
            PaperAutoTradeLog.created_at >= datetime.combine(trade_date, time.min),
            PaperAutoTradeLog.created_at <= observed_at,
        ).order_by(PaperAutoTradeLog.created_at.desc(), PaperAutoTradeLog.id.desc())
        # Connection-level SAVEPOINT deliberately avoids Session.begin_nested's
        # unconditional flush of pending business objects. This query is Core-only:
        # no ORM writes/autoflush and no rollback/commit of the outer transaction.
        connection = await db.connection()
        async with connection.begin_nested():
            rows = await connection.execute(stmt)
            for row in rows:
                try:
                    payload = json.loads(row.candidate_json or "{}")
                    if not isinstance(payload, dict) or payload.get("confirmation_version") != "champion_persistent_v1":
                        continue
                    sample_at = datetime.fromisoformat(str(payload.get("confirmation_sample_at")))
                    if sample_at.date() != trade_date or sample_at > observed_at:
                        continue
                except (ValueError, TypeError):
                    continue
                evidence.update(
                    historical_quote_path_confirmed="true",
                    historical_confirmed_at=row.created_at.isoformat(),
                    historical_log_id=row.id,
                )
                break
        return evidence
    except Exception:
        # Audit availability is not an execution gate. Never include SQL, bind
        # parameters, exception text/tracebacks, or account details in the warning.
        logger.warning("Confirmation history audit unavailable; evidence is unknown")
        unknown = confirmation_evidence({})
        unknown["observed_at"] = evidence.get("observed_at")
        return unknown


def order_confirmation_evidence(candidate: dict, status: str) -> dict:
    """Submission response is distinct from a completed fill; unknown stays unknown."""
    evidence = confirmation_evidence(candidate)
    evidence["order_result"] = status or "unknown"
    evidence["execution_permitted"] = (
        "true" if status in {"filled", "submitted", "partial"}
        else "false" if status in {"rejected", "risk_blocked"} else "unknown"
    )
    return evidence


def _emit_candidate_stage(payload):
    # Observability must not turn a source success/failure into a different result.
    # The payload is built from owned leaves only; never pass candidates to logging.
    try:
        logger.info("[paper-candidate-stage] %s", json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


async def observe_candidate_stage(
    stage, operation, *args, round_id=None, span_account_id=None,
    result_kind="candidates_notes", input_candidate_count=None, **kwargs,
):
    """Measure one serial source call, preserving its exact return and exceptions."""
    started = perf_counter_ns()
    base = {
        "schema": "paper_candidate_stage_v1", "span_id": uuid4().hex,
        "stage": stage if type(stage) is str else None,
        "round_id": round_id if type(round_id) is str else None,
        "account_id": span_account_id if type(span_account_id) is int else None,
        "input_candidate_count": input_candidate_count if type(input_candidate_count) is int else None,
        "clock": "perf_counter_ns", "candidate_count": None, "result_count": None,
        "source_scan_issues": [], "source_scan_status": "unknown",
    }
    def emit(status, **leaves):
        _emit_candidate_stage({
            **base, "status": status,
            "elapsed_ms": round((perf_counter_ns() - started) / 1_000_000, 3),
            **leaves,
        })
    emit("started")
    try:
        result = await operation(*args, **kwargs)
    except asyncio.CancelledError:
        emit("cancelled")
        raise
    except Exception as exc:
        emit("failed", error_type=type(exc).__name__)
        raise
    count = None
    result_count = None
    issues = []
    # Read only expected containers and declared issue leaves, without copying rows.
    if result_kind == "candidates_notes" and type(result) is tuple and len(result) == 2:
        rows, notes = result
        count = len(rows) if type(rows) is list else None
        for issue in getattr(notes, "issues", ()):
            if type(issue) is dict:
                issues.append({key: issue[key] for key in ("source", "reason_code", "code")
                               if type(issue.get(key)) is str})
    elif result_kind == "candidates" and type(result) is list:
        count = len(result)
    elif result_kind == "fund" and type(result) is dict:
        result_count = len(result)
    emit("completed", candidate_count=count, result_count=result_count,
         source_scan_issues=issues, source_scan_status="degraded" if issues else "completed")
    return result


def _observation_now():
    return datetime.now()


def entry_consumer_timing(*, decision_at, confirmed_at=None, event_created_at=None) -> dict:
    """Observe entry processing on the wall clock; never consulted by a gate."""
    at = _observation_now()
    def valid(value):
        return isinstance(value, datetime) and value.tzinfo is None
    clocks = (decision_at, confirmed_at, event_created_at)
    ok = valid(at) and all(value is None or (
        valid(value) and value.date() == at.date() and value <= at
    ) for value in clocks) and valid(decision_at)
    if confirmed_at is not None and event_created_at is not None:
        ok = ok and confirmed_at <= event_created_at
    def stamp(value):
        return value.isoformat() if valid(value) else None
    return {
        "schema": "paper_entry_wall_clock_v1",
        "basis": "per_candidate_entry_not_transaction_commit",
        "decision_clock": stamp(decision_at),
        "confirmed_at": stamp(confirmed_at),
        "event_created_at": stamp(event_created_at),
        "consumer_started_at": stamp(at),
        "clock_status": "ok" if ok else "invalid",
        "confirmed_to_consumer_seconds": (
            round((at - confirmed_at).total_seconds(), 6) if ok and confirmed_at is not None else None),
        "event_created_to_consumer_seconds": (
            round((at - event_created_at).total_seconds(), 6) if ok and event_created_at is not None else None),
        "commit_known_at": None,
    }


def log_execution_timing(candidate: dict) -> dict | None:
    raw = candidate.get("execution_timing")
    if not isinstance(raw, dict) or raw.get("schema") != "paper_entry_wall_clock_v1":
        return None
    # Only owned diagnostic leaves, never a live object or a new trade permission.
    result = {key: value for key, value in raw.items()
              if type(value) in (str, int, float, bool) or value is None}
    at = _observation_now()
    try:
        started = datetime.fromisoformat(str(raw.get("consumer_started_at")))
        ok = (raw.get("clock_status") == "ok" and started.tzinfo is None
              and at.tzinfo is None and started.date() == at.date() and started <= at)
    except (ValueError, TypeError):
        ok = False
    result.update(
        log_observed_at=at.isoformat(),
        consumer_to_log_seconds=round((at-started).total_seconds(), 6) if ok else None,
        log_clock_status="ok" if ok else "invalid", commit_known_at=None,
    )
    return result


def deferred_buy_log_evidence(candidate: dict, *, outcome: dict, code: str,
                              evaluated_at: datetime) -> dict:
    """New response observation, never reuse old setup/permission as this round.

    Invoked explicitly by reconciliation, not enabled by a marker in candidate.
    No query, mutation, signal, order or inference that waiting means risk passed.
    """
    at = _observation_now()
    if (not isinstance(evaluated_at, datetime) or evaluated_at.tzinfo is not None
            or evaluated_at.date() != at.date() or evaluated_at > at
            or type(outcome) is not dict or type(outcome.get("order")) is not dict):
        raise ValueError("invalid_deferred_buy_observation")
    order = outcome["order"]
    if (order.get("broker") != "paper" or order.get("side") != "buy"
            or order.get("code") != code or not isinstance(order.get("order_id"), str)
            or not order["order_id"]):
        raise ValueError("invalid_deferred_buy_identity")
    prior = confirmation_evidence(candidate)
    current = dict(prior)
    status = order.get("status")
    status = status if isinstance(status, str) and status else "unknown"
    event = outcome.get("event")
    event = event if isinstance(event, str) else "unknown"
    fills = outcome.get("fills")
    if type(fills) is not list or len(fills) > 64:
        raise ValueError("invalid_deferred_buy_fill_list")
    # Reported execution, not a reusable future permission or profitability claim.
    fill_identity_ok = all(
        type(fill) is dict and fill.get("order_id") == order["order_id"]
        and fill.get("broker") == "paper" and fill.get("side") == "buy"
        and fill.get("code") == code and type(fill.get("quantity")) is int
        and fill["quantity"] >= 100 and fill["quantity"] % 100 == 0
        for fill in fills
    )
    filled = sum(fill["quantity"] for fill in fills) if fills and fill_identity_ok else None
    permission = "unknown"
    basis = "not_proven_this_round"
    if event == status and status in {"filled", "partial"} and filled is not None:
        permission, basis = "true", "reported_execution_this_round_not_reusable_permission"
    elif event == status and status in {"risk_blocked", "rejected", "canceled"} and fills == []:
        permission, basis = "false", "reported_block_or_terminal_response_this_round"
    current.update(
        current_setup_valid="unknown", execution_permitted=permission,
        order_result=status, observed_at=at.isoformat(),
    )
    # Original history stays original; this reconciler does not rerun the setup.
    def leaf(value):
        return value if value is None or type(value) in (str, int, bool) else None
    return {
        "confirmation_evidence": current,
        "deferred_order_observation": {
            "schema_version": "confirmation:deferred_buy_v1",
            "original_decision_confirmation": prior,
            "original_decision_scope": "frozen_order_candidate_not_current_setup",
            "order_response": {key: leaf(order.get(key)) for key in (
                "order_id", "account_id", "code", "side", "status", "strategy_version",
                "decision_round_id", "last_fill_round_id", "decision_at", "trade_date")},
            "outcome_event": event,
            "reported_filled_quantity_this_round": filled if fills else 0,
            "reported_fill_order_identity_matches": fill_identity_ok if fills else None,
            "execution_permission_basis": basis,
            "current_setup_basis": "not_rechecked_by_deferred_reconciliation",
            "strategy_evaluated_at": evaluated_at.isoformat(),
            "response_observed_at": at.isoformat(),
            "commit_known_at": None, "replay_ready": False,
        },
    }


def freeze_log_evidence(candidate: dict, *, action: str, decision: str) -> dict:
    """Freeze this log's observation without changing the candidate/earlier logs."""
    evidence = confirmation_evidence(candidate)
    if action == "skip_buy" and decision in {"blocked", "skipped"}:
        evidence["execution_permitted"] = "false"
        evidence["order_result"] = "not_submitted"
    elif action == "confirm_buy":
        evidence["order_result"] = "not_submitted"
    elif action == "buy" and decision == "dry_run":
        evidence["order_result"] = "dry_run"
    return evidence
