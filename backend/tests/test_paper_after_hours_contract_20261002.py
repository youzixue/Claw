"""Pure frozen candidate/clock tests, NOT production book/broker authorization."""
from copy import deepcopy
from dataclasses import replace, FrozenInstanceError
from datetime import timedelta
import json

import pytest
from fastapi import HTTPException

from app.data.after_hours import _json
from app.trading import paper_after_hours_execution as execution
from app.trading import paper_after_hours_allocation as allocator
from app.trading import paper_authorization as auth
from app.trading.broker import BrokerOrderRequest
from test_paper_after_hours_allocation_20261002 import AT, NOW, SOURCE, feed, local, audited_fixture


def original():
    order = local()
    proof = json.loads(order.risk_json)
    proof["paper_after_hours_intent"]["numeric_account_id"] = 7
    order.risk_json = _json(proof)
    order.decision_at = order.created_at = AT - timedelta(seconds=1)
    order.decision_round_id, order.strategy_version, order.signal_id = "original.decision", "fixed.fixture.v1", ""
    return order


def frozen(payload=None, order=None, dispatch=NOW, **changes):
    return execution._freeze_fill_candidate(feed() if payload is None else payload,
        original() if order is None else order, dispatch_at=dispatch,
        account_numeric_id=changes.pop("account_numeric_id", 7),
        quote_round_id=changes.pop("quote_round_id", "fixed.round.1"), **changes)


def request(candidate):
    p = json.loads(candidate.contract_json)
    return BrokerOrderRequest(order_id=p["order_id"], code=p["code"], side=p["side"],
        price=p["fill_price"], quantity=p["filled_quantity"], order_type=execution.MODE,
        account_name=p["account_id"], strategy_version=p["strategy_version"], signal_id=p["signal_id"],
        decision_round_id=p["decision_round_id"], fill_round_id=p["quote_round_id"],
        filled_at=allocator._clock(p["dispatch_validated_at"]))


def clock_check(candidate, req=None, phase="lock_acquired", times=None, **kwargs):
    clocks = iter(times or [NOW, NOW])
    return execution._validate_candidate_clock(candidate, req or request(candidate), phase=phase,
        clock=lambda: next(clocks), **kwargs)


def test_frozen_inputs_not_permits_and_fixed_price_not_limit(audited_fixture):
    payload, order = feed(), original()
    before = deepcopy(payload), deepcopy(order.__dict__)
    candidate = frozen(payload, order)
    p = json.loads(candidate.contract_json)
    assert p["fill_price"] == 10 and p["original_limit_price"] == 10.5
    assert p["account_numeric_id"] == 7 and p["signal_id"] == order.order_id
    assert p["decision_round_id"] != p["quote_round_id"]
    assert p["execution_authorized"] is False and p["allocation"]["fillable"] is False
    assert before == (payload, order.__dict__)
    with pytest.raises(FrozenInstanceError):
        candidate.feed_json = "{}"
    payload["official_close"]["price"] = 11
    order.price = 11
    assert json.loads(candidate.contract_json) == p
    assert clock_check(candidate)[0] == NOW


def test_production_providers_cannot_upgrade_candidate_from_booleans():
    payload = feed()
    for source, profile in allocator._PROVIDERS.items():
        payload.update(source=source, source_version=profile.version,
            execution_authorized=True, queue_verified=True, capability="order_level")
        with pytest.raises(ValueError, match="provider_aggregate_only"):
            frozen(payload)


@pytest.mark.parametrize("account", [None, False, 0, 8])
def test_original_numeric_account_cannot_be_missing_replaced_or_bool(audited_fixture, account):
    order = original()
    proof = json.loads(order.risk_json)
    proof["paper_after_hours_intent"]["numeric_account_id"] = account
    order.risk_json = _json(proof)
    with pytest.raises(ValueError):
        frozen(order=order)


@pytest.mark.parametrize("change", [
    lambda o: setattr(o, "decision_at", AT),
    lambda o: setattr(o, "created_at", NOW + timedelta(seconds=1)),
    lambda o: setattr(o, "decision_round_id", ""),
    lambda o: setattr(o, "strategy_version", ""),
    lambda o: setattr(o, "status", "partial"),
    lambda o: setattr(o, "filled_quantity", 100),
])
def test_unknown_original_provenance_or_partial_cannot_freeze(audited_fixture, change):
    order = original()
    change(order)
    with pytest.raises(ValueError):
        frozen(order=order)


@pytest.mark.parametrize("change", [
    {"account_numeric_id": True}, {"account_numeric_id": 8},
    {"quote_round_id": "original.decision"}, {"quote_round_id": "x" * 65},
])
def test_freeze_identity_arguments_do_not_override_original(audited_fixture, change):
    with pytest.raises(ValueError):
        frozen(**change)


@pytest.mark.parametrize("field,value", [
    ("order_type", "limit"), ("code", "000001"), ("quantity", 200), ("quantity", 100.0),
    ("price", 10.5), ("account_name", "promotion"), ("signal_id", ""),
    ("strategy_version", "another"), ("decision_round_id", "another"),
    ("fill_round_id", "original.decision"), ("filled_at", NOW + timedelta(seconds=1)),
])
def test_request_fully_bound_to_frozen_fixed_contract(audited_fixture, field, value):
    candidate = frozen()
    req = replace(request(candidate), **{field: value})
    with pytest.raises(HTTPException) as exc:
        clock_check(candidate, req)
    assert exc.value.status_code == 409


def test_boolean_price_cannot_equal_a_one_yuan_fixed_price(audited_fixture):
    payload, order = feed(), original()
    payload["official_close"]["price"] = 1
    payload["initial"]["orders"][0]["limit_price"] = 1
    order.price = 1.5
    proof = json.loads(order.risk_json)
    proof["paper_after_hours_intent"]["original_limit_price"] = order.price
    order.risk_json = _json(proof)
    candidate = frozen(payload, order)
    with pytest.raises(HTTPException):
        clock_check(candidate, replace(request(candidate), price=True))


def test_lock_and_before_mutation_sample_after_replay(audited_fixture):
    candidate = frozen()
    lock = NOW + timedelta(milliseconds=100)
    checked, proof = clock_check(candidate, times=[NOW, lock])
    assert checked == lock and proof is None
    terminal = NOW + timedelta(milliseconds=300)
    checked, proof = clock_check(candidate, phase="before_mutation", lock_checked_at=lock,
        times=[NOW + timedelta(milliseconds=200), terminal])
    assert checked == terminal and proof["before_mutation_checked_at"] == terminal.isoformat()
    assert proof["guard_version"] == execution.LEDGER_VERSION
    assert proof["execution_authorized"] is False and proof["physical_commit_at"] is None
    with pytest.raises(HTTPException):
        clock_check(candidate, phase="before_mutation", lock_checked_at=lock,
            ledger_timing=proof, times=[terminal, terminal])


@pytest.mark.parametrize("phase,kwargs,times", [
    ("before_mutation", {}, [NOW, NOW]),
    ("lock_acquired", {"lock_checked_at": NOW}, [NOW, NOW]),
    ("before_mutation", {"lock_checked_at": NOW + timedelta(seconds=1)}, [NOW, NOW]),
    ("lock_acquired", {}, [NOW, NOW - timedelta(microseconds=1)]),
    ("unknown", {}, [NOW, NOW]),
    ("lock_acquired", {}, [NOW - timedelta(seconds=1), NOW]),
    ("lock_acquired", {}, [NOW, AT + timedelta(seconds=15)]),
])
def test_missing_repeated_rolled_back_or_expired_clock(audited_fixture, phase, kwargs, times):
    with pytest.raises(HTTPException) as exc:
        clock_check(frozen(), phase=phase, times=times, **kwargs)
    assert exc.value.status_code == 409


def test_synchronous_validation_cannot_borrow_152959_clock_across_close(audited_fixture):
    payload = feed()
    source = AT.replace(minute=29, second=50)
    payload.update(source_quote_at=source.isoformat(), received_at=(source + timedelta(milliseconds=100)).isoformat(),
                   available_at=(source + timedelta(milliseconds=200)).isoformat())
    dispatch = source + timedelta(seconds=1)
    candidate = frozen(payload, dispatch=dispatch)
    with pytest.raises(HTTPException):
        clock_check(candidate, times=[AT.replace(minute=29, second=59, microsecond=999999),
                                      AT.replace(minute=30)])


@pytest.mark.parametrize("part", ["contract_json", "feed_json", "order_json", "priors_json", "protocol_version"])
def test_capsule_retyping_or_changed_originals_rejected(audited_fixture, part):
    candidate = frozen()
    req = request(candidate)  # Do not parse an intentionally damaged capsule in the test helper.
    altered = replace(candidate, **{part: "{}" if part.endswith("_json") else "immediate"})
    with pytest.raises(HTTPException):
        clock_check(altered, req)
    if part.endswith("_json"):
        pieces = tuple(getattr(altered, key) for key in
            ("contract_json", "feed_json", "order_json", "priors_json"))
        altered = replace(altered, fingerprint=execution._candidate_fingerprint(*pieces))
        with pytest.raises(HTTPException):
            clock_check(altered, req)


def test_source_profile_change_cannot_extend_frozen_expiry(audited_fixture, monkeypatch):
    candidate = frozen()
    monkeypatch.setattr(allocator, "_PROVIDERS", {
        SOURCE: allocator._Provider("fixture.v1", "order_level", allocator.METHOD, max_age_seconds=30)})
    with pytest.raises(HTTPException):
        clock_check(candidate)


@pytest.mark.asyncio
async def test_candidate_validation_never_opens_existing_production_guard(audited_fixture, monkeypatch):
    from app.api.v1 import paper
    candidate = frozen()
    req, db = request(candidate), object()
    monkeypatch.setattr(paper, "_public_order_clock", lambda: NOW)
    with auth._paper_execution_scope(db, req, immediate_evidence_json=candidate.contract_json):
        auth._SCOPE.get().stage = "ledger"  # Isolated private fixture, not an API/real book call.
        with pytest.raises(HTTPException) as exc:
            auth.validate_ledger_clock(db, phase="lock_acquired")
        assert exc.value.status_code == 409
        assert auth._SCOPE.get().ledger_timing is None
