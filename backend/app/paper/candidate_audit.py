"""Append-only per-stock candidate decisions on the existing auto-log ledger.

This module never decides eligibility, confirms a signal, submits, or notifies.
Only state transitions are persisted; unchanged observations reference the first
record instead of writing the full immutable pool on every quote round.
"""
import hashlib
import json
from datetime import datetime

from sqlalchemy import func, select

from app.models.paper import PaperAutoTradeLog

SCHEMA = "candidate_trace_v1"
ACTION = "candidate_audit"


def promotion_trace(record, run, *, cutoff, decision_at, reason_code, reason,
                    decision="rejected", stage="candidate_contract",
                    technical_evaluated=False, probability=None, threshold=None,
                    metrics=None):
    """Small owned leaves from the very predicate that made the decision."""
    def at(value):
        return value.isoformat() if isinstance(value, datetime) else None
    facts = dict(metrics or {})
    identity = {
        "snapshot_id": record.id, "prediction_run_id": run.id,
        "reason_code": reason_code,
        # Mainline reasons include changing live strengths/counts. The producer
        # supplies a stable predicate code; full first-observed values stay below.
        "predicate_detail": facts.get("predicate_code") or facts.get("full_reason") or reason,
        "decision": decision, "stage": stage,
        "technical_evaluated": technical_evaluated,
        "production_probability": probability, "threshold": threshold,
        # Settings/structural evidence belongs in the transition identity;
        # changing price or clock alone is not a new rejection state.
        "trade_gate_passed": record.trade_gate_passed,
        "watch_only": record.watch_only, "snapshot_actionable": record.actionable,
        "rank_scope": record.rank_scope,
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, allow_nan=False).encode()).hexdigest()
    trace = {
        "schema": SCHEMA, "state_key": digest, "decision": decision,
        "technical_evaluated": technical_evaluated,
        "decision_at": at(decision_at), "visible_cutoff": at(cutoff),
        "snapshot_created_at": at(record.created_at),
        "batch_as_of_at": at(run.as_of_at), "batch_created_at": at(run.created_at),
        "batch_completed_at": at(run.completed_at),
        "production_probability": probability, "probability_threshold": threshold,
        "trade_gate_passed": record.trade_gate_passed, "watch_only": record.watch_only,
        "snapshot_actionable": record.actionable, "rank_scope": record.rank_scope,
        "gate_metrics": facts, "is_buy_confirmation": False,
        "recording_semantics": "first_observation_of_state_transition_not_every_scan",
    }
    return {
        "code": str(record.code or "").strip(), "name": str(record.name or ""),
        "stage_code": stage, "reason_code": reason_code, "reason": reason,
        "metric_value": probability if stage == "candidate_contract" else None,
        "threshold_value": threshold,
        "candidate": {
            "prediction_snapshot_id": record.id, "prediction_run_id": run.id,
            "prediction_run_key": run.run_key, "snapshot_context": run.snapshot_context,
            "route": record.candidate_route, "target_board": record.target_board,
            "signal_date": record.prediction_trade_date.isoformat(),
            "candidate_trace": trace,
        },
    }


def candidate_trace(item):
    candidate = item.get("candidate")
    if not isinstance(candidate, dict):
        return None
    trace = candidate.get("candidate_trace")
    return trace if isinstance(trace, dict) and trace.get("schema") == SCHEMA else None


async def append_candidate_audits(db, diagnostics, *, append_log, account_id,
                                 strategy_version, run_id, trade_date, trigger):
    """One projected query and one flush for new transitions, no extra commit.

    The caller owns the existing account/transaction lock; this is not a second
    independent writer or cross-transaction uniqueness mechanism.
    """
    items = [d for d in diagnostics if candidate_trace(d) is not None]
    if not items:
        return [], {"evaluated": 0, "appended": 0, "unchanged": 0, "reused_log_ids": []}
    # A clock rollback / point-in-time replay cannot reuse a future audit row.
    # All items here come from one candidate scan, never mixed decision times.
    clocks = {datetime.fromisoformat(candidate_trace(item)["decision_at"]) for item in items}
    if len(clocks) != 1:
        raise ValueError("one candidate decision clock required")
    observed_at = next(iter(clocks))
    if observed_at.tzinfo is not None or observed_at.date() != trade_date:
        raise ValueError("same-day local candidate decision clock required")
    latest = select(func.max(PaperAutoTradeLog.id)).where(
        PaperAutoTradeLog.account_id == account_id,
        PaperAutoTradeLog.trade_date == trade_date,
        PaperAutoTradeLog.strategy_version == strategy_version,
        PaperAutoTradeLog.action == ACTION,
        PaperAutoTradeLog.created_at <= observed_at,
    ).group_by(PaperAutoTradeLog.code)
    rows = (await db.execute(select(
        PaperAutoTradeLog.id, PaperAutoTradeLog.code, PaperAutoTradeLog.candidate_json,
    ).where(PaperAutoTradeLog.id.in_(latest)))).all()
    previous = {}
    for row_id, code, raw in rows:
        try:
            value = json.loads(raw or "{}").get("candidate_trace", {})
        except (TypeError, ValueError, AttributeError):
            continue
        if isinstance(value, dict) and value.get("schema") == SCHEMA:
            previous[code] = (value.get("state_key"), row_id)
    logs, reused = [], []
    for diagnostic in items:
        trace = candidate_trace(diagnostic)
        old = previous.get(diagnostic["code"])
        if old and old[0] == trace["state_key"]:
            reused.append(old[1])
            continue
        log = await append_log(
            db, account_id=account_id, strategy_version=strategy_version,
            run_id=run_id, trade_date=trade_date, trigger=trigger,
            source="candidate", action=ACTION, decision=trace["decision"],
            flush=False, **diagnostic,
        )
        logs.append(log)
        previous[diagnostic["code"]] = (trace["state_key"], log)
    if logs:
        await db.flush()
    return logs, {"evaluated": len(items), "appended": len(logs),
                  "unchanged": len(reused),
                  "reused_log_ids": [item if isinstance(item, int) else item.id for item in reused]}
