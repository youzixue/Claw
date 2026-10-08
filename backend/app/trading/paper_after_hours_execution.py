"""Typed fixed-price intent lifecycle, not continuous-auction matching.

Current public feeds are aggregate-only: NO fill authority is produced. Unknown
external predecessors never equal zero. A real order-level allocator and typed
ledger/receipt contracts must be added together before any fills can be enabled.
"""
import hashlib
import json
import math
from dataclasses import dataclass
from types import SimpleNamespace
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import select

from app.core.trade_calendar import is_official_closed_day
from app.data.after_hours import PROTOCOL, _json
from app.data.sources.after_hours_source import exchange_of, RULE_VERSION
from app.models.governance import TradeCalendarModel
from app.models.stock import StockAfterHoursObservation

MODE = "after_hours_fixed"
CONTRACT = "after_hours_fixed_intent_v1_20261002"
FILL_VERSION = "after_hours_fixed_fill_v1_20261002"
LEDGER_VERSION = "after_hours_fixed_ledger_timing_v1_20261002"
FROZEN_VERSION = "after_hours_frozen_candidate_v1_20261002"
PARTIAL_CANDIDATE_VERSION = "after_hours_partial_candidate_v3_20261002"
PARTIAL_FROZEN_VERSION = "after_hours_partial_frozen_candidate_v3_20261002"
PARTIAL_CLOCK_VERSION = "after_hours_partial_candidate_clock_v3_20261002"
PARTIAL_FEE_MODEL_VERSION = "after_hours_partial_existing_book_fees_v1_20261002"
# Future resource-finalize contracts, NOT accepted/issued by the current ledger.
PARTIAL_FILL_VERSION = "after_hours_partial_fixed_fill_v1_20261002"
PARTIAL_LEDGER_VERSION = "after_hours_partial_ledger_timing_v1_20261002"
MAX_PARTIAL_CANDIDATE_BYTES = 8 * 1024 * 1024  # Five frozen parts, shared total UTF8 bound.
SESSION_START = time(15, 5)
SESSION_END = time(15, 30)


def clock_valid(at):
    return (isinstance(at, datetime) and at.tzinfo is None
            and at.date() >= date(2026, 7, 6) and at.weekday() < 5
            and not is_official_closed_day(at.date())
            and SESSION_START <= at.time() < SESSION_END)


def limit_compatible(side, limit_price, fixed_price):
    limit, fixed = Decimal(str(limit_price)), Decimal(str(fixed_price))
    if not limit.is_finite() or not fixed.is_finite() or min(limit, fixed) <= 0:
        raise ValueError("invalid fixed-price condition")
    if side not in {"buy", "sell"}:
        raise ValueError("invalid order side")
    return limit >= fixed if side == "buy" else limit <= fixed


def original_declaration_parameters(code, side, price, quantity):
    """Conservative LOCAL original-order subset; never a fragment/resource gate.

    SSE 2026 3.7.6/6.7 and SZSE 2026 3.6.7 cap closing-fixed declarations
    at 1,000,000 shares; STAR stocks/depositary receipts start at 200. STAR
    permits one-share increments and verified residual sells below 200, but
    this paper protocol supports neither odd originals nor residual exceptions.
    Ordinary orders and native counterparties do not use this helper.
    """
    from app.trading.paper_after_hours_allocation import _integer, _price

    exchange = exchange_of(code)
    if side not in {"buy", "sell"}:
        raise ValueError("invalid fixed-price declaration side")
    _integer(quantity)
    _price(price)  # Validate before any service float normalization.
    star = code.startswith(("688", "689"))
    minimum = 200 if star else 100
    if not minimum <= quantity <= 1_000_000 or quantity % 100:
        raise ValueError("unsupported original fixed-price declaration quantity")
    return {
        "rule_version": RULE_VERSION, "exchange": exchange,
        "quantity_scope": "original_local_declaration_not_fill_fragment_or_external_resource",
        "exchange_maximum_quantity": 1_000_000,
        "exchange_star_minimum_without_residual_exception": 200 if star else None,
        "paper_minimum_quantity": minimum, "paper_quantity_step": 100,
        "A_share_price_tick": "0.01",
        "odd_original_and_residual_sale_exception_supported": False,
        "eligibility_or_execution_authority": False,
    }


async def intent_evidence(db, cmd, *, accepted_at, validated_at):
    parameters = original_declaration_parameters(cmd.code, cmd.side, cmd.price, cmd.quantity)
    exchange = parameters["exchange"]
    if any(not isinstance(at, datetime) or at.tzinfo is not None for at in (accepted_at, validated_at)):
        raise ValueError("invalid fixed-price intent clocks")
    result = {
        "contract_version": CONTRACT, "mandatory": True, "simulation_only": True,
        "order_type": MODE, "code": cmd.code, "side": cmd.side,
        "account_id": cmd.account_id, "rule_version": RULE_VERSION, "exchange": exchange,
        "accepted_at": accepted_at.isoformat(), "validated_at": validated_at.isoformat(),
        "session_end_at": datetime.combine(accepted_at.date(), SESSION_END).isoformat(),
        "original_limit_price": cmd.price, "original_quantity": cmd.quantity,
        "declaration_parameters": parameters,
        "registration_scope": "manual_1505_to_before_1530_subset_not_full_exchange_declaration_window",
        "allocation_scope": "independent_account_counterfactual_not_twelve_shared_market_pool",
        "matching_capability": "aggregate_only", "external_queue_ahead_shares": None,
        "counterparty_evidence": None, "status": "waiting",
        "reason": "counterparty_or_queue_unknown", "fillable": False,
        "partial_fill_allowed": False, "cash_reserved": False, "position_reserved": False,
        "closing_reference": None,
    }
    if (not clock_valid(accepted_at) or not clock_valid(validated_at)
            or accepted_at.date() != validated_at.date() or validated_at < accepted_at):
        result.update(status="rejected", reason="outside_original_fixed_price_registration_session")
        return result
    calendar = await db.get(TradeCalendarModel, accepted_at.date())
    if calendar is None or calendar.is_trade_day is not True or calendar.session_type != "full":
        result.update(status="rejected", reason="stored_full_trade_day_unknown_or_closed")
        return result
    # The pre-session research baseline is a reference, NEVER official execution
    # close authority. No Kline mixed adjustment, ordinary depth or volume differencing.
    row = await db.scalar(select(StockAfterHoursObservation).where(
        StockAfterHoursObservation.code == cmd.code,
        StockAfterHoursObservation.trade_date == accepted_at.date(),
        StockAfterHoursObservation.stage == "regular_close",
        StockAfterHoursObservation.source == "tencent_close",
        StockAfterHoursObservation.source_version == "tencent_pre_fixed_price_baseline_v1",
        StockAfterHoursObservation.available_at <= accepted_at,
        StockAfterHoursObservation.received_at <= accepted_at,
        StockAfterHoursObservation.recorded_at <= accepted_at,
    ).order_by(StockAfterHoursObservation.available_at.desc(), StockAfterHoursObservation.id.desc()).limit(1))
    if row is not None:
        try:
            p = json.loads(row.payload_json)
            if (row.protocol_version != PROTOCOL or p["protocol"] != PROTOCOL or p["missing"]
                    or p["code"] != cmd.code or p["trade_date"] != accepted_at.date().isoformat()
                    or hashlib.sha256(_json(p).encode()).hexdigest() != row.content_hash
                    or row.source_quote_at is None or row.source_quote_at > row.received_at
                    or row.source_quote_at.date() != accepted_at.date()
                    or not time(15) <= row.source_quote_at.time() < SESSION_START):
                raise ValueError("invalid closing reference")
            price = p["values"]["close_price_1500"]
            result["closing_reference"] = {
                "observation_id": row.id, "content_hash": row.content_hash,
                "price": price, "price_basis": p["values"]["price_basis"],
                "source_quote_at": row.source_quote_at.isoformat(),
                "available_at": row.available_at.isoformat(), "execution_authority": False,
                "limit_compatible": limit_compatible(cmd.side, cmd.price, price),
            }
            if result["closing_reference"]["limit_compatible"] is False:
                result.update(status="rejected", reason="limit_incompatible_with_observed_close_reference")
        except (ValueError, KeyError, TypeError):
            result.update(status="rejected", reason="closing_reference_invalid")
    if result["closing_reference"] is None and result["status"] == "waiting":
        result["reason"] = "official_closing_price_and_counterparty_queue_unknown"
    return result


def fifo_key(order):
    """Local acceptance order, not proof of the external exchange queue position."""
    evidence = json.loads(order.risk_json)["paper_after_hours_intent"]
    if not isinstance(evidence, dict):
        raise ValueError("invalid fixed-price intent identity")
    original_declaration_parameters(order.code, order.side, order.price, order.quantity)
    # Python numeric equality alone permits bool/float aliases in the raw proof.
    original_declaration_parameters(order.code, order.side,
                                    evidence.get("original_limit_price"), evidence.get("original_quantity"))
    if (evidence.get("contract_version") != CONTRACT or evidence.get("order_id") != order.order_id
            or evidence.get("acceptance_sequence") != order.id
            or evidence.get("mandatory") is not True or evidence.get("fillable") is not False
            or evidence.get("simulation_only") is not True or evidence.get("order_type") != MODE
            or evidence.get("account_id") != order.account_id or evidence.get("code") != order.code
            or evidence.get("side") != order.side or evidence.get("original_limit_price") != order.price
            or evidence.get("original_quantity") != order.quantity):
        raise ValueError("invalid fixed-price intent identity")
    requested, accepted, validated, terminal, end = [datetime.fromisoformat(evidence[key]) for key in (
        "requested_at", "accepted_at", "validated_at", "terminal_validated_at", "session_end_at")]
    if (not clock_valid(accepted) or not clock_valid(terminal) or accepted.date() != order.trade_date
            or requested.date() != accepted.date() or not requested <= accepted <= validated <= terminal < end
            or end != datetime.combine(order.trade_date, SESSION_END)
            or type(evidence["acceptance_sequence"]) is not int):
        raise ValueError("invalid fixed-price intent clock")
    # The sequence is allocated under the service lock. Wall-clock rollback
    # between otherwise valid requests must not let a later intent jump ahead.
    return order.id, accepted


def waiting_plan(orders):
    """Stable FIFO diagnostic: aggregates/depth/client metadata can NEVER create fills."""
    active = [order for order in orders if order.order_type == MODE
              and order.broker == "paper" and order.status in {"submitted", "partial"}]
    active.sort(key=fifo_key)
    # Group scopes are independent counterfactual accounts, not mutual counterparties.
    groups, plan = {}, []
    for order in active:
        key = (order.account_id, order.code, order.side, order.trade_date)
        ahead = groups.setdefault(key, [])
        plan.append({"order_id": order.order_id, "status": "waiting",
                     "local_fifo_ahead": list(ahead[:20]), "local_fifo_ahead_count": len(ahead),
                     "local_fifo_ahead_truncated": len(ahead) > 20, "external_fifo_ahead": None,
                     "local_fifo_basis": "service_locked_acceptance_sequence_not_exchange_priority",
                      "original_quantity": order.quantity,
                      "filled_quantity_preserved": order.filled_quantity,
                      "unfilled_quantity": order.quantity - order.filled_quantity,
                      "reason": "counterparty_or_queue_unknown", "fills": []})
        ahead.append(order.order_id)
    return plan


@dataclass(frozen=True, slots=True)
class _FrozenFixedPriceCandidate:
    """Private validation input, NOT a broker/ledger permit or resource reservation."""
    contract_json: str
    feed_json: str
    order_json: str
    priors_json: str
    fingerprint: str
    protocol_version: str = FROZEN_VERSION


@dataclass(frozen=True, slots=True)
class _FrozenPartialFixedPriceCandidate:
    """Pure fragment validation input, deliberately NOT accepted by ledger scopes."""
    contract_json: str
    feed_json: str
    order_json: str
    priors_json: str
    local_orders_json: str
    fingerprint: str
    protocol_version: str = PARTIAL_FROZEN_VERSION


def _candidate_fingerprint(*parts):
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.encode()
        digest.update(str(len(encoded)).encode() + b":" + encoded)
    return digest.hexdigest()


def _bounded_candidate_json(value):
    from app.trading import paper_after_hours_allocation as allocator
    result = _json(value)
    if len(result.encode()) > allocator.MAX_BYTES:
        raise ValueError("fixed_price_candidate_byte_budget")
    return result


def _candidate_parts_budget(parts, *, partial):
    from app.trading import paper_after_hours_allocation as allocator
    total = 0
    for part in parts:
        if not isinstance(part, str) or len(part) > allocator.MAX_BYTES:
            raise ValueError("fixed_price_candidate_byte_budget")
        size = len(part.encode())
        if size > allocator.MAX_BYTES:
            raise ValueError("fixed_price_candidate_byte_budget")
        total += size
    if partial and total > MAX_PARTIAL_CANDIDATE_BYTES:
        raise ValueError("partial_candidate_total_byte_budget")


@dataclass(frozen=True, slots=True)
class _FrozenPaperFeeSettings:
    PAPER_COMMISSION_RATE: float
    PAPER_MIN_COMMISSION: float
    PAPER_STAMP_TAX_RATE: float


def _partial_fee_parameters():
    """Current paper settings only, not a historical or real-broker fee schedule."""
    from app.api.v1 import paper
    keys = ("PAPER_COMMISSION_RATE", "PAPER_MIN_COMMISSION", "PAPER_STAMP_TAX_RATE")
    values = []
    for key in keys:
        value = getattr(paper.settings, key)
        if type(value) not in (int, float):
            raise ValueError("partial_fee_model_invalid_parameter_" + key)
        try:
            value = float(value)
        except OverflowError:
            raise ValueError("partial_fee_model_invalid_parameter_" + key) from None
        if not math.isfinite(value) or value < 0:
            raise ValueError("partial_fee_model_invalid_parameter_" + key)
        if key != "PAPER_MIN_COMMISSION" and value >= 1:
            raise ValueError("partial_fee_model_invalid_rate_" + key)
        values.append(value)
    return tuple(values)


def _partial_fee_preview(proposal, *, frozen_model=None):
    """Reuse book calculators; optional past model does NOT certify its issuance."""
    from app.api.v1 import paper
    from app.trading import paper_after_hours_allocation as allocator
    quantity = allocator._integer(proposal["quantity"])
    if quantity % 100 or proposal["side"] not in ("buy", "sell"):
        raise ValueError("partial_fee_model_invalid_fragment_or_side")
    price = float(allocator._price(proposal["fixed_price"]))
    if frozen_model is None:
        parameters = _partial_fee_parameters()
    else:
        if not isinstance(frozen_model, dict):
            raise ValueError("partial_fee_history_model_missing")
        parameters = tuple(frozen_model[key] for key in
                           ("commission_rate", "minimum_commission", "stamp_tax_rate"))
        if (any(type(value) is not float or not math.isfinite(value) or value < 0 for value in parameters)
                or parameters[0] >= 1 or parameters[2] >= 1):
            raise ValueError("partial_fee_history_model_invalid_parameters")
    value = price * quantity  # Same float notional as the existing book.
    if not math.isfinite(value) or value <= 0:
        raise ValueError("partial_fee_model_nonfinite_notional")
    # Reuse the exact book formulas with this captured model, not global settings
    # read inside the calculators. A temporary change then restore (ABA) must not
    # label a different charge as belonging to these frozen parameters.
    policy = _FrozenPaperFeeSettings(*parameters)
    commission = paper._commission(value, fee_settings=policy)
    tax = paper._stamp_tax(value, fee_settings=policy) if proposal["side"] == "sell" else 0.0
    # Calculator hooks/settings changes cannot mix two fee models in one freeze.
    if frozen_model is None and parameters != _partial_fee_parameters():
        raise ValueError("partial_fee_model_changed_during_freeze")
    cash = -(value + commission) if proposal["side"] == "buy" else value - commission - tax
    if (any(type(n) not in (int, float) or not math.isfinite(n) for n in (commission, tax, cash))
            or commission < 0 or tax < 0):
        raise ValueError("partial_fee_model_invalid_calculation")
    model = {"version": PARTIAL_FEE_MODEL_VERSION, "commission_rate": parameters[0],
        "minimum_commission": parameters[1], "stamp_tax_rate": parameters[2],
        "charging_unit": "each_paper_trade_log_fragment_not_original_order_minimum",
        "rounding": "existing_book_python_round_two_decimals",
        "source": "current_paper_settings_snapshot", "configuration_pit_certified": False,
        "real_broker_schedule_certified": False}
    if frozen_model is not None and _json(model) != _json(frozen_model):
        raise ValueError("partial_fee_history_model_identity_changed")
    return {"status": "modeled_only", "model": model,
        "model_sha256": hashlib.sha256(_json(model).encode()).hexdigest(),
        "fragment_quantity": quantity, "fixed_price": price, "side": proposal["side"],
        "gross_value": value, "commission": commission, "tax": tax,
        "cash_change_modeled": cash, "cash_reserved": False, "book_authority": False,
        "partial_ledger_supported": False}


def _partial_fee_parameters_match(preview):
    # Three bounded numeric reads/comparisons only, no hashing after the final
    # clock sample. This is an observation, NOT a settings concurrency lock.
    model = preview["model"]
    expected = tuple(model[key] for key in ("commission_rate", "minimum_commission", "stamp_tax_rate"))
    return expected == _partial_fee_parameters()


def _partial_request_identifier(account_numeric_id, proposal):
    """Stable fragment key also fits the existing broker's fill- prefix limit."""
    key = {name: proposal[name] for name in ("order_id", "local_intent_hash",
                                           "fragment_index", "cumulative_before")}
    key["account_numeric_id"] = account_numeric_id
    return "afp-" + hashlib.sha256(_json(key).encode()).hexdigest()[:31]


def _candidate_identifier(value, maximum):
    from app.trading import paper_after_hours_allocation as allocator
    result = allocator._identifier(value)
    if len(result) > maximum:
        raise ValueError("fixed_price_candidate_identifier_budget")
    return result


def _freeze_fill_candidate(feed, order, *, account_numeric_id, quote_round_id,
                           dispatch_at, prior_proposals=()):
    """Future service-private full-fill validation; no DB, risk, scope or broker.

    Order/account and priors MUST be read by the future service inside its owned
    transaction. This pure function cannot certify those DB reads or authorize a
    fill. Current server-owned providers are aggregate-only and always reject.
    """
    return _freeze_candidate(feed, order, account_numeric_id=account_numeric_id,
        quote_round_id=quote_round_id, dispatch_at=dispatch_at,
        prior_proposals=prior_proposals, partial=False)


def _freeze_partial_fill_candidate(feed, order, *, local_orders, account_numeric_id,
                                   quote_round_id, dispatch_at, prior_proposals=()):
    """Freeze ALL original local states/history for one fragment, not a ledger permit.

    Supplied states and proposals are NOT actual receipts. Fee previews freeze
    the current paper model only: no actual charge, reservation, T+1, remainder
    cancellation or CAS. Integration must certify the original DB state atomically.
    """
    return _freeze_candidate(feed, order, account_numeric_id=account_numeric_id,
        quote_round_id=quote_round_id, dispatch_at=dispatch_at,
        prior_proposals=prior_proposals, partial=True, local_orders=local_orders)


def _candidate_order_material(order):
    fields = ("id", "order_id", "order_type", "broker", "account_id", "code", "side",
              "price", "quantity", "status", "filled_quantity", "risk_json",
              "decision_round_id", "strategy_version", "signal_id")
    original = {key: getattr(order, key) for key in fields}
    original.update(trade_date=order.trade_date.isoformat(), created_at=order.created_at.isoformat(),
                    decision_at=order.decision_at.isoformat())
    return original


def _candidate_original_binding(order, *, account_numeric_id, snapshot, dispatch):
    from app.api.v1 import paper
    from app.trading import paper_after_hours_allocation as allocator
    if (type(account_numeric_id) is not int or account_numeric_id <= 0
            or order.account_id not in paper.PAPER_ALL_ACCOUNTS):
        raise ValueError("fixed_price_candidate_existing_normal_account_required")
    if (not isinstance(order.risk_json, str)
            or len(order.risk_json.encode()) > allocator.MAX_LOCAL_PROOF_BYTES):
        raise ValueError("fixed_price_candidate_original_proof_byte_budget")
    fifo_key(order)
    proof = json.loads(order.risk_json)["paper_after_hours_intent"]
    decision = allocator._clock(order.decision_at)
    terminal = allocator._clock(proof["terminal_validated_at"])
    if (proof.get("numeric_account_id") != account_numeric_id
            or type(proof.get("numeric_account_id")) is not int
            or decision != allocator._clock(proof["requested_at"])
            or not allocator._clock(order.created_at) <= terminal <= dispatch
            or decision.date() != snapshot.day):
        raise ValueError("fixed_price_candidate_original_account_or_clock")
    return decision


def _freeze_candidate(feed, order, *, account_numeric_id, quote_round_id, dispatch_at,
                      prior_proposals, partial, local_orders=None):
    from app.trading import paper_after_hours_allocation as allocator
    dispatch = allocator._clock(dispatch_at)
    snapshot = allocator._verify(feed, now=dispatch)
    decision = _candidate_original_binding(order, account_numeric_id=account_numeric_id,
        snapshot=snapshot, dispatch=dispatch)
    decision_round = _candidate_identifier(order.decision_round_id, 64)
    round_id = _candidate_identifier(quote_round_id, 64)
    if decision_round == round_id:
        raise ValueError("fixed_price_candidate_decision_and_fill_round_conflict")
    _candidate_identifier(order.order_id, 40)
    _candidate_identifier(order.strategy_version, 64)
    signal = order.signal_id or ""
    if signal:
        _candidate_identifier(signal, 80)
    original = _candidate_order_material(order)
    context_material = None
    if partial:
        if not isinstance(local_orders, (list, tuple)) or not 1 <= len(local_orders) <= allocator.MAX_LOCAL_ORDERS:
            raise ValueError("partial_candidate_local_context_budget")
        context_material = []
        for peer in local_orders:
            _candidate_original_binding(peer, account_numeric_id=account_numeric_id,
                snapshot=snapshot, dispatch=dispatch)
            context_material.append(_candidate_order_material(peer))
        # Whole original context is bounded before planning. A target may not be
        # supplied separately with a different status, remainder or proof.
        _bounded_candidate_json(context_material)
        matches = [p for p in context_material if p["order_id"] == order.order_id]
        if len(matches) != 1 or _json(matches[0]) != _json(original):
            raise ValueError("partial_candidate_target_not_exact_original_context")
        planned = allocator.propose_partial_allocations(feed, local_orders, now=dispatch,
            scenario_account=order.account_id, prior_proposals=prior_proposals)
        selected = [p for p in planned["proposals"] if p["order_id"] == order.order_id]
        if planned["status"] != "proposal_only" or len(selected) != 1:
            raise ValueError("partial_candidate_resources_or_fifo_unavailable")
        # A batch plan is not a receipt for its earlier proposals. Freeze only
        # the first allocatable original on this side; earlier full/partial
        # plans must become certified history before a later order can dispatch.
        first = next(p for p in planned["proposals"] if p["side"] == order.side)
        if first["order_id"] != order.order_id:
            raise ValueError("partial_candidate_earlier_uncommitted_original")
        proposal = selected[0]
    else:
        planned = allocator.propose_allocations(feed, [order], now=dispatch,
            scenario_account=order.account_id, prior_proposals=prior_proposals)
        if len(planned["proposals"]) != 1 or planned["waiting"] or planned["status"] != "proposal_only":
            raise ValueError("fixed_price_candidate_resources_unavailable")
        proposal = planned["proposals"][0]
    preview = None
    if partial:
        preview = _partial_fee_preview(proposal)
        for prior in prior_proposals:
            if prior["order_id"] == order.order_id:
                model = prior.get("fee_model")
                if (model != preview["model"] or not isinstance(model, dict)
                        or allocator._hash(model) != preview["model_sha256"]
                        or prior.get("fee_model_sha256") != preview["model_sha256"]):
                    raise ValueError("partial_fee_original_history_unknown_or_policy_changed")
        # Fee tags belong to the candidate's copy, not the allocator's market
        # contract or client authority. The pure allocator does not certify fees.
        proposal = {**proposal, "fee_model": preview["model"], "fee_model_sha256": preview["model_sha256"]}
        proposal["proposal_id"] = allocator._hash({key: value for key, value in proposal.items()
                                                  if key != "proposal_id"})
    contract = {
        "contract_version": FILL_VERSION, "status": "validated", "mandatory": True,
        "simulation_only": True, "execution_authorized": False,
        "validation_scope": "frozen_candidate_not_risk_or_transaction_or_fill_authority",
        "account_numeric_id": account_numeric_id, "account_id": order.account_id,
        "order_id": order.order_id, "request_id": order.order_id,
        "code": order.code, "side": order.side, "fill_price": float(snapshot.price),
        "filled_quantity": order.quantity, "original_quantity": order.quantity,
        "original_limit_price": order.price, "decision_at": decision.isoformat(),
        "decision_round_id": decision_round, "quote_round_id": round_id,
        "strategy_version": order.strategy_version, "signal_id": signal or order.order_id,
        "source_quote_at": feed["source_quote_at"], "received_at": feed["received_at"],
        "source_available_at": feed["available_at"],
        "source_max_age_seconds": allocator._PROVIDERS[snapshot.source].max_age_seconds,
        "dispatch_validated_at": dispatch.isoformat(), "quote_expires_at": snapshot.expires_at.isoformat(),
        "session_end_at": datetime.combine(snapshot.day, SESSION_END).isoformat(),
        "allocation": proposal,
    }
    if partial:
        # Stable per-original fragment identity, not a frame/quote retry identity.
        # No database uniqueness/actual receipt is certified by this pure key.
        # BrokerFill adds "fill-" to the request; no database field is widened.
        request_id = _partial_request_identifier(account_numeric_id, proposal)
        contract.pop("filled_quantity")
        contract.update(contract_version=PARTIAL_CANDIDATE_VERSION, status="validated_candidate",
            validation_scope="frozen_partial_candidate_not_ledger_or_fee_or_fill_authority",
            request_id=request_id, expected_fill_id="fill-" + request_id, fragment_quantity=proposal["quantity"],
            fragment_index=proposal["fragment_index"], cumulative_before=proposal["cumulative_before"],
            cumulative_after=proposal["cumulative_after"],
            remaining_quantity_after=proposal["remaining_quantity_after"],
            local_acceptance_sequence=proposal["local_acceptance_sequence"],
            local_accepted_at=proposal["local_accepted_at"], local_intent_hash=proposal["local_intent_hash"],
            partial_fill_allowed=False, ledger_contract_supported=False, fees_certified=False,
            fee_preview=preview,
            prior_history_sha256=hashlib.sha256(_bounded_candidate_json(list(prior_proposals)).encode()).hexdigest(),
            local_context_sha256=hashlib.sha256(_bounded_candidate_json(context_material).encode()).hexdigest())
    values = (contract, feed, original, list(prior_proposals))
    if partial:
        values += (context_material,)
    parts = tuple(_bounded_candidate_json(value) for value in values)
    _candidate_parts_budget(parts, partial=partial)
    kind = _FrozenPartialFixedPriceCandidate if partial else _FrozenFixedPriceCandidate
    return kind(*parts, _candidate_fingerprint(*parts))


def _validate_candidate_clock(candidate, req, *, phase, clock,
                              lock_checked_at=None, ledger_timing=None):
    """Synchronous clock predicate, not a permission or production guard branch.

    Replays the original frozen evidence and resamples AFTER expensive validation.
    The integrating service must supply the actual wall clock, retain its typed
    scope, recheck risk/calendar/account and finalize book/receipt/resources.
    JSON alone never opens validate_ledger_clock; its fixed branch additionally
    requires this exact candidate in an original service-owned transaction.
    """
    return _validate_frozen_clock(candidate, req, phase=phase, clock=clock,
        lock_checked_at=lock_checked_at, ledger_timing=ledger_timing, partial=False)


def _validate_partial_candidate_clock(candidate, req, *, phase, clock,
                                       lock_checked_at=None, ledger_timing=None):
    """Fragment-only pure predicate; never accepted by existing ledger scopes."""
    return _validate_frozen_clock(candidate, req, phase=phase, clock=clock,
        lock_checked_at=lock_checked_at, ledger_timing=ledger_timing, partial=True)


def _restore_candidate_order(original):
    from app.trading import paper_after_hours_allocation as allocator
    if not isinstance(original, dict):
        raise ValueError("fixed_price_candidate_original_unknown")
    order = SimpleNamespace(**original)
    order.trade_date = date.fromisoformat(order.trade_date)
    order.created_at, order.decision_at = map(allocator._clock, (order.created_at, order.decision_at))
    return order


def _validate_frozen_clock(candidate, req, *, phase, clock, lock_checked_at, ledger_timing, partial):
    from fastapi import HTTPException
    from app.trading import paper_after_hours_allocation as allocator
    try:
        kind = _FrozenPartialFixedPriceCandidate if partial else _FrozenFixedPriceCandidate
        protocol = PARTIAL_FROZEN_VERSION if partial else FROZEN_VERSION
        if type(candidate) is not kind or candidate.protocol_version != protocol:
            raise ValueError("fixed_price_frozen_candidate_required")
        parts = (candidate.contract_json, candidate.feed_json, candidate.order_json, candidate.priors_json)
        if partial:
            parts += (candidate.local_orders_json,)
        _candidate_parts_budget(parts, partial=partial)
        if _candidate_fingerprint(*parts) != candidate.fingerprint:
            raise ValueError("fixed_price_candidate_fingerprint")
        contract, feed, original, priors = [json.loads(part) for part in parts[:4]]
        order = _restore_candidate_order(original)
        arguments = dict(account_numeric_id=contract["account_numeric_id"],
            quote_round_id=contract["quote_round_id"], dispatch_at=contract["dispatch_validated_at"],
            prior_proposals=priors)
        if partial:
            context = json.loads(candidate.local_orders_json)
            if not isinstance(context, list) or not 1 <= len(context) <= allocator.MAX_LOCAL_ORDERS:
                raise ValueError("partial_candidate_local_context_budget")
            rebuilt = _freeze_partial_fill_candidate(feed, order,
                local_orders=[_restore_candidate_order(value) for value in context], **arguments)
        else:
            rebuilt = _freeze_fill_candidate(feed, order, **arguments)
        if candidate != rebuilt:
            raise ValueError("fixed_price_candidate_changed_or_retyped")
        expected = {"order_id": contract["request_id"], "order_type": MODE, "account_name": order.account_id,
            "code": order.code, "side": order.side, "price": contract["fill_price"],
            "quantity": contract["fragment_quantity"] if partial else order.quantity, "signal_id": contract["signal_id"],
            "strategy_version": order.strategy_version, "decision_round_id": order.decision_round_id,
            "fill_round_id": contract["quote_round_id"],
            "filled_at": allocator._clock(contract["dispatch_validated_at"])}
        # Dataclass annotations do not validate runtime types: 100.0 == 100 and
        # True == 1.0 must not smuggle differently typed execution quantities/prices.
        allocator._integer(req.quantity)
        if (allocator._price(req.price) != allocator._price(contract["fill_price"])
                or any(getattr(req, key) != value for key, value in expected.items())):
            raise ValueError("fixed_price_candidate_request_changed")
        started = allocator._clock(clock())
        snapshot = allocator._verify(feed, now=started)
        dispatch = expected["filled_at"]
        if dispatch > started:
            raise ValueError("fixed_price_candidate_future_dispatch")
        if phase == "lock_acquired":
            if lock_checked_at is not None or ledger_timing is not None:
                raise ValueError("fixed_price_candidate_lock_already_consumed")
        elif phase == "before_mutation":
            lock_checked_at = allocator._clock(lock_checked_at)
            if not dispatch <= lock_checked_at <= started or ledger_timing is not None:
                raise ValueError("fixed_price_candidate_missing_or_repeated_lock")
        else:
            raise ValueError("fixed_price_candidate_unknown_clock_phase")
        contract_hash = hashlib.sha256(candidate.contract_json.encode()).hexdigest()
        # No await, hashing, or replay after this final sample. Date/session/TTL
        # are checked again so synchronous work cannot borrow its starting clock.
        terminal = allocator._clock(clock())
        if not started <= terminal < snapshot.expires_at or not clock_valid(terminal):
            raise ValueError("fixed_price_candidate_terminal_expiry_or_rollback")
        if partial and not _partial_fee_parameters_match(contract["fee_preview"]):
            raise ValueError("partial_fee_model_changed_at_terminal_check")
        if phase == "lock_acquired":
            return terminal, None
        timing = {
            "guard_version": PARTIAL_CLOCK_VERSION if partial else LEDGER_VERSION, "status": "validated",
            "scope": "frozen_partial_clock_not_ledger_authority" if partial else
                     "frozen_candidate_clock_not_scope_or_risk_or_physical_commit",
            "input_contract_version": PARTIAL_CANDIDATE_VERSION if partial else FILL_VERSION,
            "input_sha256": contract_hash,
            "dispatch_validated_at": contract["dispatch_validated_at"],
            "lock_acquired_checked_at": lock_checked_at.isoformat(),
            "before_mutation_checked_at": terminal.isoformat(),
            "quote_round_id": contract["quote_round_id"], "quote_expires_at": contract["quote_expires_at"],
            "session_end_at": contract["session_end_at"], "physical_commit_at": None,
            "execution_authorized": False,
        }
        if partial:
            timing.update(ledger_contract_supported=False, request_id=contract["request_id"],
                fragment_index=contract["fragment_index"], fragment_quantity=contract["fragment_quantity"],
                cumulative_before=contract["cumulative_before"], cumulative_after=contract["cumulative_after"],
                remaining_quantity_after=contract["remaining_quantity_after"],
                fee_model_sha256=contract["fee_preview"]["model_sha256"], fees_certified=False)
        return terminal, timing
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
        raise HTTPException(409, f"盘后专用候选时钟验收失败[{phase}]: {exc}") from None
