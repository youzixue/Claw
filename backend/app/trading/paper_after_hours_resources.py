"""Dormant receipt-backed CAS kernel, not a fill authorization or service entry point.

Requires the original typed ledger scope spanning the actual book AND receipt.
Production providers are aggregate-only and cannot satisfy it. Only SQLite is verified. Helpers
never commit/rollback, create accounts, mutate positions or dispatch a broker.
One session-scope CAS serializes ALL its resources; immutable grouped receipts
retain every original external-order slice independently of mutable projections.
"""
import hashlib
import json
import math
import re
from datetime import datetime, time, timedelta
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import (Date, DateTime, Float, Integer, LargeBinary, String,
                        and_, or_, case, cast, func, select, update, text)
from sqlalchemy.dialects.sqlite import insert

from app.data.after_hours import _json
from app.models.paper import PaperAccount, PaperTradeLog
from app.models.trading import (TradeFill, TradeOrder, PaperAfterHoursResourceScope as State,
                                PaperAfterHoursResourceReceipt as Receipt)
from app.trading import paper_after_hours_allocation as allocation
from app.trading.paper_after_hours_execution import (MODE, FILL_VERSION, LEDGER_VERSION,
    PARTIAL_FILL_VERSION, PARTIAL_LEDGER_VERSION)
from app.trading.paper_authorization import _current, paper_transaction_active

from app.trading.paper_after_hours_resource_schema import FULL_PROTOCOL, PARTIAL_PROTOCOL

CONSUMPTION_VERSION = FULL_PROTOCOL
PARTIAL_CONSUMPTION_VERSION = PARTIAL_PROTOCOL  # Dormant finalize kernel; no partial permit/service.
PARTIAL_HISTORY_STRUCTURE_VERSION = "after_hours_partial_history_structure_v1_20261002"
PARTIAL_BOOK_CANDIDATE_VERSION = "after_hours_partial_book_candidate_binding_v1_20261002"
MAX_RECEIPTS, MAX_PAYLOAD_BYTES, MAX_READ_BYTES = 500, 2 * 1024 * 1024, 8 * 1024 * 1024
MAX_GUARD_FIELD_BYTES = 2 * 1024 * 1024  # DDL is not a JSON payload.


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _scope_values(snapshot, account):
    values = {"account_numeric_id": account.id, "account_name": account.account_name,
              "code": snapshot.code, "trade_date": snapshot.day, "source": snapshot.source,
              "source_version": snapshot.version, "session_id": snapshot.session_id}
    key = _digest({**values, "trade_date": snapshot.day.isoformat()})
    return key, values


def _object(text, *, reject_duplicate_keys=False):
    if not isinstance(text, str) or len(text) > MAX_PAYLOAD_BYTES or len(text.encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError("resource_receipt_byte_budget")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("resource_receipt_duplicate_json_key")
            result[key] = value
        return result
    result = json.loads(text, object_pairs_hook=unique if reject_duplicate_keys else None)
    if not isinstance(result, dict):
        raise ValueError("invalid_resource_receipt")
    return result


def _timing_binding(contract, timing, order, *, cutoff):
    allocation._identifier(contract["quote_round_id"])
    source, received, available, dispatch, expiry, end = [
        allocation._clock(contract[key]) for key in ("source_quote_at", "received_at",
            "source_available_at", "dispatch_validated_at", "quote_expires_at", "session_end_at")]
    decision = allocation._clock(contract["decision_at"])
    from app.trading.paper_after_hours_execution import fifo_key
    fifo_key(order)  # Original identity AND accepted/validated/terminal clocks remain mandatory.
    intent = _object(order.risk_json)["paper_after_hours_intent"]
    terminal = allocation._clock(intent["terminal_validated_at"])
    if not order.created_at <= terminal <= dispatch:
        raise ValueError("resource_receipt_original_intent_terminal_or_creation_conflict")
    if allocation._clock(intent["requested_at"]) != decision or decision.date() != order.trade_date or decision > dispatch:
        raise ValueError("resource_receipt_future_or_changed_original_decision")
    lock, before = [allocation._clock(timing[key]) for key in
                    ("lock_acquired_checked_at", "before_mutation_checked_at")]
    max_age = contract["source_max_age_seconds"]
    if (type(max_age) is not int or not 1 <= max_age <= 30
            or end != datetime.combine(order.trade_date, time(15, 30))
            or expiry != min(source + timedelta(seconds=max_age), end)
            or not source <= received <= available <= dispatch <= lock <= before <= cutoff
            or not allocation.clock_valid(source) or not allocation.clock_valid(before)
            or not before < expiry or source.date() != before.date()
            or contract["decision_at"] != order.decision_at.isoformat()
            or contract["original_limit_price"] != order.price or contract["original_quantity"] != order.quantity
            or timing["dispatch_validated_at"] != contract["dispatch_validated_at"]
            or timing["quote_round_id"] != contract["quote_round_id"]
            or timing["quote_expires_at"] != contract["quote_expires_at"]
            or timing["session_end_at"] != contract["session_end_at"]
            or contract["allocation"]["source_quote_at"] != contract["source_quote_at"]
            or contract["allocation"]["source_available_at"] != contract["source_available_at"]
            or allocation._clock(contract["allocation"]["proposed_at"]) > dispatch):
        raise ValueError("resource_receipt_original_clock_or_request_conflict")


def _economic_binding(order, fill, trade, account, contract, timing, *, cutoff,
                      current=False, partial=False, executed=False):
    """Bind row identities; callers certify reads, not raw_json or account names.

    Pure partial candidates and future actual partial contracts have separate
    versions. The latter use the frozen past fee model, never current settings.
    Neither this predicate nor a JSON contract can issue a ledger scope.
    """
    if executed and not partial:
        raise ValueError("partial_execution_binding_requires_partial_protocol")
    _timing_binding(contract, timing, order, cutoff=cutoff)
    intent_account = _object(order.risk_json)["paper_after_hours_intent"].get("numeric_account_id")
    if type(intent_account) is not int or intent_account != account.id:
        raise ValueError("resource_receipt_original_numeric_account_conflict")
    for key in ("commission", "tax", "realized_pnl"):
        value = getattr(trade, key)
        if (value is None and key != "realized_pnl") or (value is not None and
                (type(value) not in (int, float) or not math.isfinite(value)
                 or (key != "realized_pnl" and value < 0))):
            raise ValueError("resource_receipt_invalid_fees")
    raw = _object(fill.raw_json, reject_duplicate_keys=executed)
    expected_request = contract["request_id"] if partial else order.order_id
    expected_quantity = contract["fragment_quantity"] if partial else order.quantity
    before_quantity = contract["cumulative_before"] if partial else 0
    after_quantity = contract["cumulative_after"] if partial else order.quantity
    if partial:
        # Partial coordinates are distinct from the full receipt protocol.
        for value in (account.id, order.id, fill.id, trade.id, trade.amount, expected_quantity,
                      contract["fragment_index"], contract["original_quantity"],
                      trade.account_id, order.quantity):
            allocation._integer(value)
        for value in (order.filled_quantity, before_quantity, after_quantity):
            allocation._integer(value, zero=True)
        allocation._price(trade.price)
        allocation._price(contract["original_limit_price"])
        from app.trading.paper_after_hours_execution import limit_compatible
        if order.quantity % 100 or not limit_compatible(order.side, order.price, fill.price):
            raise ValueError("partial_book_limit_or_original_quantity_conflict")
        for key in ("commission", "tax", "realized_pnl"):
            value = getattr(fill, key)
            if (value is None and key != "realized_pnl") or (value is not None and
                    (type(value) not in (int, float) or not math.isfinite(value)
                     or (key != "realized_pnl" and value < 0))):
                raise ValueError("partial_book_candidate_fill_fee_type_or_value_conflict")
        from app.trading import paper_after_hours_execution as execution
        if (expected_request != execution._partial_request_identifier(account.id, contract["allocation"])
                or not before_quantity + expected_quantity == after_quantity <= order.quantity
                or contract["remaining_quantity_after"] != order.quantity - after_quantity
                or len(expected_request) > 35 or len(fill.fill_id) > 40
                or contract["expected_fill_id"] != "fill-" + expected_request):
            raise ValueError("partial_book_candidate_fragment_identity_or_remaining_conflict")
    if (order.order_type != MODE or order.broker != "paper" or fill.broker != "paper"
            or order.account_id != account.account_name or trade.account_id != account.id
            or fill.order_id != order.order_id or fill.fill_id != "fill-" + expected_request
            or fill.broker_trade_id != str(trade.id)
            or not (order.code == fill.code == trade.code == contract["code"])
            or not (order.side == fill.side == trade.trade_type == contract["side"])
            or not (order.trade_date == fill.trade_date == trade.trade_time.date())
            or not order.created_at <= trade.trade_time
            or trade.trade_time != fill.filled_at or trade.trade_time.isoformat() != timing["before_mutation_checked_at"]
            or fill.quantity != trade.amount or fill.quantity != expected_quantity
            or type(fill.quantity) is not int or fill.quantity < 100 or fill.quantity % 100
            or fill.price != trade.price or fill.price != contract["fill_price"]
            or allocation._price(fill.price) != allocation._price(contract["allocation"]["fixed_price"])
            or any(getattr(fill, key) != getattr(trade, key) for key in ("commission", "tax", "realized_pnl"))
            or not trade.decision_round_id or not trade.fill_round_id
            or contract["quote_round_id"] != fill.fill_round_id
            or fill.decision_round_id != trade.decision_round_id
            or order.decision_round_id != trade.decision_round_id or fill.fill_round_id != trade.fill_round_id
            or trade.strategy_version != order.strategy_version
            or trade.signal_id != (order.signal_id or order.order_id)
            or (not current and not (after_quantity <= order.filled_quantity <= order.quantity))
            or (current and (order.status not in ({"submitted", "partial"} if partial else {"submitted"})
                             or order.filled_quantity != before_quantity))):
        raise ValueError("resource_receipt_economic_identity_conflict")
    for key in ("id", "code", "trade_type", "price", "amount", "commission", "tax",
                "realized_pnl", "signal_id", "strategy_version", "decision_round_id", "fill_round_id"):
        if raw.get(key) != getattr(trade, key):
            raise ValueError("resource_receipt_raw_book_identity_conflict")
    from app.trading import paper_after_hours_execution as execution
    contract_version = (PARTIAL_FILL_VERSION if executed else execution.PARTIAL_CANDIDATE_VERSION) if partial else FILL_VERSION
    guard_version = (PARTIAL_LEDGER_VERSION if executed else execution.PARTIAL_CLOCK_VERSION) if partial else LEDGER_VERSION
    if (allocation._clock(raw.get("trade_time")) != trade.trade_time
            or raw.get("after_hours_fixed_execution") != contract or raw.get("ledger_execution_timing") != timing
            or raw.get("immediate_execution_evidence") is not None or raw.get("pending_execution_timing") is not None
            or contract.get("contract_version") != contract_version
            or contract.get("status") != ("validated_candidate" if partial and not executed else "validated")
            or contract.get("mandatory") is not True or contract.get("simulation_only") is not True
            or type(contract.get("account_numeric_id")) is not int
            or contract.get("account_numeric_id") != account.id or contract.get("account_id") != account.account_name
            or contract.get("order_id") != order.order_id or contract.get("request_id") != expected_request
            or contract.get("fragment_quantity" if partial else "filled_quantity") != trade.amount
            or timing.get("guard_version") != guard_version or timing.get("status") != "validated"
            or timing.get("input_contract_version") != contract_version):
        raise ValueError("resource_receipt_typed_binding_conflict")
    if partial:
        preview = contract["fee_preview"]
        expected_preview = execution._partial_fee_preview(contract["allocation"],
            frozen_model=preview["model"] if executed else None)
        allocation._integer(raw["id"])
        allocation._integer(raw["amount"])
        allocation._price(raw["price"])
        fragment_keys = ("fragment_index", "cumulative_before", "cumulative_after", "remaining_quantity_after")
        timing_values = {key: contract[key] for key in fragment_keys}
        timing_values.update(request_id=expected_request, fragment_quantity=expected_quantity)
        local_keys = ("local_intent_hash", "local_acceptance_sequence", "local_accepted_at")
        if (contract["decision_round_id"] != order.decision_round_id
                or contract["strategy_version"] != order.strategy_version
                or contract["signal_id"] != (order.signal_id or order.order_id)
                or any(_json(contract[key]) != _json(contract["allocation"][key]) for key in local_keys)
                or any(_json(contract[key]) != _json(contract["allocation"][key]) for key in fragment_keys)
                or any(_json(timing.get(key)) != _json(value) for key, value in timing_values.items())
                or timing["input_sha256"] != _digest(contract)
                or contract["dispatch_validated_at"] != contract["allocation"]["proposed_at"]
                or any(type(raw[key]) not in (int, float) for key in ("commission", "tax"))
                or (raw["realized_pnl"] is not None and type(raw["realized_pnl"]) not in (int, float))
                or _json(raw["after_hours_fixed_execution"]) != _json(contract)
                or _json(raw["ledger_execution_timing"]) != _json(timing)
                or _json(preview) != _json(expected_preview)
                or any(contract.get(key) is not False for key in
                ("execution_authorized", "partial_fill_allowed", "ledger_contract_supported", "fees_certified"))
                or timing.get("execution_authorized") is not False
                or timing.get("ledger_contract_supported") is not False
                or timing.get("fees_certified") is not False
                or timing.get("fee_model_sha256") != preview["model_sha256"]
                or preview["commission"] != trade.commission or preview["tax"] != trade.tax
                or preview["model"] != contract["allocation"]["fee_model"]
                or preview["model_sha256"] != _digest(preview["model"])
                or preview["model_sha256"] != contract["allocation"]["fee_model_sha256"]
                or (order.side == "buy" and trade.realized_pnl is not None)
                or (order.side == "sell" and trade.realized_pnl is None)):
            raise ValueError("partial_book_candidate_fees_or_false_authority_conflict")


def _partial_book_candidate_binding(candidate, req, order, fill, trade, account, timing, *,
                                    cutoff, prior_fragments=()):
    """Pure consistency of supplied book candidates, NOT DB/receipt/ledger authority.

    Historical pairs MUST be the complete original-order fill/trade history.
    They and the current rows are still caller views; this does not certify fresh
    database reads, risk, T+1, P&L, a CAS or a durable resource root. No writes.
    Current full receipt scopes/readers deliberately cannot recognize this result.
    """
    from app.trading import paper_after_hours_execution as execution
    try:
        if type(candidate) is not execution._FrozenPartialFixedPriceCandidate:
            raise ValueError("partial_book_candidate_exact_frozen_type_required")
        if not isinstance(prior_fragments, (list, tuple)) or len(prior_fragments) > MAX_RECEIPTS:
            raise ValueError("partial_book_candidate_history_budget")
        if any(not isinstance(pair, (list, tuple)) or len(pair) != 2 for pair in prior_fragments):
            raise ValueError("partial_book_candidate_history_pair_required")
        parts = (candidate.contract_json, candidate.feed_json, candidate.order_json,
                 candidate.priors_json, candidate.local_orders_json, order.risk_json, fill.raw_json)
        parts += tuple(pair[0].raw_json for pair in prior_fragments)
        # Bound all supplied JSON before parsing, including old raw book proofs.
        # This is a parser input budget, not a claim about prior SQL hydration.
        execution._candidate_parts_budget(parts, partial=True)
        cutoff = allocation._clock(cutoff)
        lock, before = map(allocation._clock, (
            timing["lock_acquired_checked_at"], timing["before_mutation_checked_at"]))
        if before > cutoff:
            raise ValueError("partial_book_candidate_future_book_clock")
        checked, _ = execution._validate_partial_candidate_clock(
            candidate, req, phase="lock_acquired", clock=lambda: lock)
        _, replay_timing = execution._validate_partial_candidate_clock(
            candidate, req, phase="before_mutation", lock_checked_at=checked, clock=lambda: before)
        if _json(replay_timing) != _json(timing):
            raise ValueError("partial_book_candidate_clock_diagnostic_changed")
        contract = _object(candidate.contract_json)
        if _json(execution._candidate_order_material(order)) != candidate.order_json:
            raise ValueError("partial_book_candidate_original_state_changed")
        all_priors = json.loads(candidate.priors_json)
        expected = [p for p in all_priors if p["order_id"] == order.order_id]
        if len(prior_fragments) != len(expected) or len(expected) != contract["fragment_index"] - 1:
            raise ValueError("partial_book_candidate_original_history_missing_or_extra")
        fill_ids, fill_row_ids, trade_ids, cumulative = set(), set(), set(), 0
        previous_book = allocation._clock(json.loads(order.risk_json)["paper_after_hours_intent"]["terminal_validated_at"])
        for (previous_fill, previous_trade), proposal in zip(prior_fragments, expected):
            previous_raw = _object(previous_fill.raw_json)
            previous_contract = previous_raw["after_hours_fixed_execution"]
            previous_timing = previous_raw["ledger_execution_timing"]
            if (_json(previous_contract["allocation"]) != _json(proposal)
                    or previous_contract["cumulative_before"] != cumulative
                    or previous_trade.trade_time < previous_book
                    or allocation._clock(previous_contract["dispatch_validated_at"]) < previous_book
                    or previous_trade.trade_time > allocation._clock(contract["dispatch_validated_at"])
                    or previous_timing["input_sha256"] != _digest(previous_contract)):
                raise ValueError("partial_book_candidate_original_history_order_or_contract_conflict")
            _economic_binding(order, previous_fill, previous_trade, account,
                previous_contract, previous_timing, cutoff=cutoff, partial=True)
            cumulative += previous_trade.amount
            previous_book = previous_trade.trade_time
            if (previous_fill.fill_id in fill_ids or previous_fill.id in fill_row_ids
                    or previous_trade.id in trade_ids):
                raise ValueError("partial_book_candidate_reused_actual_receipt")
            fill_ids.add(previous_fill.fill_id)
            fill_row_ids.add(previous_fill.id)
            trade_ids.add(previous_trade.id)
        if cumulative != contract["cumulative_before"]:
            raise ValueError("partial_book_candidate_cumulative_book_conflict")
        _economic_binding(order, fill, trade, account, contract, timing,
                          cutoff=cutoff, current=True, partial=True)
        if (fill.fill_id in fill_ids or fill.id in fill_row_ids or trade.id in trade_ids
                or not previous_book <= trade.trade_time <= cutoff):
            raise ValueError("partial_book_candidate_reused_or_regressed_actual_receipt")
        return {"contract_version": PARTIAL_BOOK_CANDIDATE_VERSION,
            "status": "validated_partial_book_candidate", "order_id": order.order_id,
            "request_id": contract["request_id"], "trade_fill_id": fill.id, "fill_id": fill.fill_id,
            "paper_trade_id": trade.id, "account_numeric_id": account.id,
            "fragment_index": contract["fragment_index"], "fragment_quantity": fill.quantity,
            "cumulative_before": cumulative, "cumulative_after": cumulative + fill.quantity,
            "remaining_quantity_after": order.quantity - cumulative - fill.quantity,
            "fee_model_sha256": contract["fee_preview"]["model_sha256"],
            "prior_original_book_count": len(prior_fragments), "supplied_rows_only": True,
            "database_reads_certified": False, "execution_authorized": False,
            "ledger_contract_supported": False, "durable_resources_certified": False,
            "cash_change_certified": False, "realized_pnl_certified": False}
    except HTTPException as exc:
        raise ValueError("partial_book_candidate_clock_rejected: " + str(exc.detail)) from None
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("partial_book_candidate_invalid: " + str(exc)) from None


def _bounded_leaf(col, *, budget=True):
    kind, stored = col.type, func.typeof(col)
    if isinstance(kind, String):
        limit = min(MAX_PAYLOAD_BYTES, kind.length * 4) if kind.length else MAX_PAYLOAD_BYTES
        # SQLite length(TEXT) stops at NUL. Reject embedded NUL in selected text
        # before transfer so it cannot bypass declared character bounds.
        valid = and_(stored == "text", func.instr(col, func.char(0)) == 0,
                     func.length(cast(col, LargeBinary)) <= limit)
        if kind.length:
            valid = and_(valid, func.length(col) <= kind.length)
    elif isinstance(kind, DateTime):
        valid = and_(stored == "text", func.length(cast(col, LargeBinary)) <= 32)
    elif isinstance(kind, Date):
        valid = and_(stored == "text", func.length(cast(col, LargeBinary)) <= 10)
    elif isinstance(kind, Integer):
        valid = stored == "integer"
    elif isinstance(kind, Float):
        valid = stored.in_(("integer", "real"))
    else:
        raise ValueError("resource_book_unverified_projection_type")
    allowed = or_(col.is_(None), valid) if col.nullable else valid
    guarded = and_(budget, allowed)
    return case((guarded, col), else_=None), case((guarded, 0), else_=1)


def _fixed_receipt_marker():
    # SQL JSON inspection, not unbounded Python materialization/parse. Invalid
    # old JSON keeps the old fail-closed path; a non-null AH key cannot spoof limit.
    kind = case((func.json_valid(TradeFill.raw_json),
                 func.json_type(TradeFill.raw_json, "$.after_hours_fixed_execution")), else_=None)
    return or_(TradeOrder.order_type == MODE, and_(kind.is_not(None), kind != "null"))


def _fixed_identity_leaf(key, maximum):
    value = case((func.json_valid(TradeFill.raw_json),
                  func.json_extract(TradeFill.raw_json, "$.after_hours_fixed_execution." + key)), else_=None)
    valid = and_(func.typeof(value) == "text", func.length(value) <= maximum,
                 func.length(cast(value, LargeBinary)) <= maximum * 4)
    return case((valid, value), else_=None)


def _recognition_leaf(col, *, fixed):
    # AH raw/proof are fetched ONLY by the bounded durable root reader. Metadata
    # has declared-size/type SQL gates; the ordinary legacy path is unchanged.
    if col.key in {"raw_json", "risk_json"}:
        return case((fixed, None), else_=col)
    bounded, _ = _bounded_leaf(col)
    return case((fixed, bounded), else_=col)


async def _book_row(db, model, predicate, *, max_text_bytes=MAX_READ_BYTES, include_proofs=True):
    # SQLite does not enforce VARCHAR lengths or numeric affinity. Bound EVERY
    # selected leaf, not only JSON, and never hydrate unrelated TEXT columns.
    fields = {
        TradeOrder: ("id", "order_id", "order_type", "broker", "account_id", "code", "side", "price",
            "quantity", "status", "filled_quantity", "trade_date", "risk_json", "strategy_version",
            "signal_id", "decision_round_id", "decision_at", "created_at"),
        TradeFill: ("id", "order_id", "broker", "fill_id", "broker_trade_id", "code", "side",
            "trade_date", "filled_at", "quantity", "price", "commission", "tax", "realized_pnl",
            "decision_round_id", "fill_round_id", "raw_json"),
        PaperTradeLog: ("id", "account_id", "code", "trade_type", "price", "amount", "trade_time",
            "commission", "tax", "realized_pnl", "signal_id", "strategy_version", "decision_round_id", "fill_round_id"),
        Receipt: ("id", "allocation_id", "scope_key", "trade_fill_id", "paper_trade_id", "order_id",
            "protocol_version", "payload_json", "content_hash", "recorded_at"),
        State: ("scope_key", "account_numeric_id", "account_name", "code", "trade_date", "source",
            "source_version", "session_id", "revision", "terminal_sequence", "lifecycle_prefix_hash",
            "source_quote_at", "source_available_at", "checked_at"),
    }[model]
    if not include_proofs:
        # Structural reads certify neither raw execution nor original intent. Do not
        # fetch/parse unrelated proof text or use it as authority for these results.
        fields = tuple(key for key in fields if key not in {"raw_json", "risk_json"})
    leaves = [getattr(model, key) for key in fields]
    # This SQL count includes strings AND the raw date/time text before SQLAlchemy
    # converts it. Oversized/ill-typed leaves are NULLed in SQL, never returned.
    row_bytes = sum(case((func.typeof(col) == "text",
                         func.length(cast(col, LargeBinary))), else_=0) for col in leaves)
    budget = row_bytes <= max_text_bytes
    columns, invalid = [], []
    for key, col in zip(fields, leaves):
        bounded, bad = _bounded_leaf(col, budget=budget)
        columns.append(bounded.label(key))
        invalid.append(bad)
    rows = (await db.execute(select(*columns, row_bytes.label("_read_text_bytes"),
        sum(invalid).label("_invalid_leaves")).where(predicate).limit(2))).mappings().all()
    if len(rows) != 1:
        return None
    row = SimpleNamespace(**rows[0])
    if row._invalid_leaves or row._read_text_bytes > max_text_bytes:
        raise ValueError("resource_book_text_byte_budget_or_invalid_leaf")
    for key in {"risk_json", "raw_json", "payload_json"}.intersection(fields):
        value = getattr(row, key)
        if not isinstance(value, str) or len(value.encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError("resource_book_text_byte_budget_or_missing")
    return row


async def _receipt_metadata(db, scope_key, read_budget):
    if type(read_budget) is not int or not 1 <= read_budget <= MAX_READ_BYTES:
        raise ValueError("durable_resource_receipt_read_budget")
    metadata = (await db.execute(select(Receipt.id, func.length(cast(Receipt.payload_json, LargeBinary)))
        .where(Receipt.scope_key == scope_key).order_by(Receipt.id).limit(MAX_RECEIPTS + 1))).all()
    if (len(metadata) > MAX_RECEIPTS or any(n is None or n > MAX_PAYLOAD_BYTES for _, n in metadata)
            or sum(n for _, n in metadata) > read_budget):
        raise ValueError("durable_resource_receipt_read_budget")
    return metadata


async def _durable_priors(db, scope_key, account, *, cutoff, verified_bindings=None,
                         max_text_bytes=None, partial=False, pending_fill_id=None, read_usage=None):
    # Metadata first; then charge each bounded row BEFORE fetching the next.
    # No batch prefetch can consume uncharged header bytes ahead of a book read.
    read_budget = MAX_READ_BYTES if max_text_bytes is None else min(MAX_READ_BYTES, max_text_bytes)
    if type(read_budget) is not int or read_budget < 1:
        raise ValueError("durable_resource_receipt_read_budget")
    metadata = await _receipt_metadata(db, scope_key, read_budget)
    if pending_fill_id is not None:
        if not partial:
            raise ValueError("partial_pending_book_requires_partial_protocol")
        _owned_scope(db, partial=True)  # Only the current typed finalize may exclude its new book.
        allocation._integer(pending_fill_id)
    protocol = PARTIAL_CONSUMPTION_VERSION if partial else CONSUMPTION_VERSION
    priors, byte_count, bindings, totals = [], 0, {}, {}
    for receipt_id, payload_bytes in metadata:
        receipt = await _book_row(db, Receipt, Receipt.id == receipt_id,
                                 max_text_bytes=read_budget - byte_count)
        if receipt is None or len(receipt.payload_json.encode()) != payload_bytes:
            raise ValueError("durable_resource_receipt_disappeared_or_changed")
        byte_count += receipt._read_text_bytes
        payload = _object(receipt.payload_json, reject_duplicate_keys=partial)
        if partial:
            for key in ("account_numeric_id", "trade_fill_id", "paper_trade_id"):
                allocation._integer(payload[key])
        if (_digest(payload) != receipt.content_hash or receipt.protocol_version != protocol
                or payload.get("protocol_version") != protocol
                or payload.get("scope_key") != scope_key or payload.get("account_numeric_id") != account.id
                or payload.get("trade_fill_id") != receipt.trade_fill_id
                or payload.get("paper_trade_id") != receipt.paper_trade_id
                or payload.get("order_id") != receipt.order_id
                or payload["allocation"]["proposal_id"] != receipt.allocation_id):
            raise ValueError("durable_resource_receipt_hash_or_identity")
        book = []
        for model, predicate in ((TradeFill, TradeFill.id == receipt.trade_fill_id),
                (TradeOrder, TradeOrder.order_id == receipt.order_id),
                (PaperTradeLog, PaperTradeLog.id == receipt.paper_trade_id)):
            row = await _book_row(db, model, predicate, max_text_bytes=read_budget - byte_count)
            if row is None:
                raise ValueError("durable_resource_receipt_missing_book")
            byte_count += row._read_text_bytes
            book.append(row)
        fill, order, trade = book
        contract, timing = payload["execution"], payload["ledger_timing"]
        _economic_binding(order, fill, trade, account, contract, timing, cutoff=cutoff,
                          partial=partial, executed=partial)
        if (await db.scalar(select(func.count()).select_from(TradeFill).where(
                TradeFill.broker == "paper", TradeFill.broker_trade_id == str(trade.id)))) != 1:
            raise ValueError("durable_resource_receipt_duplicate_book_link")
        if (contract.get("allocation") != payload["allocation"]
                or receipt.recorded_at != fill.filled_at
                or timing.get("input_sha256") != hashlib.sha256(payload["execution_json"].encode()).hexdigest()
                or _object(payload["execution_json"], reject_duplicate_keys=partial) != contract):
            raise ValueError("durable_resource_receipt_original_contract_conflict")
        if partial:
            intent = _object(order.risk_json, reject_duplicate_keys=True)["paper_after_hours_intent"]
            identity = allocation._partial_identity(order, intent)
            if (any(_json(payload["allocation"].get(key)) != _json(value) for key, value in identity.items())
                    or allocation._integer(intent["original_quantity"]) != order.quantity
                    or allocation._price(intent["original_limit_price"]) != allocation._price(order.price)):
                raise ValueError("durable_partial_changed_original_intent")
            coordinates = ("fragment_index", "original_quantity", "cumulative_before",
                           "cumulative_after", "remaining_quantity_after")
            for key in coordinates:
                allocation._integer(payload[key], zero=key in {
                    "cumulative_before", "cumulative_after", "remaining_quantity_after"})
                if _json(payload[key]) != _json(contract[key]):
                    raise ValueError("durable_partial_top_level_coordinate_conflict")
            if (payload["request_id"] != contract["request_id"]
                    or payload["allocation_id"] != receipt.allocation_id
                    or type(payload["fixed_price"]) not in (int, float)
                    or allocation._price(payload["fixed_price"]) != allocation._price(fill.price)):
                raise ValueError("durable_partial_request_or_fixed_price_conflict")
            previous = totals.setdefault(order.order_id, dict(count=0, quantity=0,
                filled_at=order.created_at, order=order))
            if (contract["fragment_index"] != previous["count"] + 1
                    or contract["cumulative_before"] != previous["quantity"]
                    or allocation._clock(contract["dispatch_validated_at"]) < previous["filled_at"]
                    or trade.trade_time < previous["filled_at"] or fill.fill_id in bindings
                    or fill.id == pending_fill_id):
                raise ValueError("durable_partial_original_history_chain_conflict")
            previous.update(count=previous["count"] + 1, quantity=previous["quantity"] + trade.amount,
                            filled_at=trade.trade_time, order=order)
        elif order.order_id in bindings:
            raise ValueError("durable_resource_duplicate_full_fill_order")
        bindings[fill.fill_id if partial else order.order_id] = {"order_id": order.order_id, "trade_id": trade.id,
            "trade_fill_id": fill.id, "fill_id": fill.fill_id, "account_numeric_id": account.id,
            "code": trade.code, "side": trade.trade_type, "quantity": trade.amount,
            "price": trade.price, "filled_at": trade.trade_time, "signal_id": trade.signal_id,
            "strategy_version": trade.strategy_version, "decision_round_id": trade.decision_round_id,
            "fill_round_id": trade.fill_round_id, "order_quantity": order.quantity,
            "order_filled_quantity": order.filled_quantity, "order_price": order.price,
            "order_created_at": order.created_at, "order_decision_at": order.decision_at,
            "order_account_name": order.account_id}
        proposal = payload["allocation"]
        if partial and (proposal.get("contract_version") != allocation.PARTIAL_ALLOCATION_PROTOCOL
                or proposal["proposal_id"] != allocation._hash({k:v for k,v in proposal.items() if k != "proposal_id"})):
            raise ValueError("durable_partial_allocation_protocol_or_hash")
        priors.append(proposal)
    if partial:
        for order_id, original in totals.items():
            count = await db.scalar(select(func.count()).select_from(Receipt).where(Receipt.order_id == order_id))
            fills = await db.scalar(select(func.count()).select_from(TradeFill).where(
                TradeFill.order_id == order_id, TradeFill.id != pending_fill_id if pending_fill_id is not None else True))
            order = original["order"]
            if (count != original["count"] or fills != count or order.filled_quantity != original["quantity"]
                    or order.status not in ({"filled"} if order.quantity == original["quantity"] else {"partial", "canceled"})):
                raise ValueError("durable_partial_missing_book_or_original_projection_conflict")
    if verified_bindings is not None:
        verified_bindings.update(bindings)  # Publish only after ALL history validates.
    if read_usage is not None:
        read_usage["text_bytes"] = byte_count
        read_usage["last_book_at"] = max((v["filled_at"] for v in bindings.values()), default=None)
    return priors


async def _durable_partial_priors(db, scope_key, account, *, cutoff, pending_fill_id=None,
                                  max_text_bytes=None, read_usage=None):
    """Future actual-protocol history only; not pure candidates/structural headers."""
    return await _durable_priors(db, scope_key, account, cutoff=cutoff, partial=True,
        pending_fill_id=pending_fill_id, max_text_bytes=max_text_bytes, read_usage=read_usage)


async def _verify_sqlite_guards(db, *, max_text_bytes=None):
    from app.trading.paper_after_hours_resource_schema import sqlite_guards
    if db.bind.dialect.name != "sqlite":
        raise ValueError("resource_unverified_database_dialect")
    def normalize_ddl(statement):
        return re.sub(r"\s+", " ", statement.replace("IF NOT EXISTS ", "")).strip().rstrip(";").lower()
    required = {statement.split("IF NOT EXISTS ", 1)[1].split()[0]: normalize_ddl(statement)
                for statement in sqlite_guards()}
    # Separate schema budget: canonical required DDL plus bounded whitespace
    # allowance. Unrelated triggers are never hydrated into this verifier.
    schema_budget = 4 * sum(len(sql.encode()) for sql in required.values())
    parameters = {f"name{i}": name for i, name in enumerate(required)}
    names = ", ".join(":" + key for key in parameters)
    predicate = f"type='trigger' AND name IN ({names})"
    total_bytes, name_bytes = (await db.execute(text(
        f"SELECT SUM(length(CAST(sql AS BLOB))), SUM(length(CAST(name AS BLOB))) "
        f"FROM sqlite_master WHERE {predicate}"), parameters)).one()
    if (total_bytes is None or name_bytes is None or total_bytes > schema_budget
            or (max_text_bytes is not None and total_bytes + name_bytes > max_text_bytes)):
        raise ValueError("resource_database_guard_schema_byte_budget")
    parameters["field_budget"] = min(MAX_GUARD_FIELD_BYTES, schema_budget)
    installed = dict((await db.execute(text(f"SELECT name, CASE WHEN typeof(sql)='text' "
        f"AND length(CAST(sql AS BLOB)) <= :field_budget THEN sql ELSE NULL END "
        f"FROM sqlite_master WHERE {predicate}"), parameters)).all())
    if any(sql is None for sql in installed.values()):
        raise ValueError("resource_database_guard_schema_byte_budget")
    if any(name not in installed or normalize_ddl(installed[name]) != sql for name, sql in required.items()):
        raise ValueError("resource_database_guards_missing_or_changed")
    return total_bytes + name_bytes


async def _receipt_account_root(db, *, account_numeric_id, code, trade_date, cutoff, shared_text_budget=False):
    from app.api.v1 import paper
    allocation._integer(account_numeric_id)
    if cutoff.date() < trade_date:
        raise ValueError("resource_recognition_future_trade_day")
    schema_bytes = await _verify_sqlite_guards(db,
        max_text_bytes=MAX_READ_BYTES if shared_text_budget else None)
    identity_bytes = schema_bytes if shared_text_budget else 0
    name, invalid_name = _bounded_leaf(PaperAccount.account_name,
        budget=func.length(cast(PaperAccount.account_name, LargeBinary)) <= MAX_READ_BYTES - identity_bytes)
    row = (await db.execute(select(PaperAccount.id, name.label("account_name"),
        invalid_name.label("invalid_name")).where(
        PaperAccount.id == account_numeric_id, PaperAccount.status == "active",
        PaperAccount.account_name.in_(paper.PAPER_ALL_ACCOUNTS)))).one_or_none()
    if row is None:
        raise ValueError("resource_recognition_existing_account_missing")
    if row.invalid_name:
        raise ValueError("resource_recognition_account_text_byte_budget")
    identity_bytes += len(row.account_name.encode()) if shared_text_budget else 0
    account = SimpleNamespace(id=row.id, account_name=row.account_name, _read_text_bytes=identity_bytes)
    active_ids = list((await db.scalars(select(PaperAccount.id).where(
        PaperAccount.account_name == account.account_name, PaperAccount.status == "active").limit(2))).all())
    if active_ids != [account.id]:
        raise ValueError("resource_recognition_ambiguous_account")
    state = await _book_row(db, State, and_(State.account_numeric_id == account.id,
        State.code == code, State.trade_date == trade_date), max_text_bytes=MAX_READ_BYTES - identity_bytes)
    if state is None:
        raise ValueError("resource_recognition_root_missing")
    identity = {key: getattr(state, key) for key in ("account_numeric_id", "account_name", "code",
        "trade_date", "source", "source_version", "session_id")}
    if (state.account_name != account.account_name
            or state.scope_key != _digest({**identity, "trade_date": trade_date.isoformat()})
            or not state.source_quote_at <= state.source_available_at <= state.checked_at <= cutoff):
        raise ValueError("resource_recognition_root_identity_or_clock")
    return account, state


async def _verified_receipt_bindings(db, *, account_numeric_id, code, trade_date, cutoff, partial=False):
    """Read-only durable recognition, NOT broker/ledger/source or future-fill authority.

    Validate the entire account/security/day root before returning ANY trade binding.
    Missing history never releases consumption/quota; no refresh/rebuild/repair writes.
    Catalog freshness is for NEW fills, not retrospective recognition of old receipts.
    """
    cutoff = allocation._clock(cutoff)
    with db.no_autoflush:
        account, state = await _receipt_account_root(db, account_numeric_id=account_numeric_id,
            code=code, trade_date=trade_date, cutoff=cutoff, shared_text_budget=partial is not False)
        # Consumer selection is based on the WHOLE stored root, never a JSON
        # label supplied by a caller. Mixed/unknown/empty roots do not fall back.
        if partial is None:
            count, full_count, partial_count = (await db.execute(select(func.count(),
                func.sum(case((Receipt.protocol_version == CONSUMPTION_VERSION, 1), else_=0)),
                func.sum(case((Receipt.protocol_version == PARTIAL_CONSUMPTION_VERSION, 1), else_=0)))
                .where(Receipt.scope_key == state.scope_key))).one()
            if not 0 < count <= MAX_RECEIPTS or max(full_count, partial_count) != count:
                raise ValueError("resource_recognition_mixed_unknown_or_missing_protocol")
            partial = partial_count == count
        if type(partial) is not bool:
            raise ValueError("resource_recognition_protocol_selector")
        bindings, usage = {}, {}
        priors = await _durable_priors(db, state.scope_key, account, cutoff=cutoff,
            verified_bindings=bindings, max_text_bytes=MAX_READ_BYTES - account._read_text_bytes - state._read_text_bytes,
            partial=partial, read_usage=usage)
        if not priors or state.revision != len(priors) or state.checked_at < max(b["filled_at"] for b in bindings.values()):
            raise ValueError("resource_recognition_missing_consumption")
        previous_source = previous_available = previous_sequence = None
        slices, resource_hashes, resource_count = {}, {}, 0
        prefix_hashes, original_models, scope_price = {}, {}, None
        protocol = allocation.PARTIAL_ALLOCATION_PROTOCOL if partial else allocation.ALLOCATION_PROTOCOL
        for proposal in priors:
            if (proposal.get("contract_version") != protocol
                    or proposal.get("scenario_account") != account.account_name
                    or any(proposal.get(key) != getattr(state, key) for key in
                           ("source", "source_version", "session_id", "code"))
                    or proposal.get("trade_date") != trade_date.isoformat()
                    or proposal.get("proposal_id") != allocation._hash({k: v for k, v in proposal.items()
                                                                         if k != "proposal_id"})):
                raise ValueError("resource_recognition_original_allocation_conflict")
            source, available = map(allocation._clock, (proposal["source_quote_at"], proposal["source_available_at"]))
            sequence = allocation._integer(proposal["terminal_sequence"], zero=True)
            if (not source <= available <= allocation._clock(proposal["proposed_at"]) <= cutoff
                    or (previous_source is not None and (source < previous_source
                        or available < previous_available or sequence < previous_sequence))):
                raise ValueError("resource_recognition_history_clock_or_watermark")
            previous_source, previous_available, previous_sequence = source, available, sequence
            if partial:
                from app.trading.paper_after_hours_execution import _partial_request_identifier
                binding = bindings["fill-" + _partial_request_identifier(account.id, proposal)]
                price = allocation._price(proposal["fixed_price"])
                if (scope_price is not None and price != scope_price
                        or prefix_hashes.setdefault(sequence, proposal["lifecycle_prefix_hash"])
                            != proposal["lifecycle_prefix_hash"]
                        or re.fullmatch(r"[0-9a-f]{64}", proposal["lifecycle_prefix_hash"]) is None
                        or re.fullmatch(r"[0-9a-f]{64}", proposal["source_evidence_hash"]) is None
                        or proposal.get("execution_authorized") is not False
                        or proposal.get("fillable") is not False
                        or original_models.setdefault(proposal["order_id"], _json(proposal["fee_model"]))
                            != _json(proposal["fee_model"])):
                    raise ValueError("partial_recognition_fixed_price_prefix_or_fee_model_conflict")
                scope_price = price
            else:
                binding = bindings[proposal["order_id"]]
            if (proposal["side"] != binding["side"] or allocation._integer(proposal["quantity"]) != binding["quantity"]
                    or allocation._price(proposal["fixed_price"]) != allocation._price(binding["price"])
                    or sum(allocation._integer(p["shares"]) for p in proposal["resources"]) != binding["quantity"]):
                raise ValueError("resource_recognition_slice_quantity_or_price_conflict")
            for piece in proposal["resources"]:
                resource_count += 1
                if resource_count > allocation.MAX_EVENTS:
                    raise ValueError("resource_recognition_slice_budget")
                external_id = allocation._identifier(piece["external_order_id"])
                source_hash = piece["source_order_hash"]
                if (not isinstance(source_hash, str) or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None
                        or resource_hashes.setdefault(external_id, source_hash) != source_hash):
                    raise ValueError("resource_recognition_external_identity_changed")
                start, shares = allocation._integer(piece["offset"], zero=True), allocation._integer(piece["shares"])
                intervals = slices.setdefault(piece["external_order_id"], [])
                if any(max(start, a) < min(start + shares, b) for a, b in intervals):
                    raise ValueError("resource_recognition_overlapping_consumption")
                intervals.append((start, start + shares))
        latest = priors[-1]
        if (state.source_quote_at != previous_source or state.source_available_at != previous_available
                or state.terminal_sequence != previous_sequence
                or state.lifecycle_prefix_hash != latest["lifecycle_prefix_hash"]):
            raise ValueError("resource_recognition_latest_watermark_conflict")
        if partial:
            # A root cannot hide another original's economic facts, including a
            # filled/canceled peer. No status filter may turn those fills into zero.
            original_filter = (TradeOrder.account_id == account.account_name,
                TradeOrder.code == code, TradeOrder.trade_date == trade_date, TradeOrder.order_type == MODE)
            originals = await db.scalar(select(func.count()).select_from(TradeOrder)
                .where(*original_filter, TradeOrder.filled_quantity > 0))
            fills = await db.scalar(select(func.count()).select_from(TradeFill).join(TradeOrder,
                TradeOrder.order_id == TradeFill.order_id).where(*original_filter))
            if originals != len(original_models) or fills != len(priors):
                raise ValueError("partial_recognition_missing_peer_or_unreceipted_book")
            final_state = await _book_row(db, State, State.scope_key == state.scope_key,
                max_text_bytes=MAX_READ_BYTES - account._read_text_bytes - state._read_text_bytes - usage["text_bytes"])
            if final_state is None or vars(final_state) != vars(state):
                raise ValueError("partial_recognition_root_changed_during_read")
        return bindings


async def _verified_partial_receipt_bindings(db, **identity):
    """Actual partial protocol only; readonly CURRENT history, not a fill permit."""
    return await _verified_receipt_bindings(db, **identity, partial=True)


async def _verified_fixed_receipt_bindings(db, **identity):
    """Consumers use fill IDs across exact full/partial roots; no mixed fallback.

    This recognizes stored economic receipts, not source issuance, prospective
    matching, historical account valuations or partial broker/ledger authority.
    The old full-only reader retains its original default and order-ID keys.
    """
    bindings = await _verified_receipt_bindings(db, **identity, partial=None)
    return {binding["fill_id"]: binding for binding in bindings.values()}


async def _partial_receipt_history_structure(db, *, account_numeric_id, code, trade_date, cutoff):
    """Bounded, read-only CURRENT stored partial row structure; NOT fill recognition.

    Whole scope must be partial. No filtering bad/unknown/full rows, no repair, no
    empty-history -> zero assumption. Raw execution/intent, source slices, quota,
    fees/P&L formulas and historical as-of projections are deliberately uncertified.
    """
    cutoff = allocation._clock(cutoff)
    try:
        with db.no_autoflush:
            account, state = await _receipt_account_root(db, account_numeric_id=account_numeric_id,
                code=code, trade_date=trade_date, cutoff=cutoff)
            metadata = await _receipt_metadata(db, state.scope_key, MAX_READ_BYTES - state._read_text_bytes)
            if not metadata or state.revision != len(metadata):
                raise ValueError("partial_history_missing_consumption")
            byte_count, groups, claimed_fills, claimed_trades, claimed_allocations = state._read_text_bytes, {}, set(), set(), set()
            scope_price = None
            for receipt_id, payload_bytes in metadata:
                receipt = await _book_row(db, Receipt, Receipt.id == receipt_id,
                    max_text_bytes=MAX_READ_BYTES - byte_count)
                if receipt is None or len(receipt.payload_json.encode()) != payload_bytes:
                    raise ValueError("partial_history_receipt_missing_or_changed")
                byte_count += receipt._read_text_bytes
                payload = _object(receipt.payload_json, reject_duplicate_keys=True)
                for key in ("account_numeric_id", "trade_fill_id", "paper_trade_id", "fragment_index", "original_quantity"):
                    allocation._integer(payload[key])
                for key in ("cumulative_before", "cumulative_after", "remaining_quantity_after"):
                    allocation._integer(payload[key], zero=True)
                if (receipt.protocol_version != PARTIAL_CONSUMPTION_VERSION
                        or payload["protocol_version"] != receipt.protocol_version
                        or _digest(payload) != receipt.content_hash
                        or payload["scope_key"] != state.scope_key or receipt.scope_key != state.scope_key
                        or payload["account_numeric_id"] != account.id
                        or payload["trade_fill_id"] != receipt.trade_fill_id
                        or payload["paper_trade_id"] != receipt.paper_trade_id
                        or payload["order_id"] != receipt.order_id
                        or payload["allocation_id"] != receipt.allocation_id
                        or re.fullmatch(r"[0-9a-f]{64}", receipt.allocation_id) is None
                        or re.fullmatch(r"afp-[0-9a-f]{31}", payload["request_id"]) is None
                        or receipt.trade_fill_id in claimed_fills or receipt.paper_trade_id in claimed_trades
                        or receipt.allocation_id in claimed_allocations):
                    raise ValueError("partial_history_receipt_protocol_hash_or_identity")
                book = []
                for model, predicate in ((TradeFill, TradeFill.id == receipt.trade_fill_id),
                        (TradeOrder, TradeOrder.order_id == receipt.order_id),
                        (PaperTradeLog, PaperTradeLog.id == receipt.paper_trade_id)):
                    row = await _book_row(db, model, predicate,
                        max_text_bytes=MAX_READ_BYTES - byte_count, include_proofs=False)
                    if row is None:
                        raise ValueError("partial_history_missing_book")
                    byte_count += row._read_text_bytes
                    book.append(row)
                fill, order, trade = book
                quantity = allocation._integer(fill.quantity)
                original = allocation._integer(order.quantity)
                allocation._integer(order.filled_quantity, zero=True)
                price, limit = allocation._price(fill.price), allocation._price(order.price)
                if scope_price is not None and price != scope_price:
                    raise ValueError("partial_history_same_security_fixed_price_conflict")
                scope_price = price
                for row in (fill, trade):
                    for key in ("commission", "tax", "realized_pnl"):
                        value = getattr(row, key)
                        if ((value is None and key != "realized_pnl") or (value is not None
                                and (type(value) not in (int, float) or not math.isfinite(value)
                                     or (key != "realized_pnl" and value < 0)))):
                            raise ValueError("partial_history_invalid_actual_fee_leaf")
                if (order.order_type != MODE or order.broker != "paper" or fill.broker != "paper"
                        or order.account_id != account.account_name or trade.account_id != account.id
                        or not (order.code == fill.code == trade.code == code)
                        or not (order.side == fill.side == trade.trade_type) or order.side not in {"buy", "sell"}
                        or fill.order_id != order.order_id or order.order_id != receipt.order_id
                        or fill.fill_id != "fill-" + payload["request_id"] or fill.broker_trade_id != str(trade.id)
                        or not (order.trade_date == fill.trade_date == trade.trade_time.date() == trade_date)
                        or not order.created_at <= trade.trade_time == fill.filled_at == receipt.recorded_at <= state.checked_at
                        or not allocation.clock_valid(trade.trade_time)
                        or quantity % 100 or original % 100 or trade.amount != quantity
                        or payload["original_quantity"] != original
                        or type(payload["fixed_price"]) not in (int, float)
                        or allocation._price(payload["fixed_price"]) != price
                        or allocation._price(trade.price) != price
                        or (order.side == "buy" and limit < price) or (order.side == "sell" and limit > price)
                        or any(getattr(fill, key) != getattr(trade, key) for key in ("commission", "tax", "realized_pnl"))):
                    raise ValueError("partial_history_actual_book_structure_conflict")
                if await db.scalar(select(func.count()).select_from(TradeFill).where(
                        TradeFill.broker == "paper", TradeFill.broker_trade_id == str(trade.id))) != 1:
                    raise ValueError("partial_history_duplicate_book_link")
                claimed_fills.add(fill.id)
                claimed_trades.add(trade.id)
                claimed_allocations.add(receipt.allocation_id)
                group = groups.setdefault(order.order_id, [])
                group.append((payload, order, fill, receipt))
            summaries = {}
            for order_id, rows in groups.items():
                rows.sort(key=lambda row: row[0]["fragment_index"])
                cumulative, previous_at, fixed_price = 0, None, None
                order = rows[-1][1]
                for index, (payload, row_order, fill, receipt) in enumerate(rows, 1):
                    if (payload["fragment_index"] != index or payload["cumulative_before"] != cumulative
                            or payload["cumulative_after"] != cumulative + fill.quantity
                            or payload["cumulative_after"] > order.quantity
                            or payload["remaining_quantity_after"] != order.quantity - payload["cumulative_after"]
                            or (previous_at is not None and receipt.recorded_at < previous_at)
                            or (fixed_price is not None and fill.price != fixed_price)
                            or vars(row_order) != vars(order)):
                        raise ValueError("partial_history_original_fragment_chain_conflict")
                    cumulative += fill.quantity
                    previous_at, fixed_price = receipt.recorded_at, fill.price
                # Global original-order counts, not a request ID/new scope projection.
                receipts = await db.scalar(select(func.count()).select_from(Receipt).where(Receipt.order_id == order_id))
                fills = await db.scalar(select(func.count()).select_from(TradeFill).where(TradeFill.order_id == order_id))
                if receipts != len(rows) or fills != len(rows):
                    raise ValueError("partial_history_missing_extra_or_cross_scope_original_book")
                status_ok = order.status == "filled" if cumulative == order.quantity else order.status in {"partial", "canceled"}
                if order.filled_quantity != cumulative or not status_ok:
                    raise ValueError("partial_history_current_projection_conflict")
                summaries[order_id] = {"original_quantity": order.quantity, "fragment_count": len(rows),
                    "filled_quantity": cumulative, "remaining_quantity": order.quantity - cumulative,
                    "fixed_price": fixed_price, "current_status": order.status,
                    "last_stored_fill_at": previous_at.isoformat()}
            # A concurrent atomic writer changing the root invalidates the whole read.
            final_state = await _book_row(db, State, State.scope_key == state.scope_key,
                max_text_bytes=MAX_READ_BYTES - byte_count)
            if final_state is None or vars(final_state) != vars(state):
                raise ValueError("partial_history_root_changed_during_read")
            byte_count += final_state._read_text_bytes
            return {"contract_version": PARTIAL_HISTORY_STRUCTURE_VERSION,
                "status": "consistent_partial_receipt_structure", "scope_key": state.scope_key,
                "account_numeric_id": account.id, "receipt_count": len(metadata),
                "orders": summaries, "read_text_bytes": byte_count, "cutoff": cutoff.isoformat(),
                "structure_only": True, "execution_authorized": False, "ledger_contract_supported": False,
                "durable_resource_slices_certified": False, "raw_execution_contract_certified": False,
                "quota_binding_certified": False, "fees_certified": False, "realized_pnl_certified": False,
                "historical_asof_projection_certified": False}
    except (KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise ValueError("partial_history_invalid_structure: " + str(exc)) from None


def _binding_matches_book(binding, trade, fill, order, *, as_of):
    """Compare a caller view to fresh validated DB facts; a dict is NOT authority."""
    if not isinstance(binding, dict) or order is None:
        return False
    expected = {"order_id": order.order_id, "trade_id": trade.id, "trade_fill_id": fill.id,
        "fill_id": fill.fill_id, "account_numeric_id": trade.account_id, "code": trade.code,
        "side": trade.trade_type, "quantity": trade.amount, "price": trade.price,
        "filled_at": trade.trade_time, "signal_id": trade.signal_id, "strategy_version": trade.strategy_version,
        "decision_round_id": trade.decision_round_id, "fill_round_id": trade.fill_round_id,
        "order_quantity": order.quantity, "order_filled_quantity": order.filled_quantity,
        "order_price": order.price, "order_created_at": order.created_at,
        "order_decision_at": order.decision_at, "order_account_name": order.account_id}
    return (binding == expected and order.order_type == MODE and order.broker == fill.broker == "paper"
        and fill.order_id == order.order_id and fill.broker_trade_id == str(trade.id)
        and trade.code == fill.code == order.code and trade.trade_type == fill.side == order.side
        and trade.amount == fill.quantity and trade.price == fill.price
        and trade.trade_time == fill.filled_at <= as_of and fill.trade_date == order.trade_date == trade.trade_time.date()
        and order.strategy_version == trade.strategy_version
        and order.decision_round_id == fill.decision_round_id == trade.decision_round_id
        and fill.fill_round_id == trade.fill_round_id)


def _partial_resource_contract(candidate):
    """Canonical future finalize material, NOT a scope or a ledger permit.

    Only the private partial service may carry this canonical material in its
    exact typed scope. Candidate flags/JSON remain non-authoritative; no public
    matcher or production order-level provider is enabled.
    """
    from app.trading import paper_after_hours_execution as execution
    if type(candidate) is not execution._FrozenPartialFixedPriceCandidate:
        raise ValueError("partial_resource_exact_frozen_candidate_required")
    parts = (candidate.contract_json, candidate.feed_json, candidate.order_json,
             candidate.priors_json, candidate.local_orders_json)
    execution._candidate_parts_budget(parts, partial=True)
    if (candidate.protocol_version != execution.PARTIAL_FROZEN_VERSION
            or candidate.fingerprint != execution._candidate_fingerprint(*parts)):
        raise ValueError("partial_resource_candidate_fingerprint")
    contract = _object(candidate.contract_json, reject_duplicate_keys=True)
    if contract["contract_version"] != execution.PARTIAL_CANDIDATE_VERSION:
        raise ValueError("partial_resource_candidate_protocol")
    return {**contract, "contract_version": PARTIAL_FILL_VERSION, "status": "validated",
            "validation_scope": "future_partial_resource_finalize_not_ledger_permission"}


def _partial_resource_timing(candidate, candidate_timing):
    """Bind a previously validated candidate clock; never mint ledger authority."""
    from app.trading import paper_after_hours_execution as execution
    if (candidate_timing.get("guard_version") != execution.PARTIAL_CLOCK_VERSION
            or candidate_timing.get("input_contract_version") != execution.PARTIAL_CANDIDATE_VERSION
            or candidate_timing.get("input_sha256") != hashlib.sha256(candidate.contract_json.encode()).hexdigest()):
        raise ValueError("partial_resource_candidate_clock_identity")
    return {**candidate_timing, "guard_version": PARTIAL_LEDGER_VERSION,
        "input_contract_version": PARTIAL_FILL_VERSION,
        "input_sha256": _digest(_partial_resource_contract(candidate)),
        "scope": "future_partial_resource_finalize_not_ledger_permission"}


def _owned_scope(db, *, partial=False):
    if not paper_transaction_active(db) or db.bind.dialect.name != "sqlite":
        raise HTTPException(403, "盘后资源消费必须属于受支持的原子paper事务")
    scope = _current(db, "ledger")
    if (scope.request.order_type != MODE or scope.transaction_identity is None
            or scope.transaction_identity is not db.sync_session.get_transaction()
            or scope.resource_consumption_used or not isinstance(scope.ledger_timing, dict)):
        raise HTTPException(403, "盘后资源消费缺少原事务的一次性专用落账授权")
    contract = _object(scope.immediate_evidence_json)
    timing = scope.ledger_timing
    version, guard = (PARTIAL_FILL_VERSION, PARTIAL_LEDGER_VERSION) if partial else (FILL_VERSION, LEDGER_VERSION)
    if partial and scope.immediate_evidence_json != _json(_partial_resource_contract(scope.fixed_candidate)):
        raise HTTPException(403, "分片资源回报必须绑定原精确冻结候选，重贴JSON不是授权")
    if (contract.get("contract_version") != version
            or timing.get("guard_version") != guard
            or timing.get("input_contract_version") != version
            or timing.get("input_sha256") != hashlib.sha256(scope.immediate_evidence_json.encode()).hexdigest()):
        raise HTTPException(403, "普通成交/撮合提案不能授权盘后资源消费")
    return scope, contract, timing


async def _partial_finalize_context(db, *, account, order, fill, candidate, priors, read_bytes):
    """Read ALL originals (including filled/canceled), never supplied zero history."""
    from app.trading import paper_after_hours_execution as execution
    ids = (await db.execute(select(TradeOrder.id).where(
        TradeOrder.account_id == account.account_name, TradeOrder.code == order.code,
        TradeOrder.trade_date == order.trade_date, TradeOrder.order_type == MODE)
        .order_by(TradeOrder.id).limit(allocation.MAX_LOCAL_ORDERS + 1))).scalars().all()
    if not 1 <= len(ids) <= allocation.MAX_LOCAL_ORDERS:
        raise ValueError("partial_finalize_original_context_row_budget")
    context, quantities, counts = [], {}, {}
    for prior in priors:
        name = prior["order_id"]
        quantities[name] = quantities.get(name, 0) + prior["quantity"]
        counts[name] = counts.get(name, 0) + 1
    for numeric_id in ids:
        peer = await _book_row(db, TradeOrder, TradeOrder.id == numeric_id,
                              max_text_bytes=MAX_READ_BYTES - read_bytes)
        if peer is None:
            raise ValueError("partial_finalize_original_context_changed")
        read_bytes += peer._read_text_bytes
        # Count all receipts/fills globally for the original, including other roots
        # and protocols. Only this exact typed scope's freshly flushed fill is excluded.
        receipts = await db.scalar(select(func.count()).select_from(Receipt).where(Receipt.order_id == peer.order_id))
        fills = await db.scalar(select(func.count()).select_from(TradeFill).where(
            TradeFill.order_id == peer.order_id, TradeFill.id != fill.id))
        if (peer.broker != "paper" or receipts != counts.get(peer.order_id, 0)
                or fills != receipts or peer.filled_quantity != quantities.get(peer.order_id, 0)):
            raise ValueError("partial_finalize_original_missing_or_unreceipted_book")
        context.append(peer)
    if set(quantities) - {p.order_id for p in context}:
        raise ValueError("partial_finalize_receipt_original_missing_from_context")
    supplied = json.loads(candidate.local_orders_json)
    materials = {p.order_id: execution._candidate_order_material(p) for p in context}
    if (not isinstance(supplied, list) or len(supplied) != len(context)
            or len({p["order_id"] for p in supplied}) != len(context)
            or any(_json(p) != _json(materials.get(p["order_id"])) for p in supplied)
            or candidate.priors_json != _json(priors)
            or candidate.order_json != _json(execution._candidate_order_material(order))):
        raise ValueError("partial_finalize_complete_original_context_or_history_changed")
    # Preserve the original frozen ordering only AFTER certifying the whole DB set.
    return [next(p for p in context if p.order_id == value["order_id"]) for value in supplied], read_bytes


async def _persist_fill_resources(db, *, order_id, fill_id, feed, partial_candidate=None):
    """Owned-scope finalize; full service path or dormant future partial kernel.

    No commit, rollback, order mutation or broker call.

    The service MUST keep the original typed scope alive through this operation.
    Any failure aborts the whole book/receipt/resource transaction; no local retry.
    """
    try:
        partial = partial_candidate is not None
        scope, contract, timing = _owned_scope(db, partial=partial)
        if partial:
            if scope.fixed_candidate is not partial_candidate:
                raise HTTPException(403, "分片资源回报候选不是原事务冻结对象")
            if _json(feed) != partial_candidate.feed_json:
                raise ValueError("partial_finalize_original_feed_changed")
            # Own a copy of the frozen input. Caller mutation across a DB await
            # must not renew the original candidate expiry or replace its frame.
            feed = _object(partial_candidate.feed_json, reject_duplicate_keys=True)
            timing = dict(timing)
        from app.api.v1 import paper
        if scope.request.account_name not in paper.PAPER_ALL_ACCOUNTS:
            raise HTTPException(403, "盘后资源消费仅限既有常规paper账户")
        now = allocation._clock(paper._public_order_clock())
        snapshot = allocation._verify(feed, now=now)  # Server-owned catalog, never a payload capability.
        if not allocation._clock(timing["before_mutation_checked_at"]) <= now:
            raise ValueError("resource_finalize_clock_rollback")
        try:
            await _verify_sqlite_guards(db)
        except ValueError:
            raise HTTPException(403, "盘后资源消费数据库保护缺失或不匹配") from None
        await db.flush()
        read_bytes = 0
        order = await _book_row(db, TradeOrder, TradeOrder.order_id == order_id)
        if partial and order is not None:
            read_bytes += order._read_text_bytes
        fill = await _book_row(db, TradeFill, TradeFill.fill_id == fill_id,
            max_text_bytes=MAX_READ_BYTES - read_bytes)
        if order is None or fill is None or not str(fill.broker_trade_id).isdigit():
            raise ValueError("resource_finalize_missing_actual_receipt")
        if partial:
            read_bytes += fill._read_text_bytes
        trade = await _book_row(db, PaperTradeLog, PaperTradeLog.id == int(fill.broker_trade_id),
            max_text_bytes=MAX_READ_BYTES - read_bytes)
        if partial and trade is not None:
            read_bytes += trade._read_text_bytes
        if partial:
            name_leaf, name_bad = _bounded_leaf(PaperAccount.account_name)
            accounts = (await db.execute(select(PaperAccount.id, name_leaf.label("account_name"),
                name_bad.label("invalid")).where(PaperAccount.account_name == scope.request.account_name,
                PaperAccount.status == "active").limit(2))).mappings().all()
            if len(accounts) != 1 or accounts[0]["invalid"]:
                raise ValueError("partial_finalize_account_missing_or_invalid")
            read_bytes += len(accounts[0]["account_name"].encode())
            if read_bytes > MAX_READ_BYTES:
                raise ValueError("partial_finalize_total_read_budget")
            accounts = [SimpleNamespace(id=accounts[0]["id"], account_name=accounts[0]["account_name"])]
        else:
            accounts = (await db.execute(select(PaperAccount).where(
                PaperAccount.account_name == scope.request.account_name, PaperAccount.status == "active")
                .limit(2).execution_options(populate_existing=True))).scalars().all()
        if trade is None or len(accounts) != 1:
            raise ValueError("resource_finalize_existing_account_or_book_missing")
        account = accounts[0]
        _economic_binding(order, fill, trade, account, contract, timing, cutoff=now,
                          current=True, partial=partial, executed=partial)
        if (await db.scalar(select(func.count()).select_from(TradeFill).where(
                TradeFill.broker == "paper", TradeFill.broker_trade_id == str(trade.id)))) != 1:
            raise ValueError("resource_finalize_duplicate_book_link")
        req = scope.request
        allocation._integer(req.quantity)
        if allocation._price(req.price) != allocation._price(fill.price):
            raise ValueError("resource_finalize_request_price_changed")
        if (req.order_id != (contract["request_id"] if partial else order.order_id)
                or req.code != order.code or req.side != order.side
                or req.quantity != fill.quantity or req.price != fill.price
                or not req.strategy_version or req.strategy_version != trade.strategy_version
                or req.signal_id != (order.signal_id or order.order_id)
                or req.decision_round_id != fill.decision_round_id or req.fill_round_id != fill.fill_round_id
                or req.filled_at != allocation._clock(contract["dispatch_validated_at"])
                or contract["source_max_age_seconds"] != allocation._PROVIDERS[snapshot.source].max_age_seconds
                or contract["source_quote_at"] != feed["source_quote_at"]
                or contract["source_available_at"] != feed["available_at"]
                or contract["received_at"] != feed["received_at"]
                or contract["quote_expires_at"] != snapshot.expires_at.isoformat()):
            raise ValueError("resource_finalize_request_changed")
        key, values = _scope_values(snapshot, account)
        if snapshot.code != order.code or snapshot.day != order.trade_date:
            raise ValueError("resource_finalize_source_identity")
        if partial:
            state_row = await _book_row(db, State, and_(State.account_numeric_id == account.id,
                State.code == snapshot.code, State.trade_date == snapshot.day),
                max_text_bytes=MAX_READ_BYTES - read_bytes)
            state = vars(state_row) if state_row is not None else None
            if state_row is not None:
                read_bytes += state_row._read_text_bytes
        else:
            state = (await db.execute(select(State.__table__).where(
                State.account_numeric_id == account.id, State.code == snapshot.code,
                State.trade_date == snapshot.day))).mappings().one_or_none()
        if state is not None and state["scope_key"] != key:
            raise ValueError("resource_source_version_or_session_changed_midday")
        usage = {}
        if partial:
            priors = await _durable_partial_priors(db, key, account, cutoff=now,
                pending_fill_id=fill.id, max_text_bytes=MAX_READ_BYTES - read_bytes, read_usage=usage)
            read_bytes += usage["text_bytes"]
        else:
            priors = await _durable_priors(db, key, account, cutoff=now)
        if state is None:
            if priors:
                raise ValueError("resource_scope_missing_with_existing_consumption")
        elif (state["revision"] != len(priors) or any(state[k] != v for k, v in values.items())
                or state["source_quote_at"] > snapshot.source_at
                or state["source_available_at"] > snapshot.available_at
                or snapshot.lifecycle_prefixes.get(state["terminal_sequence"]) != state["lifecycle_prefix_hash"]):
            raise ValueError("resource_scope_watermark_or_missing_consumption")
        proposal = contract["allocation"]
        if partial:
            from app.trading import paper_after_hours_execution as execution
            if (state is not None and (state["checked_at"] > now
                    or usage["last_book_at"] is not None and state["checked_at"] < usage["last_book_at"])):
                raise ValueError("partial_finalize_root_clock_conflict")
            context, read_bytes = await _partial_finalize_context(db, account=account, order=order,
                fill=fill, candidate=partial_candidate, priors=priors, read_bytes=read_bytes)
            if _json(feed) != partial_candidate.feed_json:
                raise ValueError("partial_finalize_original_feed_changed")
            rebuilt = execution._freeze_partial_fill_candidate(feed, order, local_orders=context,
                account_numeric_id=account.id, quote_round_id=contract["quote_round_id"],
                dispatch_at=contract["dispatch_validated_at"], prior_proposals=priors)
            if rebuilt != partial_candidate:
                raise ValueError("partial_finalize_changed_candidate")
            recorded = allocation._clock(timing["before_mutation_checked_at"])
            _, candidate_timing = execution._validate_partial_candidate_clock(partial_candidate, req,
                phase="before_mutation", clock=lambda: recorded,
                lock_checked_at=timing["lock_acquired_checked_at"])
            if _json(_partial_resource_timing(partial_candidate, candidate_timing)) != _json(timing):
                raise ValueError("partial_finalize_original_typed_clock_changed")
        else:
            planned = allocation.propose_allocations(feed, [order], now=allocation._clock(proposal["proposed_at"]),
                scenario_account=account.account_name, prior_proposals=priors)
            if planned["proposals"] != [proposal] or contract["filled_quantity"] != proposal["quantity"]:
                raise ValueError("durable_consumption_changed_original_allocation")
        # Revalidate task/request/physical root after all asynchronous reads.
        if (_owned_scope(db, partial=partial)[0] is not scope
                or partial and (scope.fixed_candidate is not partial_candidate
                    or scope.immediate_evidence_json != _json(contract)
                    or _json(scope.ledger_timing) != _json(timing))):
            raise HTTPException(403, "资源回报原事务授权已改变")
        # All new execution, identity and history checks passed. The permit is consumed
        # BEFORE the first write; catching an error cannot reuse it in this root txn.
        scope.resource_consumption_used = True
        watermark = {"terminal_sequence": snapshot.terminal_sequence,
            "lifecycle_prefix_hash": snapshot.lifecycle_prefixes[snapshot.terminal_sequence],
            "source_quote_at": snapshot.source_at, "source_available_at": snapshot.available_at,
            "checked_at": now}
        if state is None:
            # A concurrent initialization conflict aborts the entire caller transaction.
            # Never REPLACE/upsert an existing pool or locally retry a stale history.
            await db.execute(insert(State).values(scope_key=key, **values, revision=0, **watermark))
            if partial:
                created = await _book_row(db, State, State.scope_key == key,
                                          max_text_bytes=MAX_READ_BYTES - read_bytes)
                if created is None:
                    raise ValueError("partial_finalize_created_scope_missing")
                read_bytes += created._read_text_bytes
                state = vars(created)
            else:
                state = (await db.execute(select(State.__table__).where(State.scope_key == key))).mappings().one()
            if state["revision"] != 0 or any(state[k] != v for k, v in values.items()):
                raise ValueError("resource_scope_creation_race")
        expected = state["revision"]
        changed = await db.execute(update(State).where(State.scope_key == key, State.revision == expected)
                                   .values(revision=expected + 1, **watermark))
        if changed.rowcount != 1:
            raise ValueError("resource_scope_cas_conflict")
        protocol = PARTIAL_CONSUMPTION_VERSION if partial else CONSUMPTION_VERSION
        payload = {"protocol_version": protocol, "scope_key": key,
            "account_numeric_id": account.id, "trade_fill_id": fill.id, "paper_trade_id": trade.id,
            "order_id": order.order_id, "allocation": proposal, "execution": contract,
            "execution_json": scope.immediate_evidence_json, "ledger_timing": dict(timing)}
        if partial:
            payload.update({key: contract[key] for key in ("request_id", "fragment_index",
                "original_quantity", "cumulative_before", "cumulative_after", "remaining_quantity_after")})
            payload.update(fixed_price=contract["fill_price"], allocation_id=proposal["proposal_id"])
        payload_text = _json(payload)
        if len(payload_text.encode()) > MAX_PAYLOAD_BYTES:
            raise ValueError("resource_receipt_byte_budget")
        receipt = Receipt(allocation_id=proposal["proposal_id"], scope_key=key,
            trade_fill_id=fill.id, paper_trade_id=trade.id, order_id=order.order_id,
            protocol_version=protocol, payload_json=payload_text, content_hash=_digest(payload),
            recorded_at=fill.filled_at)
        db.add(receipt)
        await db.flush()
        # Expiry/rollback is checked again after awaits; no physical-commit claim.
        terminal = allocation._clock(paper._public_order_clock())
        if terminal < now:
            raise ValueError("resource_finalize_clock_rollback")
        allocation._verify(feed, now=terminal)
        # The permit has been consumed; _current still verifies identity/epoch.
        if (_current(db, "ledger") is not scope or scope.transaction_identity is not db.sync_session.get_transaction()
                or partial and (scope.fixed_candidate is not partial_candidate
                    or scope.immediate_evidence_json != _json(contract)
                    or _json(scope.ledger_timing) != _json(timing))):
            raise HTTPException(403, "资源回报终验不属于原事务")
        if partial:
            if not execution._partial_fee_parameters_match(contract["fee_preview"]):
                raise ValueError("partial_finalize_fee_model_changed_at_terminal")
            # No replay/hash after this last wall-clock sample. This still does
            # NOT claim a physical commit clock or a settings concurrency lock.
            finished = allocation._clock(paper._public_order_clock())
            if not terminal <= finished < snapshot.expires_at or not allocation.clock_valid(finished):
                raise ValueError("partial_finalize_terminal_expiry_or_rollback")
            terminal = finished
        return {"contract_version": protocol, "receipt_id": receipt.id,
                "scope_key": key, "revision": expected + 1, "allocation_id": proposal["proposal_id"],
                "execution_authorized": False, "terminal_checked_at": terminal.isoformat(),
                "scope": "independent_account_session_resource_cas",
                **({"read_text_bytes": read_bytes, "ledger_contract_supported": False} if partial else {})}
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise HTTPException(409, f"盘后资源回报验收失败: {exc}") from None


async def _persist_partial_fill_resources(db, *, candidate, order_id, fill_id, feed):
    """Private partial finalize; cannot issue a scope or mutate book/order.

    The integrating service must keep its original exact partial scope alive,
    stage actual book/fill, call this kernel, then CAS the original order in ONE transaction.
    Helper errors must roll back that entire transaction, never retry locally.
    """
    from app.trading import paper_after_hours_execution as execution
    if type(candidate) is not execution._FrozenPartialFixedPriceCandidate:
        raise HTTPException(403, "分片资源消费只接受原精确冻结候选")
    return await _persist_fill_resources(db, order_id=order_id, fill_id=fill_id,
                                        feed=feed, partial_candidate=candidate)
