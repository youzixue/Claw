"""Numeric contract regressions. All DB fixtures remain isolated by conftest."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
from pydantic import ValidationError
import pytest

from app.api.v1 import paper, trading
from app.trading import service


@pytest.mark.parametrize("parser", [paper._to_float, service._to_float])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), "nan", "inf", "-inf", "1e400", True, False, 10**500, Decimal("sNaN")])
def test_numeric_parsers_reject_nonfinite_booleans_and_conversion_overflow(parser, value):
    assert parser(value) is None


@pytest.mark.parametrize("parser", [paper._to_float, service._to_float])
@pytest.mark.parametrize("value,expected", [(0, 0.), ("0", 0.), (-2, -2.), ("10.25", 10.25), (Decimal("0.125"), .125)])
def test_numeric_parsers_keep_genuine_zero_sign_and_numeric_strings(parser, value, expected):
    assert parser(value) == expected


@pytest.mark.parametrize("model", [paper.SimBuyRequest, paper.SimSellRequest, trading.SubmitOrderRequest])
@pytest.mark.parametrize("value", [float("inf"), "Infinity", "1e400", float("nan"), True, False])
def test_request_prices_fail_before_nonfinite_or_boolean_coercion(model, value):
    with pytest.raises(ValidationError):
        model(code="600001", side="buy", price=value, amount=100, quantity=100)


@pytest.mark.parametrize("model", [paper.SimBuyRequest, paper.SimSellRequest, trading.SubmitOrderRequest])
@pytest.mark.parametrize("price", [10, 10.25, "10.25"])
def test_request_price_compatibility_preserves_valid_numbers_and_strings(model, price):
    assert model(code="600001", side="buy", price=price, amount=100, quantity=100).price == float(price)


@pytest.mark.asyncio
@pytest.mark.parametrize("price,quantity", [
    (float("nan"), 100), (float("inf"), 100), (True, 100), ("bad", 100),
    (1e308, 100), (10**500, 100), (10, 100.), (10, True),
    (10, 2**63), (10, 10**500), (0, 100), (10, 150),
])
async def test_invalid_internal_orders_stop_before_risk_broker_or_database(monkeypatch, price, quantity):
    def untouched(*args, **kwargs):
        pytest.fail("invalid numeric command reached broker/account/risk boundary")
    monkeypatch.setattr(service, "_paper_only_guard_reason", untouched)
    with pytest.raises(HTTPException) as error:
        await service.submit_order(object(), service.SubmitOrderCommand(
            code="600001", side="buy", price=price, quantity=quantity))
    assert error.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [100, 1000, ((2**63-1)//100)*100])
async def test_valid_numeric_orders_still_enter_original_guard(monkeypatch, quantity):
    class ReachedOriginalGuard(Exception):
        pass
    def original(*args):
        raise ReachedOriginalGuard()
    monkeypatch.setattr(service, "_paper_only_guard_reason", original)
    with pytest.raises(ReachedOriginalGuard):
        await service.submit_order(object(), service.SubmitOrderCommand(
            code="600001", side="buy", price=10, quantity=quantity))


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -float("inf"), "1e400", True, 10**500])
def test_invalid_lot_input_is_no_order_not_scan_crash(value):
    assert paper._round_lot(value) == 0


@pytest.mark.parametrize("value,expected", [(0, 0), (-1, 0), (99.999, 0), (100, 100), (199.99, 100), (200, 200)])
def test_original_lot_rounding_boundary_preserved(value, expected):
    assert paper._round_lot(value) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"support_strength_score":float("inf")}, {"orderbook_imbalance":float("inf")},
    {"support_strength_score":float("nan")}, {"orderbook_imbalance":True},
])
async def test_bad_book_values_cannot_replace_unknown_main_funds(monkeypatch, fields):
    monkeypatch.setattr(paper, "_paper_main_fund_map", AsyncMock(return_value={}))
    candidate = {"code":"600001", "_source":"ma5_pullback"}
    from datetime import datetime
    now = datetime(2026,9,9,10)
    reason = await paper._confirm_candidate_main_fund(object(), candidate, SimpleNamespace(**fields),
        trade_date=now.date(), decision_at=now)
    assert reason
    assert candidate["main_fund_evidence"]["status"] == "unknown"


@pytest.mark.parametrize("hands,price,limit,ratio", [
    (float("inf"), 10, 10, .5), (float("nan"), 10, 10, .5),
    (1e308, 10, 10, 1), (10, 1e308, 1e308, .5),
    (10, 10, float("inf"), .5), (10, 10, 10, float("nan")),
    (10, 10, 10, float("inf")), (True, 10, 10, 1),
])
def test_depth_bad_values_do_not_crash_or_invent_execution(monkeypatch, hands, price, limit, ratio):
    monkeypatch.setattr(service.settings, "PAPER_DEPTH_MAX_PARTICIPATION_RATIO", ratio)
    spot = SimpleNamespace(ask1_price=price, ask1_volume=hands)
    assert service._depth_fill_plan(spot, side="buy", limit_price=limit, remaining_quantity=100) == (0,None,[])


def test_depth_original_fraction_and_price_tolerance_preserved(monkeypatch):
    monkeypatch.setattr(service.settings, "PAPER_DEPTH_MAX_PARTICIPATION_RATIO", .25)
    spot = SimpleNamespace(ask1_price=10., ask1_volume=8.)
    qty, average, levels = service._depth_fill_plan(spot, side="buy", limit_price=10., remaining_quantity=300)
    assert qty == 200 and average == 10.
    assert levels == [{"level":1, "price":10., "quantity":200}]


@pytest.mark.parametrize("checker", [paper._is_limit_up_queue_quote, service._is_sealed_limit_up_quote])
@pytest.mark.parametrize("ask", [float("inf"), float("nan"), True, "bad"])
def test_invalid_present_ask_cannot_become_sealed_queue(checker, ask):
    assert not checker(SimpleNamespace(price=11., limit_up=11., bid1_price=11., ask1_price=ask))


@pytest.mark.parametrize("checker", [paper._is_limit_up_queue_quote, service._is_sealed_limit_up_quote])
@pytest.mark.parametrize("ask", [None, 0])
def test_original_absent_or_zero_ask_queue_semantics_retained(checker, ask):
    assert checker(SimpleNamespace(price=11., limit_up=11., bid1_price=11., ask1_price=ask))


@pytest.mark.parametrize("side,fields,slippage", [
    ("buy", {"price":1e308}, .1), ("sell", {"price":1e308}, .1),
    ("buy", {"price":10., "ask1_price":float("inf")}, .1),
    ("sell", {"price":10., "bid1_price":float("nan")}, .1),
    ("buy", {"price":10., "limit_up":True}, .1),
    ("sell", {"price":10., "limit_down":"bad"}, .1),
    ("buy", {"price":10.}, float("nan")),
    ("sell", {"price":10.}, float("inf")),
    ("buy", {"price":10.}, True),
    ("sell", {"price":10.}, 100.),
])
def test_bad_execution_inputs_do_not_generate_prices(monkeypatch, side, fields, slippage):
    monkeypatch.setattr(paper.settings, "PAPER_EXECUTION_SLIPPAGE_PCT", slippage)
    assert paper._conservative_execution_price(SimpleNamespace(**fields), side) is None


@pytest.mark.parametrize("side,expected", [("buy", 10.01), ("sell", 9.99)])
def test_valid_execution_slippage_and_cent_rounding_preserved(monkeypatch, side, expected):
    monkeypatch.setattr(paper.settings, "PAPER_EXECUTION_SLIPPAGE_PCT", .1)
    assert paper._conservative_execution_price(SimpleNamespace(price=10.), side) == expected


@pytest.mark.parametrize("model", [paper.SimBuyRequest, paper.SimSellRequest, trading.SubmitOrderRequest])
@pytest.mark.parametrize("price,quantity", [(1e308,100), (10,2**63//100*100+100), (10,10**500)])
def test_api_rejects_unstorable_quantity_and_overflow_notional(model, price, quantity):
    with pytest.raises(ValidationError):
        model(code="600001", side="buy", price=price, amount=quantity, quantity=quantity)


@pytest.mark.parametrize("value", [float("inf"), float("nan"), True, "1e400", 10**500])
def test_invalid_optional_stop_never_reaches_new_position(value):
    with pytest.raises(ValidationError):
        paper.SimBuyRequest(code="600001", price=10, amount=100, stop_loss_price=value)


@pytest.mark.parametrize("value", [None, 0, -1, 9.5, "9.5"])
def test_optional_stop_finite_legacy_semantics_preserved(value):
    req = paper.SimBuyRequest(code="600001", price=10, amount=100, stop_loss_price=value)
    assert req.stop_loss_price == (None if value is None else float(value))


@pytest.mark.parametrize("value", ["100.5", "1e2"])
def test_lot_string_conversion_failure_does_not_crash_scan(value):
    assert paper._round_lot(value) == 0
