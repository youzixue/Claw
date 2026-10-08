"""Official 2026 declaration bounds, not provider/eligibility/real-market certification."""
import json
from copy import deepcopy

import pytest
from sqlalchemy import func, select

from app.models.trading import TradeOrder

from app.data.after_hours import _json
from app.trading import paper_after_hours_allocation as allocator
from app.trading import paper_after_hours_execution as execution
from test_paper_after_hours_20261002 import environment, economics, request
from test_trading_api import trading_client
from test_paper_after_hours_allocation_20261002 import audited_fixture, feed, local, NOW
from test_paper_after_hours_partial_contract_20261002 import original, check


def board_order(code, quantity):
    value = local(quantity=quantity)
    value.code = code
    proof = json.loads(value.risk_json)
    proof["paper_after_hours_intent"]["code"] = code
    value.risk_json = _json(proof)
    return value


def board_feed(code, quantity=300):
    value = feed()
    value["code"] = code
    value["exchange"] = "SSE" if code.startswith(("6",)) else "SZSE"
    value["session_id"] = value["exchange"] + "." + code + ".2026-09-30"
    value["official_close"].update(code=code, exchange=value["exchange"])
    value["initial"]["orders"][0]["quantity"] = quantity
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"code": "688256", "quantity": 100},
    {"code": "689009", "quantity": 100},
    {"code": "688256", "quantity": 150, "side": "sell"},
    {"code": "688256", "quantity": 201},
    {"code": "000001", "quantity": 1000100},
    {"code": "600000", "quantity": 1000100},
    {"code": "300750", "quantity": 1000100},
    {"code": "000001", "price": 11.601},
])
async def test_invalid_dedicated_declaration_rejected_before_order_or_economics(environment, changes):
    before = await economics(environment)
    response = await environment.client.post("/trading/orders", json=request(**changes))
    # Raw cent precision is a request-model error; supported-board original
    # quantity bounds are service errors. Neither path persists an order.
    assert response.status_code == (422 if "price" in changes else 400), response.text
    assert await economics(environment) == before
    async with environment.maker() as db:
        assert await db.scalar(select(func.count()).select_from(TradeOrder)) == 0
    environment.forbidden.assert_not_awaited()


@pytest.mark.parametrize("code,quantity", [
    ("688256", 100), ("689009", 100), ("688256", 201),
    ("688256", 1000100), ("000001", 1000100),
    ("600000", 1000100), ("300750", 1000100),
])
@pytest.mark.parametrize("planner", [allocator.propose_allocations, allocator.propose_partial_allocations])
def test_invalid_original_cannot_reach_full_or_partial_plan(audited_fixture, code, quantity, planner):
    order, payload = board_order(code, quantity), board_feed(code)
    result = planner(payload, [order], now=NOW, scenario_account="default")
    assert result["status"] == "evidence_blocked"
    assert result["proposals"] == []
    assert result["fills"] == [] and result["execution_authorized"] is False


@pytest.mark.parametrize("code", ["688256", "689009"])
def test_star_original_two_hundred_can_match_one_hundred_fragment(audited_fixture, code):
    order = board_order(code, 200)
    payload = board_feed(code, 100)
    result = allocator.propose_partial_allocations(payload, [order], now=NOW, scenario_account="default")
    assert result["status"] == "proposal_only"
    assert result["proposals"][0]["quantity"] == 100
    assert result["proposals"][0]["original_quantity"] == 200
    assert result["proposals"][0]["remaining_quantity_after"] == 100
    assert result["execution_authorized"] is False


@pytest.mark.parametrize("code", ["688256", "689009"])
def test_star_frozen_fragment_clock_uses_original_minimum_not_request_quantity(audited_fixture, code):
    order = original(quantity=200)
    order.code = code
    proof = json.loads(order.risk_json)
    proof["paper_after_hours_intent"]["code"] = code
    order.risk_json = _json(proof)
    candidate = execution._freeze_partial_fill_candidate(board_feed(code, 100), order,
        local_orders=[order], account_numeric_id=7, quote_round_id="star.fragment.1", dispatch_at=NOW)
    contract = json.loads(candidate.contract_json)
    assert contract["original_quantity"] == 200 and contract["fragment_quantity"] == 100
    assert check(candidate) == (NOW, None)
    assert contract["execution_authorized"] is False


@pytest.mark.parametrize("code", ["688256", "689009", "300750", "600000", "000001"])
def test_exact_maximum_is_after_hours_not_continuous_auction_limit(audited_fixture, code):
    order, payload = board_order(code, 1_000_000), board_feed(code, 1_000_000)
    result = allocator.propose_allocations(payload, [order], now=NOW, scenario_account="default")
    assert result["status"] == "proposal_only", result
    assert result["proposals"][0]["quantity"] == 1_000_000
    assert result["execution_authorized"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"quantity": 100.0}, {"quantity": "100"},
    {"price": "11.60000000000000000000000000001"},
])
async def test_api_raw_closure_before_pydantic_numeric_coercion(environment, changes):
    before = await economics(environment)
    response = await environment.client.post("/trading/orders", json=request(**changes))
    assert response.status_code == 422, response.text
    assert await economics(environment) == before
    async with environment.maker() as db:
        assert await db.scalar(select(func.count()).select_from(TradeOrder)) == 0
    environment.forbidden.assert_not_awaited()


def test_ordinary_request_numeric_coercion_unchanged():
    from app.api.v1.trading import SubmitOrderRequest
    ordinary = SubmitOrderRequest.model_validate(request(order_type="limit",
        price="11.60000000000000000000000000001", quantity=100.0))
    assert ordinary.price == 11.6 and type(ordinary.quantity) is int
    valid = SubmitOrderRequest.model_validate(request(price="11.60"))
    assert valid.price == 11.6 and valid.quantity == 100


@pytest.mark.parametrize("code,minimum", [
    ("600000", 100), ("000001", 100), ("300750", 100), ("301001", 100),
    ("688256", 200), ("689009", 200),
])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_original_minimum_maximum_and_explicit_paper_subset(code, minimum, side):
    for quantity in (minimum, 1_000_000):
        value = execution.original_declaration_parameters(code, side, 10.01, quantity)
        assert value["paper_minimum_quantity"] == minimum
        assert value["paper_quantity_step"] == 100
        assert value["exchange_maximum_quantity"] == 1_000_000
        assert value["odd_original_and_residual_sale_exception_supported"] is False
        assert value["eligibility_or_execution_authority"] is False
    for quantity in (minimum - 1, 1_000_001, 1_000_100, 0, -100, True, 100.0):
        with pytest.raises(ValueError):
            execution.original_declaration_parameters(code, side, 10.01, quantity)


@pytest.mark.parametrize("price", [
    True, 0, -1, 10.001, "10.00000000000000000000000000001",
    "1e999999", float("nan"), float("inf"), None,
])
def test_original_price_tick_checked_without_float_rounding(price):
    with pytest.raises(ValueError):
        execution.original_declaration_parameters("600000", "buy", price, 100)


@pytest.mark.parametrize("field,value", [
    ("original_quantity", 100.0), ("original_quantity", True),
    ("original_limit_price", True), ("original_limit_price", 10.501),
])
def test_original_proof_same_value_type_aliases_fail_closed(field, value):
    order = board_order("600000", 100)
    risk = json.loads(order.risk_json)
    risk["paper_after_hours_intent"][field] = value
    order.risk_json = _json(risk)
    with pytest.raises(ValueError):
        execution.fifo_key(order)


@pytest.mark.asyncio
async def test_regular_registration_freezes_rule_subset_but_does_not_enable_matching(environment):
    before = await economics(environment)
    response = await environment.client.post("/trading/orders", json=request())
    assert response.status_code == 200, response.text
    result = response.json()
    parameters = result["risk"]["paper_after_hours_intent"]["declaration_parameters"]
    assert parameters["exchange"] == "SZSE"
    assert parameters["A_share_price_tick"] == "0.01"
    assert parameters["exchange_maximum_quantity"] == 1_000_000
    assert result["fills"] == [] and await economics(environment) == before
    environment.forbidden.assert_not_awaited()


def test_external_resource_is_not_an_original_local_declaration(audited_fixture):
    # A native residual/odd opposite order is source evidence, not a local
    # 200-share STAR declaration. Two slices may make one 100-share fragment.
    order, payload = board_order("688256", 200), board_feed("688256", 50)
    other = deepcopy(payload["initial"]["orders"][0])
    other.update(record_id="other.offer", sequence=2)
    payload["initial"]["orders"].append(other)
    payload["initial"]["sequence"] = payload["last_sequence"] = 2
    result = allocator.propose_partial_allocations(payload, [order], now=NOW, scenario_account="default")
    assert result["status"] == "proposal_only", result
    assert result["proposals"][0]["quantity"] == 100
    assert [r["shares"] for r in result["proposals"][0]["resources"]] == [50, 50]
    assert result["execution_authorized"] is False
