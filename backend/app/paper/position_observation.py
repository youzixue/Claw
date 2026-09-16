"""Forward position observations, isolated from signals, orders and trade logs.

Capture pre-execution owned leaves synchronously; append after the original sell
pass completes. Core SAVEPOINT never flushes or commits business ORM objects.
Unknown checks stay unknown. This is not a historical revision or sell permission.
"""
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import logging
import math
import re

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from app.models.paper import PaperShadowEvent

SCHEMA = "research:position_frame_v1"
ROUTE_ID = "position_frame_observation"
ROUTE_VERSION = "research:position_frame_v1"
EVENT_TYPE = "position_frame"
logger = logging.getLogger(__name__)

POSITION_FIELDS = ("id", "account_id", "code", "buy_time", "buy_price", "buy_amount",
                   "strategy_version", "stop_loss_price", "hold_days", "is_closed")
QUOTE_FIELDS = ("quote_round_id", "source_quote_at", "received_at", "updated_at",
                "price", "high", "low", "limit_up", "limit_down")
POLICY_FIELDS = ("basis", "order_id", "first_buy_trade_id", "position_strategy_version",
                 "observed_at", "missing_keys")
EXECUTION_FIELDS = ("available_sell_amount", "exit_trigger_reason", "exit_order_reason",
    "exit_execution_status", "execution_block_code", "execution_block_reason",
    "execution_price", "exit_order_id", "exit_order_status", "exit_filled_quantity_this_round")


def _now():
    return datetime.now()


def _clock(value):
    return value if isinstance(value, datetime) and value.tzinfo is None else None


def _leaf(value):
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if _clock(value) is not None:
        return value.isoformat()
    # Invalid/missing evidence does not gain a fabricated numeric default.
    return None


def _encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_encode(value).encode()).hexdigest()


@dataclass(frozen=True)
class PositionFrame:
    captured_at: datetime
    pre_execution_json: str


def capture_position_frame(*, position, account_name, trade_date, quote,
                           quote_context, evaluated_at, quote_ok, quote_reason,
                           exit_parameters, exit_policy, trigger_reason):
    """No I/O; preserve actual wall clock separately from the source-round cutoff."""
    captured_at = _now()
    if (type(trade_date) is not date or trade_date != captured_at.date()
            or _clock(evaluated_at) is None or evaluated_at.date() != trade_date
            or evaluated_at > captured_at or type(quote_context) is not dict):
        return None
    round_id = quote_context.get("round_id")
    committed_at = _clock(quote_context.get("committed_at"))
    if (not isinstance(round_id, str) or not round_id or committed_at is None
            or committed_at.date() != trade_date or committed_at > captured_at):
        return None
    values = {key: _leaf(getattr(position, key, None)) for key in POSITION_FIELDS}
    if (type(values["id"]) is not int or values["id"] <= 0
            or type(values["account_id"]) is not int or values["account_id"] <= 0
            or not isinstance(values["code"], str) or re.fullmatch(r"[0-9]{6}", values["code"]) is None
            or not isinstance(account_name, str) or not account_name):
        return None
    policy = exit_policy if type(exit_policy) is dict else {}
    policy_projection = {key: _leaf(policy.get(key)) for key in POLICY_FIELDS if key != "missing_keys"}
    missing = policy.get("missing_keys")
    policy_projection["missing_keys"] = list(missing) if type(missing) is list and all(type(k) is str for k in missing) else None
    params = ({key: _leaf(value) for key, value in exit_parameters.items() if type(key) is str}
              if type(exit_parameters) is dict else None)
    quote_values = {key: _leaf(getattr(quote, key, None)) for key in QUOTE_FIELDS}
    payload = {
        "schema_version": SCHEMA, "trade_date": trade_date.isoformat(),
        "account_name": account_name, "position": values,
        "observed_position_state_hash": _hash(values),
        "revision_basis": "observed_state_content_not_trade_derived_revision",
        "quote": quote_values,
        "round_context": {key: _leaf(quote_context.get(key)) for key in (
            "round_id", "as_of_at", "committed_at", "code_version", "config_version")},
        "strategy_evaluated_at": evaluated_at.isoformat(),
        "strategy_clock_basis": "original_quote_round_parameter_not_physical_computation_time",
        "original_quote_check": {"passed": quote_ok if type(quote_ok) is bool else None,
                                 "reason": _leaf(quote_reason)},
        "exit_parameters_observed": params, "exit_policy_trace": policy_projection,
        "original_trigger_reason": _leaf(trigger_reason),
        "session_high_scope": "session_quote_may_precede_entry_not_post_entry_peak",
        "position_and_quote_observed_at": captured_at.isoformat(),
    }
    return PositionFrame(captured_at, _encode(payload))


def _event(frame, execution_context, completed_at):
    if not isinstance(frame, PositionFrame) or _clock(completed_at) is None or completed_at < frame.captured_at:
        raise ValueError("invalid_position_observation_clock")
    before = json.loads(frame.pre_execution_json)
    if type(before) is not dict or before.get("schema_version") != SCHEMA:
        raise ValueError("invalid_position_observation_schema")
    after = {key: _leaf(execution_context.get(key)) for key in EXECUTION_FIELDS}
    # Missing is not T+1 zero, false permission, an unchecked depth pass or a fill.
    quantity = after.get("available_sell_amount")
    if type(quantity) is not int or quantity < 0:
        after["available_sell_amount"] = None
    after["t1_quantity_scope"] = "original_rule_output_only_if_evaluated_not_order_reservation_or_sell_permission"
    after["execution_checks_observed_no_later_than"] = completed_at.isoformat()
    payload = {"schema_version": SCHEMA, "pre_execution": before, "execution_observation": after,
        "observed_at": completed_at.isoformat(), "commit_known_at": None,
        "historical_before_first_observation": "unknown_not_backfilled",
        "corporate_action_and_price_basis": "unproven",
        "calendar_historical_first_known_at": None,
        "fill_observation_clock": None, "execution_permission": None,
        "replay_ready": False, "order_connected": False}
    # Keep distinct quote/state/check revisions; repeat capture time alone is not
    # new evidence. Never update an earlier event or silently discard a changed state.
    stable_before = {k: v for k, v in before.items() if k != "position_and_quote_observed_at"}
    stable_after = {k: v for k, v in after.items() if k != "execution_checks_observed_no_later_than"}
    evidence_hash = _hash({"pre_execution": stable_before, "execution_observation": stable_after})
    payload["evidence_hash"] = evidence_hash
    position = before["position"]
    observed_price = before["quote"].get("price")
    try:
        if type(observed_price) not in (int, float) or not math.isfinite(observed_price) or observed_price <= 0:
            observed_price = None
    except OverflowError:
        observed_price = None
    return {"event_key": "ppf-" + evidence_hash, "route_id": ROUTE_ID, "route_version": ROUTE_VERSION,
        "trade_date": date.fromisoformat(before["trade_date"]), "observed_at": completed_at,
        "created_at": completed_at, "code": position["code"], "name": None,
        "event_type": EVENT_TYPE, "status": "observed_unverified",
        "price": observed_price, "assumed_fill_price": None,
        "change_pct": None, "snapshot_json": _encode(payload)}


async def append_position_frames(db, observations):
    """After the business sell pass, one bounded batch in its existing transaction.

    No new table, migration, ORM flush/rollback/commit, provider request or order.
    The caller must later commit. A failed/cancelled original pass has no claimed
    complete observations. Same semantic state re-entry uses the original event.
    """
    if not observations:
        return {"status": "not_observed", "attempted": 0}
    try:
        completed_at = _now()
        if len(observations) > 256:
            raise ValueError("position_observation_batch_too_large")
        rows = [_event(frame, context, completed_at) for frame, context in observations]
        return await _append_observation_rows(db, rows)
    except Exception as exc:
        logger.warning("Position observation unavailable (%s)", type(exc).__name__)
        return {"status": "unavailable", "error_type": type(exc).__name__}


async def _append_observation_rows(db, rows):
    """Shared Core-only writer; the caller owns commit, errors and cancellation."""
    connection = await db.connection()
    if connection.dialect.name == "sqlite":
        # Preserve a real outer transaction even after a SELECT-only Session.
        raw_connection = await connection.get_raw_connection()
        if not raw_connection.driver_connection.in_transaction:
            await connection.exec_driver_sql("BEGIN")
    async with connection.begin_nested():
        for start in range(0, len(rows), 32):
            statement = sqlite_insert(PaperShadowEvent).values(rows[start:start + 32])
            statement = statement.on_conflict_do_nothing(index_elements=["event_key"])
            await connection.execute(statement)
    return {"status": "staged_not_committed", "attempted": len(rows)}


EXECUTION_SCHEMA = "research:position_execution_v1"
EXECUTION_ROUTE_ID = "position_execution_observation"
EXECUTION_EVENT_TYPE = "execution_frame"
ORDER_RESPONSE_FIELDS = (
    "id", "order_id", "broker", "account_id", "code", "side", "price", "quantity",
    "filled_quantity", "avg_fill_price", "status", "strategy_version",
    "decision_round_id", "last_fill_round_id", "decision_at", "as_of_at",
    "trade_date", "config_version", "code_version",
)
FILL_RESPONSE_FIELDS = (
    "id", "fill_id", "order_id", "broker", "code", "side", "price", "quantity",
    "commission", "tax", "broker_trade_id", "decision_round_id", "fill_round_id",
    "trade_date", "filled_at",
)


@dataclass(frozen=True)
class DeferredExecutionFrame:
    captured_at: datetime
    response_json: str


def capture_deferred_execution_frame(*, account_id, account_name, trade_date,
                                     quote_context, evaluated_at, outcome):
    """Compatibility entry: retain the original deferred-response payload."""
    return capture_sell_execution_frame(account_id=account_id, account_name=account_name,
        trade_date=trade_date, quote_context=quote_context, evaluated_at=evaluated_at,
        outcome=outcome)


def capture_sell_execution_frame(*, account_id, account_name, trade_date,
                                 quote_context, evaluated_at, outcome,
                                 origin="deferred_reconciliation", position=None,
                                 trigger_reason=None):
    """Freeze reported sell responses, not sell permission or a complete exit cycle.

    No DB, file, risk or broker calls. Existing deferred observations retain their
    exact protocol. Direct submission additionally observes loaded position leaves
    AFTER the response; neither fill source clocks nor a QuoteRound backdate it.
    """
    if origin not in {"deferred_reconciliation", "order_submission"}:
        return None
    at = _now()
    if (type(account_id) is not int or account_id <= 0
            or not isinstance(account_name, str) or not account_name
            or type(trade_date) is not date or trade_date != at.date()
            or _clock(evaluated_at) is None or evaluated_at.date() != trade_date
            or evaluated_at > at or type(quote_context) is not dict):
        return None
    committed = _clock(quote_context.get("committed_at"))
    round_id = quote_context.get("round_id")
    if (not isinstance(round_id, str) or not round_id or committed is None
            or committed.date() != trade_date or committed > at):
        return None
    if type(outcome) is not dict or type(outcome.get("order")) is not dict:
        return None
    order = outcome["order"]
    code = order.get("code")
    if (order.get("broker") != "paper" or order.get("account_id") != account_name
            or order.get("side") != "sell" or not isinstance(code, str)
            or re.fullmatch(r"[0-9]{6}", code) is None
            or not isinstance(order.get("order_id"), str) or not order["order_id"]):
        return None
    fills = outcome.get("fills")
    # Missing is not an empty execution list / zero fills.
    if type(fills) is not list or len(fills) > 64 or any(type(fill) is not dict for fill in fills):
        raise ValueError("execution_fill_response_invalid_or_unbounded")
    deferred = (outcome.get("deferred") if origin == "deferred_reconciliation"
                and type(outcome.get("deferred")) is dict else {})
    candidate = deferred.get("candidate") if type(deferred.get("candidate")) is dict else {}
    trigger = _leaf(candidate.get("exit_trigger_reason"))
    position_id = deferred.get("position_id")
    if type(position_id) is not int or position_id <= 0:
        position_id = None
    payload = {
        "schema_version": EXECUTION_SCHEMA, "trade_date": trade_date.isoformat(),
        "account": {"id": account_id, "name": account_name},
        "position_id_from_original_metadata": position_id,
        "order_response": {key: _leaf(order.get(key)) for key in ORDER_RESPONSE_FIELDS},
        "fill_responses": [{key: _leaf(fill.get(key)) for key in FILL_RESPONSE_FIELDS} for fill in fills],
        "outcome_event": _leaf(outcome.get("event")),
        "original_exit_trigger": trigger,
        "original_exit_trigger_basis": "original_decision" if isinstance(trigger, str) and trigger else "legacy_missing",
        "round_context": {key: _leaf(quote_context.get(key)) for key in (
            "round_id", "as_of_at", "committed_at", "code_version", "config_version")},
        "strategy_evaluated_at": evaluated_at.isoformat(),
        "strategy_clock_basis": "original_quote_round_parameter_not_response_observation_time",
        "response_observed_at": at.isoformat(),
        "observation_scope": "reported_deferred_sell_outcome_not_position_revision_or_exit_cycle",
        "position_state_before_execution": None, "position_state_after_execution": None,
        "position_fully_closed": None, "commit_known_at": None,
        "historical_fill_observed_at": None, "execution_permission": None,
        "corporate_action_and_price_basis": "unproven",
        "replay_ready": False, "can_submit_orders": False,
        "historical_before_first_observation": "unknown_not_backfilled",
    }
    if origin == "order_submission":
        observed_position = {key: _leaf(getattr(position, key, None)) for key in POSITION_FIELDS}
        if (type(observed_position["id"]) is not int or observed_position["id"] <= 0
                or type(observed_position["account_id"]) is not int
                or observed_position["account_id"] != account_id or observed_position["code"] != code):
            return None
        # A loaded row after submit_order is an actual observed state, not a
        # reconstructed prior revision or proof of an entire liquidation cycle.
        payload.update({
            "submission_origin": origin,
            "submission_position_id": observed_position["id"],
            "position_state_after_execution": observed_position,
            "position_state_after_response_hash": _hash(observed_position),
            "position_revision_basis": "loaded_state_after_response_not_trade_derived_revision",
            "position_state_after_response_observed_at": at.isoformat(),
            "original_exit_trigger": _leaf(trigger_reason),
            "original_exit_trigger_basis": "current_position_scan",
            "observation_scope": "reported_paper_sell_submission_not_complete_exit_cycle",
        })
    return DeferredExecutionFrame(at, _encode(payload))


def _execution_event(frame, staged_at):
    if (not isinstance(frame, DeferredExecutionFrame) or _clock(staged_at) is None
            or staged_at < frame.captured_at):
        raise ValueError("invalid_execution_observation_clock")
    payload = json.loads(frame.response_json)
    if type(payload) is not dict or payload.get("schema_version") != EXECUTION_SCHEMA:
        raise ValueError("invalid_execution_observation_schema")
    stable = {key: value for key, value in payload.items()
              if key not in {"response_observed_at", "position_state_after_response_observed_at"}}
    evidence_hash = _hash(stable)
    payload["evidence_hash"] = evidence_hash
    payload["observation_staged_at"] = staged_at.isoformat()
    return {
        "event_key": "pef-" + evidence_hash, "route_id": EXECUTION_ROUTE_ID,
        "route_version": EXECUTION_SCHEMA, "trade_date": date.fromisoformat(payload["trade_date"]),
        "observed_at": frame.captured_at, "created_at": staged_at,
        "code": payload["order_response"]["code"], "name": None,
        "event_type": EXECUTION_EVENT_TYPE, "status": "observed_unverified",
        "price": None, "assumed_fill_price": None, "change_pct": None,
        "snapshot_json": _encode(payload),
    }


async def append_deferred_execution_frames(db, frames):
    """Append after ordinary reconciliation logs; no extra execution or commit."""
    if not frames:
        return {"status": "not_observed", "attempted": 0}
    try:
        if len(frames) > 256:
            raise ValueError("execution_observation_batch_too_large")
        staged_at = _now()
        rows = [_execution_event(frame, staged_at) for frame in frames]
        return await _append_observation_rows(db, rows)
    except Exception as exc:
        logger.warning("Deferred execution observation unavailable (%s)", type(exc).__name__)
        return {"status": "unavailable", "error_type": type(exc).__name__}

