"""Supplied partial book CANDIDATES only, not actual broker/DB/receipt certification."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from app.data.after_hours import _json
from app.models.trading import TradeFill
from app.trading import paper_after_hours_resources as resources
from app.trading import paper_after_hours_execution as execution
from app.trading import paper_after_hours_allocation as allocation
from test_paper_after_hours_allocation_20261002 import AT, NOW, audited_fixture
from test_paper_after_hours_partial_contract_20261002 import original, freeze, request
from test_paper_after_hours_partial_allocation_20261002 import capacity, append_offer, simulated_state
from test_paper_after_hours_partial_fees_20261002 import fee_model, sell, selling_feed
from test_paper_after_hours_contract_20261002 import frozen as full_frozen

BOOK_FIELDS = ("id", "code", "trade_type", "price", "amount", "commission", "tax",
               "realized_pnl", "signal_id", "strategy_version", "decision_round_id", "fill_round_id")


def book(candidate, *, sequence=1):
    """Fabricate future row views; no DB inserts, broker, ledger, cash or position."""
    contract = json.loads(candidate.contract_json)
    req = request(candidate)
    order = execution._restore_candidate_order(json.loads(candidate.order_json))
    at = allocation._clock(contract["dispatch_validated_at"])
    lock, _ = execution._validate_partial_candidate_clock(
        candidate, req, phase="lock_acquired", clock=lambda: at)
    _, timing = execution._validate_partial_candidate_clock(
        candidate, req, phase="before_mutation", lock_checked_at=lock, clock=lambda: at)
    preview = contract["fee_preview"]
    account = SimpleNamespace(id=contract["account_numeric_id"], account_name=order.account_id)
    trade = SimpleNamespace(id=100+sequence, account_id=account.id, code=order.code, trade_type=order.side,
        price=contract["fill_price"], amount=contract["fragment_quantity"], commission=preview["commission"],
        tax=preview["tax"], realized_pnl=None if order.side == "buy" else 2.0,
        signal_id=contract["signal_id"], strategy_version=order.strategy_version,
        decision_round_id=order.decision_round_id, fill_round_id=contract["quote_round_id"], trade_time=at)
    fill = SimpleNamespace(id=sequence, fill_id=contract["expected_fill_id"], order_id=order.order_id,
        broker="paper", broker_trade_id=str(trade.id), code=order.code, side=order.side,
        price=trade.price, quantity=trade.amount, commission=trade.commission, tax=trade.tax,
        realized_pnl=trade.realized_pnl, decision_round_id=trade.decision_round_id,
        fill_round_id=trade.fill_round_id, trade_date=order.trade_date, filled_at=at,
        raw_json=_json({**{key: getattr(trade, key) for key in BOOK_FIELDS},
            "trade_time": at.isoformat(), "after_hours_fixed_execution": contract,
            "ledger_execution_timing": timing}))
    return candidate, req, order, fill, trade, account, timing


def bind(item, *, priors=(), cutoff=None):
    cutoff = item[4].trade_time if cutoff is None else cutoff
    return resources._partial_book_candidate_binding(*item, cutoff=cutoff, prior_fragments=priors)


def refresh_raw(item):
    raw = json.loads(item[3].raw_json)
    raw.update({key: getattr(item[4], key) for key in BOOK_FIELDS})
    raw["trade_time"] = item[4].trade_time.isoformat()
    item[3].raw_json = _json(raw)


def second_and_third():
    order, payload = original(), capacity(150)
    first = book(freeze(payload, order))
    prior1 = json.loads(first[0].contract_json)["allocation"]
    more = append_offer(payload, identifier="new", shares=100, at=AT+timedelta(seconds=2), frame="next")
    second = book(freeze(more, simulated_state(order, 100), dispatch=AT+timedelta(seconds=3),
        priors=[prior1]), sequence=2)
    prior2 = json.loads(second[0].contract_json)["allocation"]
    last = append_offer(more, identifier="last", shares=50, at=AT+timedelta(seconds=4), frame="last")
    third = book(freeze(last, simulated_state(order, 200), dispatch=AT+timedelta(seconds=5),
        priors=[prior1, prior2]), sequence=3)
    return first, second, third


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_candidate_matches_book_fields_fees_and_fixed_price_without_authority(audited_fixture, fee_model, side):
    c = freeze() if side == "buy" else freeze(selling_feed(), sell(original()))
    item = book(c)
    before = deepcopy([vars(row) for row in item[2:6]])
    out = bind(item)
    assert out["status"] == "validated_partial_book_candidate"
    assert out["fragment_quantity"] == 100 and out["cumulative_before"] == 0
    assert out["cumulative_after"] == 100 and out["remaining_quantity_after"] == 200
    assert out["supplied_rows_only"] is True
    for key in ("database_reads_certified", "execution_authorized", "ledger_contract_supported",
                "durable_resources_certified", "cash_change_certified", "realized_pnl_certified"):
        assert out[key] is False
    assert [vars(row) for row in item[2:6]] == before


def test_three_original_fragments_require_complete_distinct_book_history(audited_fixture, fee_model):
    first, second, third = second_and_third()
    assert bind(second, priors=[first[3:5]])["cumulative_after"] == 200
    out = bind(third, priors=[first[3:5], second[3:5]])
    assert out["cumulative_after"] == 300 and out["remaining_quantity_after"] == 0
    assert out["prior_original_book_count"] == 2
    assert len({row[3].fill_id for row in (first, second, third)}) == 3
    assert first[2].id == second[2].id == third[2].id


@pytest.mark.parametrize("damage", ["history_pk_reused", "current_pk_reused"])
def test_fill_numeric_primary_key_cannot_name_two_different_rows(audited_fixture, fee_model, damage):
    first, second, third = second_and_third()
    if damage == "history_pk_reused":
        second[3].id = first[3].id
    else:
        third[3].id = first[3].id
    with pytest.raises(ValueError):
        bind(third, priors=[first[3:5], second[3:5]])


@pytest.mark.parametrize("damage", ["trade_account_float", "fill_bool_tax", "trade_bool_price"])
def test_same_value_wrong_economic_row_types_are_not_valid_candidates(audited_fixture, fee_model, damage):
    payload = capacity(150)
    if damage == "trade_bool_price":
        payload["official_close"]["price"] = 1
        payload["initial"]["orders"][0]["limit_price"] = 1
    item = book(freeze(payload))
    if damage == "trade_account_float": item[4].account_id = 7.0
    elif damage == "fill_bool_tax": item[3].tax = False
    else: item[4].price = True
    with pytest.raises(ValueError):
        bind(item)


@pytest.mark.parametrize("damage", ["missing", "reverse", "repeat", "extra", "wrong_account", "wrong_fee", "reuse_trade"])
def test_bad_or_incomplete_actual_original_history_never_becomes_book_bound(audited_fixture, fee_model, damage):
    first, second, third = second_and_third()
    priors = [first[3:5], second[3:5]]
    if damage == "missing": priors.pop()
    elif damage == "reverse": priors.reverse()
    elif damage == "repeat": priors[1] = priors[0]
    elif damage == "extra": priors.append(priors[0])
    elif damage == "wrong_account": first[4].account_id = 8
    elif damage == "wrong_fee":
        first[3].commission = first[4].commission = 0
        refresh_raw(first)
    else:
        third[4].id = first[4].id
        third[3].broker_trade_id = str(first[4].id)
        refresh_raw(third)
    with pytest.raises(ValueError):
        bind(third, priors=priors)


@pytest.mark.parametrize("field,value", [
    ("decision_round_id", "other"), ("strategy_version", "other"), ("signal_id", "other"),
    ("local_intent_hash", "0"*64), ("local_acceptance_sequence", 2),
    ("local_accepted_at", (AT-timedelta(seconds=2)).isoformat()), ("original_quantity", 300.0),
])
def test_historical_top_level_identity_cannot_disagree_with_book_and_proposal(
        audited_fixture, fee_model, field, value):
    first, second, _ = second_and_third()
    raw = json.loads(first[3].raw_json)
    raw["after_hours_fixed_execution"][field] = value
    raw["ledger_execution_timing"]["input_sha256"] = resources._digest(raw["after_hours_fixed_execution"])
    first[3].raw_json = _json(raw)
    with pytest.raises(ValueError):
        bind(second, priors=[first[3:5]])


@pytest.mark.parametrize("target,field,value", [
    (2, "filled_quantity", 100), (2, "status", "canceled"), (2, "price", 10),
    (3, "broker_trade_id", "999"), (3, "quantity", 300), (3, "price", 10.5),
    (3, "fill_id", "fill-other"), (3, "order_id", "other"),
    (3, "trade_date", AT.date()-timedelta(days=1)),
    (4, "account_id", 8), (4, "code", "000001"), (4, "trade_type", "sell"),
    (4, "commission", None), (4, "commission", -1), (4, "tax", float("nan")),
    (4, "amount", 100.0), (4, "id", True), (4, "signal_id", "other"),
    (4, "strategy_version", "other"), (4, "fill_round_id", "other"),
    (4, "realized_pnl", 0), (5, "id", 7.0), (5, "account_name", "promotion"),
])
def test_current_book_identity_or_fees_conflict_rejected(audited_fixture, fee_model, target, field, value):
    item = book(freeze())
    setattr(item[target], field, value)
    with pytest.raises(ValueError):
        bind(item)


@pytest.mark.parametrize("key,value", [("id", 101.0), ("amount", 100.0), ("tax", False),
    ("commission", False), ("account_numeric_id", 7.0), ("ledger_contract_supported", 0)])
def test_same_value_wrong_raw_types_cannot_alias_typed_candidate(audited_fixture, fee_model, key, value):
    item = book(freeze())
    raw = json.loads(item[3].raw_json)
    if key in ("account_numeric_id", "ledger_contract_supported"):
        raw["after_hours_fixed_execution"][key] = value
    else:
        raw[key] = value
    item[3].raw_json = _json(raw)
    with pytest.raises(ValueError):
        bind(item)


@pytest.mark.parametrize("damage", ["future", "guard", "hash", "fragment", "fee", "request", "authority"])
def test_changed_clock_diagnostic_rejected(audited_fixture, fee_model, damage):
    item = book(freeze())
    if damage == "future": item[6]["before_mutation_checked_at"] = (NOW+timedelta(seconds=1)).isoformat()
    elif damage == "guard": item[6]["guard_version"] = execution.LEDGER_VERSION
    elif damage == "hash": item[6]["input_sha256"] = "0" * 64
    elif damage == "fragment": item[6]["fragment_index"] = 2
    elif damage == "fee": item[6]["fee_model_sha256"] = "0" * 64
    elif damage == "request": item[6]["request_id"] = "wrong"
    else: item[6]["execution_authorized"] = True
    with pytest.raises(ValueError):
        bind(item)


def test_request_and_fill_both_fit_existing_storage_without_schema_change(audited_fixture, fee_model):
    item = book(freeze())
    contract = json.loads(item[0].contract_json)
    assert len(contract["request_id"]) == 35
    assert len(item[3].fill_id) == TradeFill.fill_id.type.length == 40
    assert item[3].fill_id == "fill-" + item[1].order_id


@pytest.mark.parametrize("version", ["after_hours_partial_frozen_candidate_v1_20261002",
                                    "after_hours_partial_frozen_candidate_v2_20261002"])
def test_old_partial_version_not_reinterpreted_as_book_bound_v3(audited_fixture, fee_model, version):
    item = book(freeze())
    changed = (replace(item[0], protocol_version=version), *item[1:])
    with pytest.raises(ValueError):
        bind(changed)


def test_historical_fragment_dispatch_cannot_precede_previous_book(audited_fixture, fee_model):
    first, second, third = second_and_third()
    for item, at in ((first, AT+timedelta(seconds=4)),
                     (second, AT+timedelta(seconds=4, milliseconds=500))):
        item[4].trade_time = item[3].filled_at = at
        item[6]["before_mutation_checked_at"] = at.isoformat()
        raw = json.loads(item[3].raw_json)
        raw["trade_time"] = at.isoformat()
        raw["ledger_execution_timing"] = item[6]
        item[3].raw_json = _json(raw)
    # The row clocks are ordered, but fragment 2 dispatch occurred before book 1.
    with pytest.raises(ValueError):
        bind(third, priors=[first[3:5], second[3:5]])


def test_delayed_causal_books_remain_valid_supplied_candidates(audited_fixture, fee_model):
    first, second, third = second_and_third()
    for item, at in ((first, AT+timedelta(seconds=2)),
                     (second, AT+timedelta(seconds=4))):
        item[4].trade_time = item[3].filled_at = at
        item[6]["before_mutation_checked_at"] = at.isoformat()
        raw = json.loads(item[3].raw_json)
        raw["trade_time"] = at.isoformat()
        raw["ledger_execution_timing"] = item[6]
        item[3].raw_json = _json(raw)
    # A delayed book is valid when it precedes the next fragment dispatch.
    out = bind(third, priors=[first[3:5], second[3:5]])
    assert out["cumulative_after"] == 300
    assert out["database_reads_certified"] is out["execution_authorized"] is False


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("field", ["commission", "tax"])
def test_zero_fee_bool_alias_is_rejected_in_current_and_history(
        audited_fixture, fee_model, monkeypatch, historical, field):
    from app.api.v1 import paper
    for key in ("PAPER_COMMISSION_RATE", "PAPER_MIN_COMMISSION", "PAPER_STAMP_TAX_RATE"):
        monkeypatch.setattr(paper.settings, key, 0.0)
    first, second, third = second_and_third()
    priors = [first[3:5], second[3:5]]
    assert bind(third, priors=priors)["cumulative_after"] == 300
    target = first if historical else third
    assert getattr(target[3], field) == getattr(target[4], field) == 0.0
    setattr(target[3], field, False)
    with pytest.raises(ValueError):
        bind(third, priors=priors)


def test_full_default_consumer_cannot_accept_partial_candidate(audited_fixture, fee_model):
    item = book(freeze())
    contract = json.loads(item[0].contract_json)
    with pytest.raises(ValueError):
        resources._economic_binding(item[2], item[3], item[4], item[5], contract, item[6],
            cutoff=NOW, current=True)
    with pytest.raises(ValueError):
        bind((full_frozen(), *item[1:]))


def test_parser_total_budget_exact_and_one_byte_short(audited_fixture, fee_model, monkeypatch):
    item = book(freeze())
    c = item[0]
    parts = (c.contract_json, c.feed_json, c.order_json, c.priors_json, c.local_orders_json,
             item[2].risk_json, item[3].raw_json)
    budget = sum(len(p.encode()) for p in parts)
    monkeypatch.setattr(execution, "MAX_PARTIAL_CANDIDATE_BYTES", budget)
    assert bind(item)["fragment_quantity"] == 100
    monkeypatch.setattr(execution, "MAX_PARTIAL_CANDIDATE_BYTES", budget-1)
    with pytest.raises(ValueError, match="total_byte_budget"):
        bind(item)


def test_oversized_old_raw_is_rejected_before_json_parse(audited_fixture, fee_model, monkeypatch):
    item = book(freeze())
    old = SimpleNamespace(raw_json="x" * (allocation.MAX_BYTES+1))
    def forbidden(*args, **kwargs):
        raise AssertionError("must bound supplied text BEFORE parsing")
    monkeypatch.setattr(resources.json, "loads", forbidden)
    with pytest.raises(ValueError, match="byte_budget"):
        bind(item, priors=[(old, SimpleNamespace())])


@pytest.mark.parametrize("prior", [None, [None], [(None,)], "invalid"])
def test_malformed_prior_pairs_are_bounded_fail_closed(audited_fixture, fee_model, prior):
    with pytest.raises(ValueError):
        bind(book(freeze()), priors=prior)
