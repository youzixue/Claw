"""Observation-only evidence. Never consulted by trading or confirmation gates."""
import json
import logging
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


def _observation_now():
    return datetime.now()


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
