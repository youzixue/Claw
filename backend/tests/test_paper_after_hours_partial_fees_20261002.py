"""Existing-paper fee previews ONLY: no book write, real broker fees or source refresh."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json

import pytest
from fastapi import HTTPException

from app.api.v1 import paper
from app.data.after_hours import _json
from app.trading import paper_after_hours_execution as execution
from app.trading import paper_after_hours_allocation as allocation
from test_paper_after_hours_allocation_20261002 import AT, NOW, external, rehash, audited_fixture
from test_paper_after_hours_partial_contract_20261002 import original, freeze, check, request
from test_paper_after_hours_partial_allocation_20261002 import capacity, append_offer, simulated_state
from test_paper_after_hours_contract_20261002 import frozen as full_frozen, request as full_request


@pytest.fixture
def fee_model(monkeypatch):
    # Explicit model assumptions, not a broker schedule or current official tax law.
    monkeypatch.setattr(paper.settings, "PAPER_COMMISSION_RATE", 0.0003)
    monkeypatch.setattr(paper.settings, "PAPER_MIN_COMMISSION", 5.0)
    monkeypatch.setattr(paper.settings, "PAPER_STAMP_TAX_RATE", 0.001)


def sell(order):
    order = deepcopy(order)
    order.side, order.price = "sell", 9.5
    risk = json.loads(order.risk_json)
    risk["paper_after_hours_intent"].update(side=order.side, original_limit_price=order.price)
    order.risk_json = _json(risk)
    return order


def selling_feed():
    payload = capacity(150)
    payload["initial"]["orders"] = [external(side="buy", quantity=150)]
    return payload


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_preview_reuses_existing_book_helpers_at_fixed_not_limit_price(audited_fixture, fee_model, side):
    order, payload = original(), capacity(150)
    if side == "sell":
        order, payload = sell(order), selling_feed()
    c = freeze(payload, order)
    p = json.loads(c.contract_json)
    preview, model = p["fee_preview"], p["fee_preview"]["model"]
    gross = p["fill_price"] * p["fragment_quantity"]
    assert gross == 1000 and p["original_limit_price"] != p["fill_price"]
    assert preview["gross_value"] == gross
    assert preview["commission"] == paper._commission(gross) == 5
    assert preview["tax"] == (paper._stamp_tax(gross) if side == "sell" else 0)
    assert preview["cash_change_modeled"] == (-1005 if side == "buy" else 994)
    assert model["configuration_pit_certified"] is model["real_broker_schedule_certified"] is False
    assert preview["status"] == "modeled_only"
    assert preview["book_authority"] is preview["cash_reserved"] is preview["partial_ledger_supported"] is False
    assert model["charging_unit"] == "each_paper_trade_log_fragment_not_original_order_minimum"
    assert preview["model_sha256"] == hashlib.sha256(_json(model).encode()).hexdigest()
    assert p["allocation"]["fee_model_sha256"] == preview["model_sha256"]
    assert p["fees_certified"] is False


@pytest.mark.parametrize("rate,minimum,tax", [(0, 0, 0), (0.01, 2, 0.005), (0.00025, 0.1, 0.0005)])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_configured_paper_rates_not_hardcoded_or_buyer_taxed(audited_fixture, monkeypatch, rate, minimum, tax, side):
    monkeypatch.setattr(paper.settings, "PAPER_COMMISSION_RATE", rate)
    monkeypatch.setattr(paper.settings, "PAPER_MIN_COMMISSION", minimum)
    monkeypatch.setattr(paper.settings, "PAPER_STAMP_TAX_RATE", tax)
    order, payload = original(), capacity(150)
    if side == "sell":
        order, payload = sell(order), selling_feed()
    preview = json.loads(freeze(payload, order).contract_json)["fee_preview"]
    value = 1000
    assert preview["commission"] == paper._commission(value)
    assert preview["tax"] == (paper._stamp_tax(value) if side == "sell" else 0)


def test_two_fragments_use_explicit_per_book_model_not_one_original_minimum(audited_fixture, fee_model):
    order, payload = original(), capacity(150)
    first = json.loads(freeze(payload, order).contract_json)
    state = simulated_state(order, 100)
    more = append_offer(payload, identifier="new", shares=100, at=AT+timedelta(seconds=2), frame="next")
    second = json.loads(freeze(more, state, dispatch=AT+timedelta(seconds=3),
        priors=[first["allocation"]]).contract_json)
    assert first["fee_preview"]["commission"] + second["fee_preview"]["commission"] == 10
    assert paper._commission(2000) == 5  # Different aggregation unit, intentionally NOT substituted.
    assert first["fee_preview"]["model_sha256"] == second["fee_preview"]["model_sha256"]
    assert first["allocation"]["local_intent_hash"] == second["allocation"]["local_intent_hash"]
    assert second["cumulative_before"] == 100 and second["cumulative_after"] == 200
    assert second["fees_certified"] is False


@pytest.mark.parametrize("field,value", [
    ("PAPER_COMMISSION_RATE", 0.01), ("PAPER_MIN_COMMISSION", 6), ("PAPER_STAMP_TAX_RATE", 0.0005),
])
def test_any_policy_change_rejects_existing_candidate_and_original_later_fragment(audited_fixture, fee_model, monkeypatch, field, value):
    order, payload = original(), capacity(150)
    c = freeze(payload, order)
    prior = json.loads(c.contract_json)["allocation"]
    monkeypatch.setattr(paper.settings, field, value)
    with pytest.raises(HTTPException):
        check(c)
    more = append_offer(payload, identifier="new", shares=100, at=AT+timedelta(seconds=2), frame="next")
    with pytest.raises(ValueError, match="original_history_unknown_or_policy_changed"):
        freeze(more, simulated_state(order, 100), dispatch=AT+timedelta(seconds=3), priors=[prior])


@pytest.mark.parametrize("field,value", [
    ("PAPER_COMMISSION_RATE", True), ("PAPER_COMMISSION_RATE", -0.01),
    ("PAPER_COMMISSION_RATE", 1), ("PAPER_COMMISSION_RATE", float("nan")),
    ("PAPER_MIN_COMMISSION", "5"), ("PAPER_MIN_COMMISSION", False),
    ("PAPER_MIN_COMMISSION", float("inf")), ("PAPER_MIN_COMMISSION", -1),
    ("PAPER_MIN_COMMISSION", 2**4096), ("PAPER_STAMP_TAX_RATE", 1),
    ("PAPER_STAMP_TAX_RATE", float("-inf")),
])
def test_invalid_fee_parameters_fail_closed_without_normalizing_bogus_values(audited_fixture, fee_model, monkeypatch, field, value):
    c = freeze()
    monkeypatch.setattr(paper.settings, field, value)
    with pytest.raises(ValueError):
        freeze()
    with pytest.raises(HTTPException):
        check(c)


@pytest.mark.parametrize("side,helper,field,temporary,expected", [
    ("buy", "_commission", "PAPER_MIN_COMMISSION", 6, 5),
    ("sell", "_stamp_tax", "PAPER_STAMP_TAX_RATE", 0.002, 1),
])
def test_settings_aba_uses_frozen_parameters_not_temporary_live_values(
        audited_fixture, fee_model, monkeypatch, side, helper, field, temporary, expected):
    real = getattr(paper, helper)
    original_parameter = getattr(paper.settings, field)
    def changing(value, **kwargs):
        setattr(paper.settings, field, temporary)
        try:
            return real(value, **kwargs)
        finally:
            setattr(paper.settings, field, original_parameter)
    monkeypatch.setattr(paper, helper, changing)
    order, payload = original(), capacity(150)
    if side == "sell":
        order, payload = sell(order), selling_feed()
    candidate = freeze(payload, order)
    preview = json.loads(candidate.contract_json)["fee_preview"]
    assert preview["commission" if side == "buy" else "tax"] == expected
    assert check(candidate) == (NOW, None)


def test_settings_change_inside_book_helper_cannot_mix_fee_models(audited_fixture, fee_model, monkeypatch):
    real = paper._commission
    def changing(value, **kwargs):
        result = real(value, **kwargs)
        monkeypatch.setattr(paper.settings, "PAPER_MIN_COMMISSION", 6)
        return result
    monkeypatch.setattr(paper, "_commission", changing)
    with pytest.raises(ValueError, match="changed_during_freeze"):
        freeze()


@pytest.mark.parametrize("phase", ["lock_acquired", "before_mutation"])
def test_terminal_clock_callback_cannot_change_fee_model_after_rebuild(audited_fixture, fee_model, monkeypatch, phase):
    c = freeze()
    calls = []
    def clock():
        calls.append(1)
        if len(calls) == 2:
            monkeypatch.setattr(paper.settings, "PAPER_MIN_COMMISSION", 6)
        return NOW
    with pytest.raises(HTTPException) as exc:
        execution._validate_partial_candidate_clock(c, request(c), phase=phase, clock=clock,
            lock_checked_at=NOW if phase == "before_mutation" else None)
    assert "changed_at_terminal_check" in exc.value.detail


@pytest.mark.parametrize("damage", ["missing", "bad_hash", "different_model"])
def test_original_history_fee_model_cannot_be_unknown_or_rewritten(audited_fixture, fee_model, damage):
    order, payload = original(), capacity(150)
    prior = json.loads(freeze(payload, order).contract_json)["allocation"]
    if damage == "missing":
        prior.pop("fee_model")
        prior.pop("fee_model_sha256")
    elif damage == "bad_hash":
        prior["fee_model_sha256"] = "0" * 64
    else:
        prior["fee_model"]["minimum_commission"] = 6
        prior["fee_model_sha256"] = allocation._hash(prior["fee_model"])
    rehash(prior)  # Proposal hashes alone are not fee identity/issuer authority.
    more = append_offer(payload, identifier="new", shares=100, at=AT+timedelta(seconds=2), frame="next")
    with pytest.raises(ValueError, match="original_history_unknown_or_policy_changed"):
        freeze(more, simulated_state(order, 100), dispatch=AT+timedelta(seconds=3), priors=[prior])


def test_corrupted_preview_cannot_upgrade_cash_or_fee_authority(audited_fixture, fee_model):
    c = freeze()
    p = json.loads(c.contract_json)
    p["fee_preview"].update(commission=0, book_authority=True)
    content = _json(p)
    altered = replace(c, contract_json=content)
    parts = (content, c.feed_json, c.order_json, c.priors_json, c.local_orders_json)
    altered = replace(altered, fingerprint=execution._candidate_fingerprint(*parts))
    with pytest.raises(HTTPException):
        check(altered, request(c))


def test_v1_partial_capsules_not_reinterpreted_as_current_fee_frozen_candidate(audited_fixture, fee_model):
    c = freeze()
    legacy = replace(c, protocol_version="after_hours_partial_frozen_candidate_v1_20261002")
    with pytest.raises(HTTPException):
        check(legacy, request(c))


def test_partial_preview_does_not_change_original_full_book_contract(audited_fixture, fee_model, monkeypatch):
    c = full_frozen()
    assert "fee_preview" not in json.loads(c.contract_json)
    monkeypatch.setattr(paper.settings, "PAPER_MIN_COMMISSION", 6)
    assert execution._validate_candidate_clock(c, full_request(c),
        phase="lock_acquired", clock=lambda: NOW) == (NOW, None)
    assert "fee_preview" not in json.loads(full_frozen().contract_json)


@pytest.mark.parametrize("close,rate", [(10.0, 0.000005), (10.03, 0.0003), (0.05, 0.001)])
def test_sell_fragment_rounding_is_exact_existing_book_not_whole_original(
        audited_fixture, monkeypatch, close, rate):
    monkeypatch.setattr(paper.settings, "PAPER_COMMISSION_RATE", rate)
    monkeypatch.setattr(paper.settings, "PAPER_MIN_COMMISSION", 0)
    monkeypatch.setattr(paper.settings, "PAPER_STAMP_TAX_RATE", rate)
    order, payload = sell(original()), selling_feed()
    order.price = 0.01
    proof = json.loads(order.risk_json)
    proof["paper_after_hours_intent"]["original_limit_price"] = order.price
    order.risk_json = _json(proof)
    payload["official_close"]["price"] = close
    payload["initial"]["orders"][0]["limit_price"] = close
    first = json.loads(freeze(payload, order).contract_json)
    more = append_offer(payload, identifier="new", shares=100, at=AT+timedelta(seconds=2), frame="next")
    more["events"][-1]["order"].update(side="buy", limit_price=close)
    second = json.loads(freeze(more, simulated_state(order, 100), dispatch=AT+timedelta(seconds=3),
        priors=[first["allocation"]]).contract_json)
    for fragment in (first, second):
        preview = fragment["fee_preview"]
        gross = close * fragment["fragment_quantity"]
        assert preview["gross_value"] == gross
        assert preview["commission"] == paper._commission(gross)
        assert preview["tax"] == paper._stamp_tax(gross)
        assert preview["cash_change_modeled"] == gross - paper._commission(gross) - paper._stamp_tax(gross)
    if close == 10.0:
        # Keep the old binary float / round boundary, not an original-order tax.
        assert first["fee_preview"]["tax"] + second["fee_preview"]["tax"] == 0.02
        assert paper._stamp_tax(2000) == 0.01


def test_fee_freeze_keeps_supplied_feed_state_and_original_history_unchanged(audited_fixture, fee_model):
    order, payload = original(), capacity(150)
    first = json.loads(freeze(payload, order).contract_json)["allocation"]
    state = simulated_state(order, 100)
    more = append_offer(payload, identifier="new", shares=100, at=AT+timedelta(seconds=2), frame="next")
    priors = [first]
    before = deepcopy((vars(state), more, priors))
    result = freeze(more, state, dispatch=AT+timedelta(seconds=3), priors=priors)
    assert (vars(state), more, priors) == before
    assert json.loads(result.contract_json)["fee_preview"]["cash_reserved"] is False
