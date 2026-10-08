"""Pure fixed-price, order-level counterfactual allocation PROPOSALS.

No broker, database write, reservation, fill certificate or ledger authority is
produced here. Production providers are aggregate-only. A trusted, audited
order-level adapter, atomic consumption and typed ledger contracts are required
TOGETHER before a proposal can ever become a paper fill. Client metadata cannot
upgrade a provider. Fixtures replace the private provider catalog in tests only.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, DecimalException
import hashlib
import re
from types import MappingProxyType

from app.data.after_hours import _json, THS_VERSION
from app.data.sources.after_hours_source import VERSION, RULE_VERSION, exchange_of
from app.trading.paper_after_hours_execution import MODE, CONTRACT, clock_valid, fifo_key, limit_compatible

FEED_PROTOCOL = "after_hours_order_level_replay_v1_20261002"
ALLOCATION_PROTOCOL = "after_hours_allocation_proposal_v1_20261002"
PARTIAL_ALLOCATION_PROTOCOL = "after_hours_partial_allocation_proposal_v1_20261002"
PARTIAL_CANCEL_OBSERVATION_PROTOCOL = "after_hours_partial_cancel_observation_v1_20261002"
METHOD = "complete_initial_queue_and_gapless_order_lifecycle"
MAX_BYTES, MAX_ORDERS, MAX_EVENTS, MAX_LOCAL_ORDERS = 2 * 1024 * 1024, 2000, 10000, 500
MAX_LOCAL_PROOF_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class _Provider:
    version: str
    capability: str = "aggregate_only"
    verification_method: str | None = None
    max_age_seconds: int = 15


# Server-owned audited catalog. NO production order-level provider exists yet.
# It is deliberately not a request/config boolean such as queue_verified=true.
_PROVIDERS = MappingProxyType({
    "sse_fixed_price": _Provider(VERSION),
    "szse_fixed_price": _Provider(VERSION),
    "ths_after_hours": _Provider(THS_VERSION),
})


class EvidenceUnavailable(ValueError):
    pass


class EvidenceInvalid(ValueError):
    pass


def matching_capabilities():
    return {"providers": {name: {"version": p.version, "capability": p.capability}
                         for name, p in _PROVIDERS.items()},
            "order_level_provider_count": sum(p.capability == "order_level" for p in _PROVIDERS.values()),
            "ledger_fill_contract_enabled": False, "execution_authorized": False,
            "reason": "audited_order_level_provider_and_atomic_typed_ledger_required"}


def _identifier(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", value) is None:
        raise EvidenceInvalid("invalid_record_identity")
    return value


def _integer(value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= 2**63 - 1:
        raise EvidenceInvalid("invalid_integer_shares_or_sequence")
    return value


def _clock(value):
    try:
        at = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(at, datetime) or at.tzinfo is not None:
            raise ValueError()
        return at
    except (TypeError, ValueError):
        raise EvidenceInvalid("invalid_shanghai_clock") from None


def _price(value):
    try:
        if isinstance(value, bool):
            raise ValueError()
        price = Decimal(str(value))
        # Multiplication may round a sub-cent value under Decimal context precision.
        # Exact equality after quantize rejects it; serialization must not change price.
        if (not price.is_finite() or price <= 0 or price != price.quantize(Decimal("0.01"))
                or Decimal(str(float(price))) != price):
            raise ValueError()
        return price
    except (DecimalException, ValueError, TypeError):
        raise EvidenceInvalid("invalid_unadjusted_A_share_cent_price") from None


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _declaration_clock(at, exchange, day):
    start = time(9, 30) if exchange == "SSE" else time(9, 15)
    return at.date() == day and (start <= at.time() < time(11, 30)
                                or time(13) <= at.time() < time(15, 30))


@dataclass(frozen=True)
class _ExternalOrder:
    record_id: str
    side: str
    quantity: int
    limit_price: Decimal
    accepted_at: datetime
    sequence: int
    identity_hash: str
    filled: int = 0
    canceled: bool = False

    @property
    def remaining(self):
        return 0 if self.canceled else self.quantity - self.filled


@dataclass(frozen=True)
class _Snapshot:
    source: str
    version: str
    session_id: str
    frame_id: str
    code: str
    day: date
    price: Decimal
    close_available_at: datetime
    source_at: datetime
    available_at: datetime
    expires_at: datetime
    evidence_hash: str
    terminal_sequence: int
    lifecycle_prefixes: MappingProxyType
    lifecycle_clocks: MappingProxyType
    order_lifecycles: MappingProxyType
    orders: tuple[_ExternalOrder, ...]


def _entry(raw, *, exchange, day, close, latest, maximum_sequence):
    if not isinstance(raw, dict) or raw.get("unit") != "shares" or raw.get("side") not in {"buy", "sell"}:
        raise EvidenceInvalid("invalid_order_level_entry_or_unit")
    identity = _identifier(raw.get("record_id"))
    quantity, sequence = _integer(raw.get("quantity")), _integer(raw.get("sequence"))
    accepted, price = _clock(raw.get("accepted_at")), _price(raw.get("limit_price"))
    if (sequence > maximum_sequence or accepted > latest or not _declaration_clock(accepted, exchange, day)
            or not limit_compatible(raw["side"], price, close)):
        raise EvidenceInvalid("invalid_external_acceptance_or_limit")
    return _ExternalOrder(identity, raw["side"], quantity, price, accepted, sequence, _hash(raw))


def _verify(payload, *, now):
    if not isinstance(payload, dict):
        raise EvidenceUnavailable("order_level_feed_unknown")
    profile = _PROVIDERS.get(payload.get("source"))
    if profile is None:
        raise EvidenceUnavailable("provider_not_audited")
    if profile.capability != "order_level":
        raise EvidenceUnavailable("provider_aggregate_only")
    if (payload.get("source_version") != profile.version or profile.verification_method != METHOD
            or payload.get("verification_method") != METHOD or type(profile.max_age_seconds) is not int
            or not 1 <= profile.max_age_seconds <= 30):
        raise EvidenceInvalid("provider_contract_mismatch")
    try:
        if len(_json(payload).encode()) > MAX_BYTES:
            raise EvidenceInvalid("order_level_evidence_byte_budget")
        day = date.fromisoformat(payload["trade_date"])
        now = _clock(now)
        exchange = exchange_of(payload["code"])
        if (payload["protocol"] != FEED_PROTOCOL or payload["session_kind"] != MODE
                or payload["rule_version"] != RULE_VERSION or payload["exchange"] != exchange
                or payload["security_state_1500"] != "trading" or not clock_valid(now) or day != now.date()):
            raise EvidenceInvalid("session_identity_clock_or_eligibility")
        source, received, available = [_clock(payload[key]) for key in (
            "source_quote_at", "received_at", "available_at")]
        expiry = min(source + timedelta(seconds=profile.max_age_seconds), datetime.combine(day, time(15, 30)))
        if not clock_valid(source) or not source <= received <= available <= now < expiry:
            raise EvidenceUnavailable("order_level_feed_expired_or_unordered")
        close = payload["official_close"]
        if (not isinstance(close, dict) or close["price_basis"] != "official_unadjusted_final_1500_close"
                or close["code"] != payload["code"] or close["trade_date"] != day.isoformat()
                or close["exchange"] != exchange):
            raise EvidenceInvalid("official_closing_price_identity_or_basis")
        _identifier(close["record_id"])
        price = _price(close["price"])
        published, close_received, close_available = [_clock(close[key]) for key in (
            "published_at", "received_at", "available_at")]
        if (published.date() != day or published.time() < time(15)
                or not published <= close_received <= close_available <= available):
            raise EvidenceInvalid("official_closing_price_availability")
        initial, events = payload["initial"], payload["events"]
        if (not isinstance(initial, dict) or not isinstance(initial["orders"], list)
                or len(initial["orders"]) > MAX_ORDERS or not isinstance(events, list) or len(events) > MAX_EVENTS):
            raise EvidenceInvalid("order_level_row_budget_or_initial_missing")
        _identifier(initial["record_id"])
        at, sequence = _clock(initial["source_at"]), _integer(initial["sequence"], zero=True)
        if at.date() != day or at.time() != time(15, 5) or at > source:
            raise EvidenceInvalid("complete_session_start_snapshot_required")
        book = {}
        used_sequences = set()
        for raw in initial["orders"]:
            order = _entry(raw, exchange=exchange, day=day, close=price, latest=at, maximum_sequence=sequence)
            if order.record_id in book or order.sequence in used_sequences:
                raise EvidenceInvalid("duplicate_external_order_or_acceptance_sequence")
            book[order.record_id] = order
            used_sequences.add(order.sequence)
        initial_priority = sorted(book.values(), key=lambda o: o.sequence)
        if any(a.accepted_at > b.accepted_at for a, b in zip(initial_priority, initial_priority[1:])):
            raise EvidenceInvalid("external_acceptance_clock_rollback")
        from dataclasses import replace
        # Bind the immutable opening queue, closing-price proof and every event prefix.
        # A later frame may append events, never revise an already observed lifecycle.
        lifecycle_hash = _hash({"initial": initial, "official_close": close,
                                "code": payload["code"], "trade_date": payload["trade_date"],
                                "session_id": payload["session_id"], "exchange": exchange,
                                "rule_version": payload["rule_version"]})
        lifecycle_prefixes = {sequence: lifecycle_hash}
        lifecycle_clocks = {sequence: at}
        # Sparse per-order history is O(orders + events), not a book copy per prefix.
        order_lifecycles = {o.record_id: [(sequence, 0, False)] for o in book.values()}
        record_ids = {initial["record_id"]}
        for event in events:
            if not isinstance(event, dict):
                raise EvidenceInvalid("invalid_lifecycle_event")
            event_id = _identifier(event["record_id"])
            next_sequence, event_at = _integer(event["sequence"]), _clock(event["source_at"])
            if (event_id in record_ids or next_sequence != sequence + 1 or not at <= event_at <= source
                    or not clock_valid(event_at)):
                raise EvidenceInvalid("lifecycle_gap_duplicate_or_clock")
            record_ids.add(event_id)
            if event["kind"] == "add":
                order = _entry(event["order"], exchange=exchange, day=day, close=price,
                               latest=event_at, maximum_sequence=next_sequence)
                if (order.record_id in book or order.sequence != next_sequence
                        or order.accepted_at != event_at or len(book) >= MAX_ORDERS):
                    raise EvidenceInvalid("external_order_reuse_or_bad_add")
                book[order.record_id] = order
                affected = (order.record_id,)
            elif event["kind"] == "cancel":
                order = book[event["order_id"]]
                if _integer(event["quantity"]) != order.remaining:
                    raise EvidenceInvalid("cancel_does_not_equal_live_remainder")
                book[order.record_id] = replace(order, canceled=True)
                affected = (order.record_id,)
            elif event["kind"] == "trade":
                buy, sell = book[event["buy_order_id"]], book[event["sell_order_id"]]
                quantity = _integer(event["quantity"])
                if (buy.side != "buy" or sell.side != "sell" or _price(event["price"]) != price
                        or min(buy.remaining, sell.remaining) < quantity):
                    raise EvidenceInvalid("trade_identity_fixed_price_or_quantity")
                for order in (buy, sell):
                    head = min((o for o in book.values() if o.side == order.side and o.remaining),
                               key=lambda o: (o.accepted_at, o.sequence))
                    if head.record_id != order.record_id:
                        raise EvidenceInvalid("source_trade_violates_time_priority")
                    book[order.record_id] = replace(order, filled=order.filled + quantity)
                affected = (buy.record_id, sell.record_id)
            else:
                raise EvidenceInvalid("unknown_lifecycle_event")
            sequence, at = next_sequence, event_at
            for identity in affected:
                updated = book[identity]
                order_lifecycles.setdefault(identity, []).append((sequence, updated.filled, updated.canceled))
            lifecycle_hash = _hash({"previous": lifecycle_hash, "event": event})
            lifecycle_prefixes[sequence] = lifecycle_hash
            lifecycle_clocks[sequence] = at
        if sequence != _integer(payload["last_sequence"], zero=True):
            raise EvidenceInvalid("missing_terminal_sequence")
        if {o.side for o in book.values() if o.remaining} == {"buy", "sell"}:
            raise EvidenceInvalid("unmatched_crossed_fixed_price_book")
        return _Snapshot(payload["source"], profile.version, _identifier(payload["session_id"]),
            _identifier(payload["frame_id"]), payload["code"], day, price, close_available,
            source, available, expiry, _hash(payload), sequence,
            MappingProxyType(lifecycle_prefixes), MappingProxyType(lifecycle_clocks),
            MappingProxyType({key: tuple(states) for key, states in order_lifecycles.items()}), tuple(book.values()))
    except EvidenceUnavailable:
        raise
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        if isinstance(exc, EvidenceInvalid):
            raise
        raise EvidenceInvalid("malformed_order_level_evidence") from None


def _subtract(interval, consumed):
    segments = [interval]
    for start, end in sorted(consumed):
        segments = [(left, right) for a, b in segments for left, right in (
            (a, min(b, start)), (max(a, end), b)) if left < right]
    return segments


def propose_allocations(payload, orders, *, now, scenario_account, prior_proposals=()):
    """Original full-fill protocol; no partial state or fill authorization."""
    return _propose_allocations(payload, orders, now=now, scenario_account=scenario_account,
                                prior_proposals=prior_proposals, partial=False)


def propose_partial_allocations(payload, orders, *, now, scenario_account, prior_proposals=()):
    """Separate research-only fragment protocol, NOT supported by current ledger.

    All historical local orders and ordered fragment plans must be supplied.
    Claimed filled_quantity must reconcile exactly to their prior plan sums;
    plans are still NOT receipts, reservations or proof of executed economics.
    V1 fragments are conservative 100-share multiples; native resource slices
    may be odd lots. No fees, cash, T+1 or partial cancellation is authorized.
    """
    return _propose_allocations(payload, orders, now=now, scenario_account=scenario_account,
                                prior_proposals=prior_proposals, partial=True)


def _partial_identity(order, intent):
    return {"original_quantity": order.quantity, "original_limit_price": float(_price(order.price)),
            "local_acceptance_sequence": order.id, "local_accepted_at": intent["accepted_at"],
            "local_intent_hash": _hash(intent)}


def _partial_cancel_clock(order, risk, intent, now):
    """Validate a supplied local research witness, NEVER authorize cancellation."""
    proof = risk["paper_after_hours_cancel"]
    canceled = _clock(proof["canceled_at"])
    # Compatibility is ONLY for the prior service's ZERO-fill local cancel
    # record, anchored by fifo_key's original intent. Never relabel partial
    # economics, release history or rewrite this immutable old observation.
    if proof.get("contract_version") == CONTRACT:
        end = datetime.combine(order.trade_date, time(15, 30))
        reason = proof.get("reason")
        if (order.filled_quantity != 0
                or _integer(proof["filled_quantity_preserved"], zero=True) != 0
                or _integer(proof["unfilled_quantity"]) != order.quantity
                or proof.get("simulation_only") is not True
                or proof.get("exchange_cancel_receipt") is not None
                or _clock(proof["original_session_end_at"]) != end
                or not _clock(intent["terminal_validated_at"]) <= canceled <= _clock(now)
                or not (reason == "session_end" and canceled >= end
                    or reason == "user_cancel" and clock_valid(canceled) and canceled.date() == order.trade_date)):
            raise EvidenceInvalid("legacy_zero_cancel_observation_not_bound_or_visible")
        return canceled
    if (proof["contract_version"] != PARTIAL_CANCEL_OBSERVATION_PROTOCOL
            or proof["order_id"] != order.order_id or proof.get("simulation_only") is not True
            or proof.get("exchange_cancel_receipt") is not None
            or _integer(proof["original_quantity"]) != order.quantity
            or _integer(proof["filled_quantity_preserved"], zero=True) != order.filled_quantity
            or _integer(proof["unfilled_quantity"]) != order.quantity - order.filled_quantity
            or not _clock(intent["terminal_validated_at"]) <= canceled <= _clock(now)):
        raise EvidenceInvalid("partial_cancel_observation_not_bound_or_visible")
    # An actual local expiry may be observed after 15:30 or on a later day.
    # This does NOT widen source/matching clocks. Legacy research witnesses with
    # no reason retain their original strict same-day matching-session subset.
    reason = proof.get("reason")
    end = datetime.combine(order.trade_date, time(15, 30))
    if reason == "session_end":
        valid = _clock(proof["original_session_end_at"]) == end and canceled >= end
    else:
        valid = reason in (None, "user_cancel") and clock_valid(canceled) and canceled.date() == order.trade_date
        if reason == "user_cancel":
            valid = valid and _clock(proof["original_session_end_at"]) == end
    if not valid:
        raise EvidenceInvalid("partial_cancel_reason_or_session_clock_conflict")
    if "verified_fill_ids" in proof:
        references = proof["verified_fill_ids"]
        if (not isinstance(references, list) or len(references) > MAX_LOCAL_ORDERS
                or any(not isinstance(value, str) or re.fullmatch(r"fill-afp-[0-9a-f]{31}", value) is None
                       for value in references)
                or len(set(references)) != len(references)
                or (order.filled_quantity == 0 and references)
                or len(references) > order.filled_quantity // 100):
            raise EvidenceInvalid("partial_cancel_actual_fill_reference_type_or_budget")
    return canceled


def _resource_live_at_prefix(snapshot, resource, terminal, source_at, start, end):
    from bisect import bisect_right
    history = snapshot.order_lifecycles[resource.record_id]
    index = bisect_right(history, terminal, key=lambda state: state[0]) - 1
    if index < 0:
        return False
    _, filled, canceled = history[index]
    return (not canceled and filled <= start < end <= resource.quantity
            and resource.sequence <= terminal and resource.accepted_at <= source_at)


def _propose_allocations(payload, orders, *, now, scenario_account, prior_proposals, partial):
    """One replay/interval engine; callers cannot upgrade source or ledger authority."""
    protocol = PARTIAL_ALLOCATION_PROTOCOL if partial else ALLOCATION_PROTOCOL
    result = {"contract_version": protocol, "status": "waiting", "proposals": [],
              "waiting": [], "execution_authorized": False, "fillable": False,
              "simulation_only": True, "partial_fill_allowed": False,
              "scope": "one_independent_account_no_market_impact_counterfactual", "fills": []}
    if partial:
        result.update(partial_proposal_supported=True, minimum_fragment_shares=100,
                      ledger_contract_supported=False)
    try:
        snapshot = _verify(payload, now=now)
        scenario = _identifier(scenario_account)
        if not isinstance(orders, (list, tuple)) or len(orders) > MAX_LOCAL_ORDERS:
            raise EvidenceInvalid("local_order_budget")
        live, order_ids, acceptance_sequences = [], set(), set()
        originals, identities, intents, cancel_clocks = {}, {}, {}, {}
        local_proof_bytes = 0
        for order in orders:
            if order.order_type != MODE or order.broker != "paper" or order.account_id != scenario:
                raise EvidenceInvalid("local_order_mode_broker_or_scenario")
            _identifier(order.order_id)
            if order.order_id in order_ids or order.code != snapshot.code or order.trade_date != snapshot.day:
                raise EvidenceInvalid("local_order_identity_or_duplicate")
            order_ids.add(order.order_id)
            sequence = _integer(order.id)
            if sequence in acceptance_sequences or order.side not in {"buy", "sell"}:
                raise EvidenceInvalid("duplicate_local_acceptance_sequence_or_side")
            acceptance_sequences.add(sequence)
            if not isinstance(order.risk_json, str) or len(order.risk_json) > MAX_LOCAL_PROOF_BYTES:
                raise EvidenceInvalid("local_intent_proof_byte_budget")
            local_proof_bytes += len(order.risk_json.encode())
            if local_proof_bytes > MAX_LOCAL_PROOF_BYTES:
                raise EvidenceInvalid("local_intent_proof_byte_budget")
            filled = _integer(order.filled_quantity, zero=True)
            if _integer(order.quantity) % 100 or _price(order.price) <= 0:
                raise EvidenceInvalid("unsupported_partial_or_invalid_local_order")
            if partial:
                if (filled % 100 or filled > order.quantity
                        or (order.status == "submitted" and filled != 0)
                        or (order.status == "partial" and not 0 < filled < order.quantity)
                        or (order.status == "filled" and filled != order.quantity)
                        or (order.status == "canceled" and filled == order.quantity)
                        or order.status not in ("submitted", "partial", "filled", "canceled")):
                    raise EvidenceInvalid("partial_local_state_or_remainder_invalid")
            elif filled:
                raise EvidenceInvalid("unsupported_partial_or_invalid_local_order")
            fifo_key(order)
            import json
            risk = json.loads(order.risk_json)
            intent = risk["paper_after_hours_intent"]
            if partial and (_integer(intent["original_quantity"]) != order.quantity
                    or _price(intent["original_limit_price"]) != _price(order.price)):
                raise EvidenceInvalid("partial_original_intent_numeric_type_or_value")
            if _clock(intent["terminal_validated_at"]) > _clock(now):
                raise EvidenceInvalid("local_intent_not_yet_visible_at_cutoff")
            originals[order.order_id], intents[order.order_id] = order, intent
            if partial and order.status == "canceled":
                cancel_clocks[order.order_id] = _partial_cancel_clock(order, risk, intent, now)
            identities[order.order_id] = _partial_identity(order, intent) if partial else None
            if order.status == "canceled" or (partial and order.status == "filled"):
                continue
            if order.status != "submitted" and not (partial and order.status == "partial"):
                raise EvidenceInvalid("unsupported_local_order_state")
            live.append(order)
        local_priority = sorted(originals.values(), key=fifo_key) if partial else ()
        history_clock = None
        book = {o.record_id: o for o in snapshot.orders}
        consumed, prior_ids, previously_planned = {}, set(), set()
        prior_totals, prior_counts, prior_clocks = {}, {}, {}
        if (not isinstance(prior_proposals, (list, tuple)) or len(prior_proposals) > MAX_LOCAL_ORDERS
                or len(_json(prior_proposals).encode()) > MAX_BYTES):
            raise EvidenceInvalid("prior_allocation_budget")
        resource_count = 0
        for prior in prior_proposals:
            if not isinstance(prior, dict):
                raise EvidenceInvalid("invalid_prior_allocation")
            content = {key: value for key, value in prior.items() if key != "proposal_id"}
            if (prior.get("proposal_id") != _hash(content) or prior["proposal_id"] in prior_ids
                    or prior["contract_version"] != protocol
                    or prior["scenario_account"] != scenario or prior["session_id"] != snapshot.session_id
                    or prior["code"] != snapshot.code or prior["trade_date"] != snapshot.day.isoformat()
                    or prior["source"] != snapshot.source or prior["source_version"] != snapshot.version
                    or _price(prior["fixed_price"]) != snapshot.price
                    or _clock(prior["proposed_at"]) > _clock(now)):
                raise EvidenceInvalid("prior_allocation_identity_or_integrity")
            proposed_at = _clock(prior["proposed_at"])
            _identifier(prior["order_id"])
            _identifier(prior["frame_id"])
            prior_source, prior_available = [_clock(prior[key]) for key in ("source_quote_at", "source_available_at")]
            prior_terminal = _integer(prior["terminal_sequence"], zero=True)
            if (not clock_valid(proposed_at) or proposed_at.date() != snapshot.day
                    or proposed_at < snapshot.close_available_at
                    or (not partial and prior["order_id"] in previously_planned)
                    or not clock_valid(prior_source) or prior_source.date() != snapshot.day
                    or not prior_source <= prior_available <= proposed_at
                    or prior_source > snapshot.source_at or prior_available > snapshot.available_at
                    or prior_terminal > snapshot.terminal_sequence
                    or snapshot.lifecycle_prefixes.get(prior_terminal) != prior["lifecycle_prefix_hash"]
                    or (prior["frame_id"] == snapshot.frame_id
                        and prior["source_evidence_hash"] != snapshot.evidence_hash)
                    or re.fullmatch(r"[0-9a-f]{64}", prior["source_evidence_hash"]) is None):
                raise EvidenceInvalid("prior_proposal_clock_or_provenance")
            if (prior["side"] not in {"buy", "sell"} or prior.get("execution_authorized") is not False
                    or prior.get("fillable") is not False or not isinstance(prior["resources"], list)):
                raise EvidenceInvalid("prior_proposal_not_a_fill_or_valid_resource_list")
            resource_count += len(prior["resources"])
            if resource_count > MAX_EVENTS:
                raise EvidenceInvalid("prior_resource_row_budget")
            prior_ids.add(prior["proposal_id"])
            previously_planned.add(prior["order_id"])
            quantity = 0
            for piece in prior["resources"]:
                resource = book[piece["external_order_id"]]
                start, shares = _integer(piece["offset"], zero=True), _integer(piece["shares"])
                end = start + shares
                intervals = consumed.setdefault(resource.record_id, [])
                if (piece["source_order_hash"] != resource.identity_hash or end > resource.quantity
                        or resource.side == prior["side"] or resource.accepted_at > proposed_at
                        or (partial and not _resource_live_at_prefix(
                            snapshot, resource, prior_terminal, prior_source, start, end))
                        or any(start < b and a < end for a, b in intervals)):
                    raise EvidenceInvalid("prior_resource_conflict_or_double_consumption")
                intervals.append((start, end))
                quantity += shares
            if quantity != _integer(prior["quantity"]) or quantity % 100:
                raise EvidenceInvalid("prior_resource_quantity_mismatch")
            if partial:
                original = originals[prior["order_id"]]
                before = prior_totals.get(original.order_id, 0)
                after = before + quantity
                index = prior_counts.get(original.order_id, 0) + 1
                previous_clock = prior_clocks.get(original.order_id)
                clocks = (prior_source, prior_available, proposed_at, prior_terminal)
                prior_expiry = min(prior_source + timedelta(seconds=_PROVIDERS[snapshot.source].max_age_seconds),
                                   datetime.combine(snapshot.day, time(15, 30)))
                if (_clock(prior["source_expires_at"]) != prior_expiry or not proposed_at < prior_expiry
                        or _integer(prior["original_quantity"]) != original.quantity
                        or _integer(prior["local_acceptance_sequence"]) != original.id
                        or _price(prior["original_limit_price"]) != _price(original.price)
                        or any(prior.get(key) != value for key, value in identities[original.order_id].items())
                        or prior["side"] != original.side
                        or _integer(prior["fragment_index"]) != index
                        or _integer(prior["cumulative_before"], zero=True) != before
                        or _integer(prior["cumulative_after"]) != after
                        or _integer(prior["remaining_quantity_after"], zero=True) != original.quantity - after
                        or after > original.quantity
                        or not snapshot.close_available_at <= _clock(
                            identities[original.order_id]["local_accepted_at"]) <= prior_source
                        or _clock(json.loads(original.risk_json)["paper_after_hours_intent"][
                            "terminal_validated_at"]) > proposed_at
                        or snapshot.lifecycle_clocks[prior_terminal] > prior_source
                        or (previous_clock and any(a < b for a, b in zip(clocks, previous_clock)))):
                    raise EvidenceInvalid("partial_prior_chain_identity_or_remainder")
                canceled = cancel_clocks.get(original.order_id)
                if canceled is not None and proposed_at == canceled:
                    # Equal wall-clock timestamps do not reverse durable service
                    # ordering. Only a cancellation's exact already-verified fill
                    # reference may establish that this fragment preceded it.
                    from app.trading.paper_after_hours_execution import _partial_request_identifier
                    cancel = json.loads(original.risk_json)["paper_after_hours_cancel"]
                    prior_fill = "fill-" + _partial_request_identifier(
                        _integer(intents[original.order_id]["numeric_account_id"]), prior)
                    if prior_fill not in cancel.get("verified_fill_ids", ()):
                        raise EvidenceInvalid("partial_equal_clock_cancel_missing_actual_fill_reference")
                if (history_clock is not None and proposed_at < history_clock
                        or canceled is not None and proposed_at > canceled):
                    raise EvidenceInvalid("partial_history_clock_rollback_or_fragment_after_cancel")
                for earlier in local_priority:
                    if earlier.id >= original.id:
                        break
                    if (earlier.side != original.side
                            or prior_totals.get(earlier.order_id, 0) == earlier.quantity
                            or not limit_compatible(earlier.side, earlier.price, snapshot.price)):
                        continue
                    # Service sequence, NOT a rolled-back wall clock, owns local
                    # priority. A source that predates an earlier head's clock
                    # cannot certify that head absent; it must still block.
                    canceled = cancel_clocks.get(earlier.order_id)
                    if canceled is not None and canceled <= proposed_at:
                        if prior_totals.get(earlier.order_id, 0) != earlier.filled_quantity:
                            raise EvidenceInvalid("partial_cancel_missing_prior_economic_history")
                        continue
                    raise EvidenceInvalid("partial_prior_violates_original_local_fifo")
                history_clock = proposed_at
                prior_totals[original.order_id], prior_counts[original.order_id] = after, index
                prior_clocks[original.order_id] = clocks
        if partial and any(order.filled_quantity != prior_totals.get(key, 0) for key, order in originals.items()):
            raise EvidenceInvalid("partial_prior_history_missing_or_not_reconciled")
        blocked = set()
        for order in sorted(live, key=fifo_key):
            import json
            intent = json.loads(order.risk_json)["paper_after_hours_intent"]
            accepted = _clock(intent["accepted_at"])
            reason = None
            predecessors = [o for o in snapshot.orders if o.side == order.side and o.remaining
                            and o.accepted_at <= accepted]
            if order.side in blocked:
                reason = "earlier_local_head_not_fully_allocatable"
            elif not partial and order.order_id in previously_planned:
                reason = "prior_proposal_requires_durable_ledger_reconciliation"
            elif not snapshot.close_available_at <= accepted <= snapshot.source_at:
                reason = "price_or_order_level_snapshot_not_visible_at_required_clock"
            elif not limit_compatible(order.side, order.price, snapshot.price):
                reason = "original_limit_incompatible_with_official_close"
            elif predecessors:
                reason = "verified_external_predecessors_still_live"
            resources = []
            requested = order.quantity - order.filled_quantity if partial else order.quantity
            remaining, planned_quantity = requested, requested
            if reason is None:
                for counterparty in sorted(snapshot.orders, key=lambda o: (o.accepted_at, o.sequence)):
                    if counterparty.side == order.side or not counterparty.remaining:
                        continue
                    for start, end in _subtract((counterparty.filled, counterparty.quantity),
                                                consumed.get(counterparty.record_id, [])):
                        take = min(end - start, remaining)
                        if take:
                            resources.append({"external_order_id": counterparty.record_id,
                                "source_order_hash": counterparty.identity_hash, "offset": start, "shares": take})
                            remaining -= take
                        if not remaining:
                            break
                    if not remaining:
                        break
                if partial:
                    planned_quantity = (requested - remaining) // 100 * 100
                    if planned_quantity:
                        trimmed, left = [], planned_quantity
                        for piece in resources:
                            shares = min(piece["shares"], left)
                            if shares:
                                trimmed.append({**piece, "shares": shares})
                                left -= shares
                        resources, remaining = trimmed, requested - planned_quantity
                    else:
                        reason = "insufficient_unconsumed_order_level_counterparty"
                elif remaining:
                    reason = "insufficient_unconsumed_order_level_counterparty"
            if reason is not None:
                result["waiting"].append({"order_id": order.order_id, "reason": reason,
                    "verified_external_ahead_shares": sum(o.remaining for o in predecessors)})
                # Invalid limit is not a legal queue entry. Other uncertainty never
                # permits a smaller later order to jump this full-fill-only head.
                if reason != "original_limit_incompatible_with_official_close":
                    blocked.add(order.side)
                continue
            proposal = {"contract_version": protocol, "scenario_account": scenario,
                "session_id": snapshot.session_id, "source": snapshot.source, "source_version": snapshot.version,
                "code": snapshot.code, "trade_date": snapshot.day.isoformat(), "order_id": order.order_id,
                "side": order.side, "quantity": planned_quantity, "fixed_price": float(snapshot.price),
                "proposed_at": _clock(now).isoformat(), "frame_id": snapshot.frame_id,
                "source_evidence_hash": snapshot.evidence_hash, "source_quote_at": snapshot.source_at.isoformat(),
                "source_available_at": snapshot.available_at.isoformat(),
                "terminal_sequence": snapshot.terminal_sequence,
                "lifecycle_prefix_hash": snapshot.lifecycle_prefixes[snapshot.terminal_sequence],
                "resources": resources,
                "execution_authorized": False, "fillable": False}
            if partial:
                proposal.update(**identities[order.order_id],
                    source_expires_at=snapshot.expires_at.isoformat(),
                    fragment_index=prior_counts.get(order.order_id, 0) + 1,
                    cumulative_before=order.filled_quantity,
                    cumulative_after=order.filled_quantity + planned_quantity,
                    remaining_quantity_after=remaining)
            proposal["proposal_id"] = _hash(proposal)
            result["proposals"].append(proposal)
            for piece in resources:
                consumed.setdefault(piece["external_order_id"], []).append(
                    (piece["offset"], piece["offset"] + piece["shares"]))
            if partial and remaining:
                blocked.add(order.side)
                result["waiting"].append({"order_id": order.order_id,
                    "reason": "partial_head_remainder_waits_for_counterparty",
                    "unfilled_quantity": remaining, "verified_external_ahead_shares": 0})
        result.update(status="proposal_only" if result["proposals"] else "waiting",
                      source_evidence_hash=snapshot.evidence_hash)
    except EvidenceUnavailable as exc:
        result["reason"] = str(exc)
    except (EvidenceInvalid, KeyError, ValueError, TypeError, AttributeError, OverflowError, RecursionError):
        # No partial result survives a malformed later input/prior allocation.
        result.update(status="evidence_blocked", reason="invalid_order_level_or_allocation_contract",
                      proposals=[], waiting=[])
    return result
