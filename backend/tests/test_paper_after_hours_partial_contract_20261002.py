"""Pure partial frozen candidates; NO real book, receipts or production authority."""
from copy import deepcopy
from dataclasses import replace, FrozenInstanceError
from datetime import timedelta
import hashlib
import json

import pytest
from fastapi import HTTPException

from app.data.after_hours import _json
from app.trading import paper_after_hours_execution as execution
from app.trading import paper_after_hours_allocation as allocation
from app.trading import paper_authorization as auth
from app.trading.broker import BrokerOrderRequest
from test_paper_after_hours_allocation_20261002 import (
    AT, NOW, SOURCE, local, rehash, audited_fixture,
)
from test_paper_after_hours_contract_20261002 import frozen as full_frozen, request as full_request
from test_paper_after_hours_partial_allocation_20261002 import (
    capacity, append_offer, simulated_state, partial_plan,
)


def original(identifier="head", quantity=300, sequence=1, *, account="default"):
    order = local(identifier, quantity=quantity, sequence=sequence, account=account)
    risk = json.loads(order.risk_json)
    risk["paper_after_hours_intent"]["numeric_account_id"] = 7
    order.risk_json = _json(risk)
    order.created_at = order.decision_at = AT - timedelta(seconds=1)
    order.decision_round_id = "original.decision"
    order.strategy_version, order.signal_id = "partial.fixture.v1", ""
    return order


def freeze(payload=None, order=None, *, context=None, dispatch=NOW, priors=(), **kwargs):
    order = original() if order is None else order
    return execution._freeze_partial_fill_candidate(
        capacity(150) if payload is None else payload, order,
        local_orders=[order] if context is None else context,
        account_numeric_id=kwargs.pop("account_numeric_id", 7),
        quote_round_id=kwargs.pop("quote_round_id", "partial.round.1"),
        dispatch_at=dispatch, prior_proposals=priors, **kwargs)


def request(candidate):
    p = json.loads(candidate.contract_json)
    return BrokerOrderRequest(order_id=p["request_id"], code=p["code"], side=p["side"],
        price=p["fill_price"], quantity=p["fragment_quantity"], order_type=execution.MODE,
        account_name=p["account_id"], strategy_version=p["strategy_version"], signal_id=p["signal_id"],
        decision_round_id=p["decision_round_id"], fill_round_id=p["quote_round_id"],
        filled_at=allocation._clock(p["dispatch_validated_at"]))


def check(candidate, req=None, *, phase="lock_acquired", times=None, **kwargs):
    times = iter([NOW, NOW] if times is None else times)
    return execution._validate_partial_candidate_clock(candidate, req or request(candidate),
        phase=phase, clock=lambda: next(times), **kwargs)


def test_fragment_immutable_fixed_price_original_identity_and_no_authority(audited_fixture):
    payload, order = capacity(150), original()
    before = deepcopy(payload), deepcopy(order.__dict__)
    c = freeze(payload, order)
    p = json.loads(c.contract_json)
    assert type(c) is execution._FrozenPartialFixedPriceCandidate
    assert p["contract_version"] == execution.PARTIAL_CANDIDATE_VERSION
    assert p["status"] == "validated_candidate"
    assert p["fill_price"] == 10 and p["original_limit_price"] == 10.5
    assert (p["original_quantity"], p["fragment_quantity"], p["cumulative_before"],
            p["cumulative_after"], p["remaining_quantity_after"]) == (300, 100, 0, 100, 200)
    assert "filled_quantity" not in p
    assert p["request_id"] != p["order_id"] and len(p["request_id"]) == 35
    assert p["expected_fill_id"] == "fill-" + p["request_id"] and len(p["expected_fill_id"]) == 40
    assert p["signal_id"] == order.order_id and p["local_acceptance_sequence"] == 1
    assert all(p[key] is False for key in (
        "execution_authorized", "partial_fill_allowed", "ledger_contract_supported", "fees_certified"))
    assert before == (payload, order.__dict__)
    assert check(c) == (NOW, None)
    with pytest.raises(FrozenInstanceError):
        c.local_orders_json = "[]"
    assert p["prior_history_sha256"] == hashlib.sha256(c.priors_json.encode()).hexdigest()
    assert p["local_context_sha256"] == hashlib.sha256(c.local_orders_json.encode()).hexdigest()
    order.filled_quantity, payload["initial"]["orders"][0]["quantity"] = 200, 900
    assert json.loads(c.contract_json) == p


def test_production_providers_cannot_upgrade_partial_candidates():
    payload = capacity(150)
    for source, profile in allocation._PROVIDERS.items():
        payload.update(source=source, source_version=profile.version, queue_verified=True,
            partial_fill_allowed=True, capability="order_level", execution_authorized=True)
        with pytest.raises(ValueError, match="provider_aggregate_only"):
            freeze(payload)


def test_stable_fragment_request_retries_and_new_fragment_sequence(audited_fixture):
    payload, order = capacity(150), original()
    c = freeze(payload, order)
    p = json.loads(c.contract_json)
    retry = freeze(payload, order, dispatch=NOW+timedelta(milliseconds=100),
                   quote_round_id="partial.round.retry")
    assert p["request_id"] == json.loads(retry.contract_json)["request_id"]
    assert c != retry  # Different frozen dispatch is not interchangeable authorization.
    prior = p["allocation"]
    changed = simulated_state(order, 100)
    more = append_offer(payload, identifier="offer.2", shares=100,
                        at=AT+timedelta(seconds=2), frame="frame.2")
    c2 = freeze(more, changed, dispatch=AT+timedelta(seconds=3),
                priors=[prior], quote_round_id="partial.round.2")
    p2 = json.loads(c2.contract_json)
    assert p2["request_id"] != p["request_id"]
    assert (p2["fragment_index"], p2["cumulative_before"], p2["cumulative_after"],
            p2["remaining_quantity_after"]) == (2, 100, 200, 100)
    assert p2["local_intent_hash"] == p["local_intent_hash"]
    assert check(c2, times=[AT+timedelta(seconds=3)]*2)[0] == AT+timedelta(seconds=3)
    with pytest.raises(HTTPException):
        check(c2, request(c), times=[AT+timedelta(seconds=3)]*2)


def test_later_original_cannot_use_an_earlier_uncommitted_batch_proposal(audited_fixture):
    head, later = original(quantity=100), original("later", quantity=100, sequence=2)
    payload = capacity(300)
    # The pure batch planner can plan both, but neither is a durable receipt.
    assert len(partial_plan(payload, [head, later])["proposals"]) == 2
    with pytest.raises(ValueError, match="earlier_uncommitted_original"):
        freeze(payload, later, context=[head, later])
    first = json.loads(freeze(payload, head, context=[later, head]).contract_json)["allocation"]
    done = simulated_state(head, 100)
    c = freeze(payload, later, context=[done, later], priors=[first])
    assert json.loads(c.contract_json)["order_id"] == later.order_id


@pytest.mark.parametrize("head_state", ["still_live", "canceled_after", "canceled_before"])
def test_history_cannot_skip_sequence_head_on_wall_clock_rollback(audited_fixture, head_state):
    head, later = original(quantity=100), original("later", quantity=300, sequence=2)
    for order, seconds in ((head, 5), (later, 4)):
        accepted = AT + timedelta(seconds=seconds)
        order.decision_at = order.created_at = accepted
        risk = json.loads(order.risk_json)
        proof = risk["paper_after_hours_intent"]
        for key in ("requested_at", "accepted_at", "validated_at", "terminal_validated_at"):
            proof[key] = accepted.isoformat()
        order.risk_json = _json(risk)
    old = capacity(150)
    source = AT + timedelta(seconds=4, milliseconds=500)
    old.update(source_quote_at=source.isoformat(), received_at=(source+timedelta(milliseconds=100)).isoformat(),
               available_at=(source+timedelta(milliseconds=200)).isoformat())
    proposed_at = AT+timedelta(seconds=6)
    # Incomplete supplied context is NOT a certified service DB read. No
    # fingerprints or native history are altered in this semantic counterexample.
    prior = json.loads(freeze(old, later, context=[later], dispatch=proposed_at).contract_json)["allocation"]
    current_head = head
    if head_state != "still_live":
        current_head = simulated_state(head, 0, canceled=True)
        risk = json.loads(current_head.risk_json)
        risk["paper_after_hours_cancel"]["canceled_at"] = (
            AT+timedelta(seconds=5, milliseconds=500) if head_state == "canceled_before"
            else AT+timedelta(seconds=7)).isoformat()
        current_head.risk_json = _json(risk)
    current_later = simulated_state(later, 100)
    latest = append_offer(old, identifier="new", shares=100, at=AT+timedelta(seconds=8), frame="latest")
    dispatch = AT+timedelta(seconds=9)
    context = [current_head, current_later]
    plan = partial_plan(latest, context, now=dispatch, prior_proposals=[prior])
    if head_state == "canceled_before":
        assert plan["status"] == "proposal_only"
        c = freeze(latest, current_later, context=context, dispatch=dispatch, priors=[prior])
        assert json.loads(c.contract_json)["cumulative_after"] == 200
    else:
        assert plan["status"] == "evidence_blocked" and plan["proposals"] == []
        with pytest.raises(ValueError):
            freeze(latest, current_later, context=context, dispatch=dispatch, priors=[prior])
        if head_state == "still_live":
            # Illegal later history cannot be hidden by dispatching the head next.
            with pytest.raises(ValueError):
                freeze(latest, current_head, context=context, dispatch=dispatch, priors=[prior])


def test_partial_head_remainder_blocks_smaller_later_candidate(audited_fixture):
    head, later = original(), original("later", quantity=100, sequence=2)
    assert json.loads(freeze(capacity(150), head, context=[later, head]).contract_json)["fragment_quantity"] == 100
    with pytest.raises(ValueError, match="resources_or_fifo_unavailable"):
        freeze(capacity(150), later, context=[head, later])


def test_price_incompatible_head_can_be_skipped_but_unknown_proof_cannot(audited_fixture):
    head, later = original(quantity=100), original("later", quantity=100, sequence=2)
    head.price = 9.5
    risk = json.loads(head.risk_json)
    risk["paper_after_hours_intent"]["original_limit_price"] = head.price
    head.risk_json = _json(risk)
    c = freeze(capacity(150), later, context=[head, later])
    assert json.loads(c.contract_json)["order_id"] == later.order_id
    head.risk_json = "{}"
    with pytest.raises((ValueError, KeyError)):
        freeze(capacity(150), later, context=[head, later])


@pytest.mark.parametrize("context_kind", ["empty", "missing", "duplicate", "different_target", "cross_account"])
def test_original_context_cannot_be_missing_or_substituted(audited_fixture, context_kind):
    head, peer = original(), original("peer", sequence=2)
    contexts = {"empty": [], "missing": [peer], "duplicate": [head, deepcopy(head)],
                "different_target": [simulated_state(head, 100)],
                "cross_account": [head, original("other", sequence=2, account="promotion")]}
    with pytest.raises(ValueError):
        freeze(order=head, context=contexts[context_kind])


@pytest.mark.parametrize("field,value", [
    ("numeric_account_id", 8), ("numeric_account_id", 7.0), ("numeric_account_id", True),
    ("terminal_validated_at", (NOW+timedelta(seconds=1)).isoformat()),
    ("original_quantity", 100.0), ("side", "sell"),
])
def test_peer_numeric_account_original_proof_and_clock_remain_mandatory(audited_fixture, field, value):
    head, peer = original(), original("peer", quantity=100, sequence=2)
    risk = json.loads(peer.risk_json)
    risk["paper_after_hours_intent"][field] = value
    peer.risk_json = _json(risk)
    with pytest.raises((ValueError, KeyError)):
        freeze(order=head, context=[head, peer])


@pytest.mark.parametrize("field,value", [
    ("order_id", "head"), ("order_type", "limit"), ("quantity", 300), ("quantity", 100.0),
    ("quantity", True), ("price", 10.5), ("account_name", "promotion"),
    ("signal_id", ""), ("strategy_version", "other"), ("fill_round_id", "original.decision"),
    ("filled_at", NOW+timedelta(seconds=1)),
])
def test_hypothetical_request_bound_to_fragment_not_original_total(audited_fixture, field, value):
    c = freeze()
    with pytest.raises(HTTPException) as exc:
        check(c, replace(request(c), **{field: value}))
    assert exc.value.status_code == 409


def test_boolean_request_price_rejected_at_one_yuan(audited_fixture):
    payload, order = capacity(150), original()
    payload["official_close"]["price"] = 1
    payload["initial"]["orders"][0]["limit_price"] = 1
    order.price = 1.5
    risk = json.loads(order.risk_json)
    risk["paper_after_hours_intent"]["original_limit_price"] = order.price
    order.risk_json = _json(risk)
    c = freeze(payload, order)
    with pytest.raises(HTTPException):
        check(c, replace(request(c), price=True))


def test_fragment_clock_proof_resamples_after_work_but_is_not_ledger_timing(audited_fixture):
    c = freeze()
    locked = NOW+timedelta(milliseconds=100)
    assert check(c, times=[NOW, locked]) == (locked, None)
    terminal = NOW+timedelta(milliseconds=300)
    checked, proof = check(c, phase="before_mutation", lock_checked_at=locked,
                          times=[NOW+timedelta(milliseconds=200), terminal])
    assert checked == terminal
    assert proof["guard_version"] == execution.PARTIAL_CLOCK_VERSION
    assert proof["input_contract_version"] == execution.PARTIAL_CANDIDATE_VERSION
    assert proof["fragment_quantity"] == 100 and proof["remaining_quantity_after"] == 200
    assert proof["execution_authorized"] is proof["ledger_contract_supported"] is False
    assert proof["physical_commit_at"] is None
    with pytest.raises(HTTPException):
        check(c, phase="before_mutation", lock_checked_at=locked, ledger_timing=proof,
              times=[terminal, terminal])


@pytest.mark.parametrize("phase,kwargs,times", [
    ("before_mutation", {}, [NOW, NOW]),
    ("lock_acquired", {"lock_checked_at": NOW}, [NOW, NOW]),
    ("before_mutation", {"lock_checked_at": NOW+timedelta(seconds=1)}, [NOW, NOW]),
    ("lock_acquired", {}, [NOW, NOW-timedelta(microseconds=1)]),
    ("unknown", {}, [NOW, NOW]),
    ("lock_acquired", {}, [NOW-timedelta(seconds=1), NOW]),
    ("lock_acquired", {}, [NOW, AT+timedelta(seconds=15)]),
])
def test_fragment_clock_missing_repeated_future_rollback_and_expiry(audited_fixture, phase, kwargs, times):
    with pytest.raises(HTTPException):
        check(freeze(), phase=phase, times=times, **kwargs)


def test_validation_may_not_borrow_clock_across_1530(audited_fixture):
    payload = capacity(150)
    source = AT.replace(minute=29, second=50)
    payload.update(source_quote_at=source.isoformat(),
                   received_at=(source+timedelta(milliseconds=100)).isoformat(),
                   available_at=(source+timedelta(milliseconds=200)).isoformat())
    c = freeze(payload, dispatch=source+timedelta(seconds=1))
    with pytest.raises(HTTPException):
        check(c, times=[source.replace(second=59, microsecond=999999), source.replace(minute=30, second=0)])


@pytest.mark.parametrize("part", ["contract_json", "feed_json", "order_json", "priors_json",
                                 "local_orders_json", "protocol_version"])
def test_changed_capsule_or_partial_context_rejected_even_with_new_fingerprint(audited_fixture, part):
    c = freeze()
    req = request(c)
    altered = replace(c, **{part: "[]" if part == "local_orders_json" else "{}"})
    with pytest.raises(HTTPException):
        check(altered, req)
    if part.endswith("_json"):
        pieces = tuple(getattr(altered, key) for key in (
            "contract_json", "feed_json", "order_json", "priors_json", "local_orders_json"))
        altered = replace(altered, fingerprint=execution._candidate_fingerprint(*pieces))
        with pytest.raises(HTTPException):
            check(altered, req)


@pytest.mark.parametrize("field,value", [
    ("cumulative_before", 100), ("cumulative_after", 200), ("remaining_quantity_after", 0),
    ("fragment_index", 2), ("local_acceptance_sequence", 2), ("request_id", "client-invented"),
    ("execution_authorized", True), ("ledger_contract_supported", True), ("fees_certified", True),
])
def test_rehashed_but_inconsistent_fragment_fields_are_not_certified(audited_fixture, field, value):
    c = freeze()
    p = json.loads(c.contract_json)
    p[field] = value
    text = _json(p)
    altered = replace(c, contract_json=text)
    parts = (text, c.feed_json, c.order_json, c.priors_json, c.local_orders_json)
    altered = replace(altered, fingerprint=execution._candidate_fingerprint(*parts))
    with pytest.raises(HTTPException):
        check(altered, request(c))


def test_missing_history_and_invalid_native_slice_cannot_freeze_second_fragment(audited_fixture):
    payload, order = capacity(150), original()
    first = json.loads(freeze(payload, order).contract_json)["allocation"]
    changed = simulated_state(order, 100)
    more = append_offer(payload, identifier="new", shares=100, at=AT+timedelta(seconds=2), frame="next")
    with pytest.raises(ValueError):
        freeze(more, changed, dispatch=AT+timedelta(seconds=3))
    bad = deepcopy(first)
    bad["resources"][0]["offset"] = 100  # End=200 exceeds this original native offer of 150.
    rehash(bad)
    with pytest.raises(ValueError):
        freeze(more, changed, dispatch=AT+timedelta(seconds=3), priors=[bad])


def test_partial_and_full_candidate_types_cannot_cross_clock_or_frozen_protocol(audited_fixture):
    c, full = freeze(), full_frozen()
    with pytest.raises(HTTPException):
        execution._validate_candidate_clock(c, request(c), phase="lock_acquired", clock=lambda: NOW)
    with pytest.raises(HTTPException):
        check(full, request(c))
    full_type_fake = execution._FrozenFixedPriceCandidate(c.contract_json, c.feed_json, c.order_json,
        c.priors_json, c.fingerprint)
    with pytest.raises(HTTPException):
        execution._validate_candidate_clock(full_type_fake, request(c), phase="lock_acquired", clock=lambda: NOW)


def test_candidate_size_and_original_count_budgets(audited_fixture, monkeypatch):
    head, peer = original(), original("peer", sequence=2)
    monkeypatch.setattr(allocation, "MAX_LOCAL_ORDERS", 1)
    with pytest.raises(ValueError, match="local_context_budget"):
        freeze(order=head, context=[head, peer])
    c = freeze(order=head)
    budget = len(c.local_orders_json.encode())
    monkeypatch.setattr(allocation, "MAX_BYTES", budget-1)
    with pytest.raises(ValueError):
        freeze(order=head)
    with pytest.raises(HTTPException):
        check(c)


def test_complete_partial_capsule_exact_utf8_total_budget(audited_fixture, monkeypatch):
    order = original()
    risk = json.loads(order.risk_json)
    risk["research_note"] = "盘后研究" * 50  # UTF8 bytes, not Python string characters.
    order.risk_json = _json(risk)
    c = freeze(order=order)
    fields = ("contract_json", "feed_json", "order_json", "priors_json", "local_orders_json")
    size = sum(len(getattr(c, field).encode()) for field in fields)
    assert size > sum(len(getattr(c, field)) for field in fields)
    monkeypatch.setattr(execution, "MAX_PARTIAL_CANDIDATE_BYTES", size)
    assert freeze(order=order) == c and check(c) == (NOW, None)
    monkeypatch.setattr(execution, "MAX_PARTIAL_CANDIDATE_BYTES", size - 1)
    with pytest.raises(ValueError, match="total_byte_budget"):
        freeze(order=order)
    with pytest.raises(HTTPException):
        check(c)
    # The original four-part full-fill capsule has not acquired this new bound.
    full = full_frozen()
    assert execution._validate_candidate_clock(full,
        full_request(full),
        phase="lock_acquired", clock=lambda: NOW) == (NOW, None)


def test_provider_profile_change_cannot_extend_fragment_expiry(audited_fixture, monkeypatch):
    c = freeze()
    monkeypatch.setattr(allocation, "_PROVIDERS", {
        SOURCE: allocation._Provider("fixture.v1", "order_level", allocation.METHOD, 30)})
    with pytest.raises(HTTPException):
        check(c)


@pytest.mark.asyncio
async def test_partial_candidate_and_json_cannot_open_existing_authorization(audited_fixture, monkeypatch):
    from app.api.v1 import paper
    c = freeze()
    req, db = request(c), object()
    monkeypatch.setattr(paper, "_public_order_clock", lambda: NOW)
    with pytest.raises(HTTPException) as exc:
        with auth._paper_execution_scope(db, req, immediate_evidence_json=c.contract_json, fixed_candidate=c):
            pass
    assert exc.value.status_code == 403
    with auth._paper_execution_scope(db, req, immediate_evidence_json=c.contract_json):
        auth._SCOPE.get().stage = "ledger"  # Isolated type rejection, not broker/book authorization.
        with pytest.raises(HTTPException) as exc:
            auth.validate_ledger_clock(db, phase="lock_acquired")
        assert exc.value.status_code == 409 and auth._SCOPE.get().ledger_timing is None
