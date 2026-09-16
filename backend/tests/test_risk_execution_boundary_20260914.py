"""风险异常/未知返回/大小写方向不能绕过正式执行边界。仅隔离DB。"""
import json
from datetime import date, datetime, time, timedelta
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select

from app.risk.engine import RiskEngine, RiskRule, RiskContext, RiskCategory, RiskLevel, RiskDecision
from app.trading import service
from app.models.stock import StockTag, StockSpot
from app.models.trading import TradeFill
from app.models.paper import PaperTradeLog
from test_trading_api import trading_client
from test_paper_deferred_exit_provenance import memory_session


class Rule(RiskRule):
    rule_name = "boundary_test"
    category = RiskCategory.BLACKLIST

    def __init__(self, response=None, error=None):
        self.response, self.error = response, error

    def check(self, ctx):
        if self.error:
            raise self.error
        return self.response


def passed():
    return RiskDecision(rule_name="boundary_test", category=RiskCategory.BLACKLIST,
                        level=RiskLevel.PASS)


@pytest.mark.parametrize("action,expected", [("buy", "block"), ("sell", "block"), ("hold", "warn")])
@pytest.mark.parametrize("error", [ValueError("bad values"), RuntimeError("missing service"), ZeroDivisionError()])
def test_rule_exception_is_not_success(action, expected, error):
    engine = RiskEngine()
    engine.register(Rule(error=error))
    result = engine.check(RiskContext(code="000001", action=action))
    assert result["final_level"] == expected
    assert result["evaluation_status"] == "incomplete"
    assert result["checked_rules"] == 1
    assert result["evaluation_errors"][0]["reason_code"] == "risk_rule_evaluation_failed"
    assert result["decisions"][0]["category"] == "engine"
    assert bool(result["block_reasons"]) is (action != "hold")
    assert bool(result["warnings"]) is (action == "hold")
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("response", [
    None, {}, {"level": "pass"}, True,
    RiskDecision("boundary_test", RiskCategory.BLACKLIST, "pass"),
    RiskDecision("boundary_test", "blacklist", RiskLevel.PASS),
    RiskDecision("different_rule", RiskCategory.BLACKLIST, RiskLevel.PASS),
    RiskDecision("boundary_test", RiskCategory.BLACKLIST, RiskLevel.PASS, message={}),
    RiskDecision("boundary_test", RiskCategory.BLACKLIST, RiskLevel.PASS, suggestion=[]),
])
def test_malformed_rule_decision_fails_before_append_and_serialization(response):
    engine = RiskEngine()
    engine.register(Rule(response=response))
    result = engine.check(RiskContext(code="000001", action="buy"))
    assert result["final_level"] == "block"
    assert len(result["decisions"]) == 1
    assert result["decisions"][0]["level"] == "block"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("all_disabled", [False, True])
@pytest.mark.parametrize("action,expected", [("buy", "block"), ("sell", "block"), ("hold", "warn")])
def test_empty_or_disabled_risk_chain_does_not_authorize(all_disabled, action, expected):
    engine = RiskEngine()
    if all_disabled:
        rule = Rule(response=passed())
        rule.enabled = False
        engine.register(rule)
    result = engine.check(RiskContext(action=action))
    assert result["checked_rules"] == 0
    assert result["final_level"] == expected
    assert result["evaluation_errors"][0]["reason_code"] == "no_enabled_risk_rules"


@pytest.mark.parametrize("action", ["BUY", "Sell ", "", None, False, "liquidate"])
def test_invalid_risk_action_cannot_skip_rule_branches(action):
    engine = RiskEngine()
    engine.register(Rule(response=passed()))
    result = engine.check(RiskContext(action=action))
    assert result["final_level"] == "block"
    assert result["evaluation_errors"][0]["reason_code"] == "invalid_risk_action"


def test_one_failed_rule_cannot_be_outvoted_by_a_passing_rule():
    engine = RiskEngine()
    engine.register(Rule(response=passed()))
    other = Rule(error=RuntimeError("unavailable"))
    other.rule_name = "failed_rule"
    engine.register(other)
    result = engine.check(RiskContext(action="buy"))
    assert result["final_level"] == "block"
    assert result["checked_rules"] == 2 and len(result["decisions"]) == 2
    assert len(result["evaluation_errors"]) == 1


def test_successful_check_retains_original_decision():
    engine = RiskEngine()
    engine.register(Rule(response=passed()))
    result = engine.check(RiskContext(action="buy"))
    assert result["final_level"] == "pass"
    assert result["evaluation_status"] == "complete"
    assert result["evaluation_errors"] == []


@pytest.mark.parametrize("result", [
    None, {}, [], {"final_level": None}, {"final_level": True},
    {"final_level": {}}, {"final_level": "allow"},
    {"final_level": "pass", "evaluation_status": "incomplete"},
    {"final_level": "pass", "evaluation_status": {}},
    {"final_level": "pass", "block_reasons": [{"message": "risk"}]},
])
def test_submission_and_both_pending_paths_share_fail_closed_summary(result):
    assert service._effective_risk_level(result) == "block"


@pytest.mark.parametrize("level", ["pass", "warn", "block"])
def test_effective_risk_level_keeps_actual_valid_decision(level):
    assert service._effective_risk_level({
        "final_level": level, "evaluation_status": "complete", "block_reasons": [],
    }) == level


@pytest.mark.parametrize("side", ["BUY", " Buy ", "bUy"])
@pytest.mark.asyncio
async def test_normalized_buy_direction_cannot_skip_st_risk(trading_client, monkeypatch, side):
    _client, factory = trading_client
    fake_broker = type("NoFillBroker", (), {})()
    fake_broker.place_order = AsyncMock(side_effect=AssertionError("不能触达broker"))
    monkeypatch.setattr(service, "get_broker_adapter", lambda _name: fake_broker)
    async with factory() as db:
        db.add(StockTag(code="000002", name="ST测试", board_type="main_sz",
                       board_tag="tradeable", is_st=True))
        db.add(StockSpot(code="000002", name="ST测试", price=10))
        await db.commit()
        cmd = service.SubmitOrderCommand(code="000002", side=side, price=10, quantity=100)
        result = await service.submit_order(db, cmd)
        assert cmd.side == "buy"
        assert result["order"]["side"] == "buy"
        assert result["order"]["status"] == "risk_blocked"
        assert result["risk"]["action"] == "buy"
        assert result["risk"]["stock_status"]["is_st"] is True
        assert result["fills"] == []
        assert (await db.scalars(select(TradeFill))).all() == []
        assert (await db.scalars(select(PaperTradeLog))).all() == []
        fake_broker.place_order.assert_not_awaited()


@pytest.mark.parametrize("rules", ["empty", "raises", "malformed"])
@pytest.mark.asyncio
async def test_actual_submit_cannot_reach_broker_when_chain_unavailable(trading_client, monkeypatch, rules):
    _client, factory = trading_client
    engine = RiskEngine()
    if rules == "raises":
        engine.register(Rule(error=ValueError("failure")))
    elif rules == "malformed":
        engine.register(Rule(response=None))
    monkeypatch.setattr(service, "risk_engine", engine)
    fake_broker = type("NoFillBroker", (), {})()
    fake_broker.place_order = AsyncMock(side_effect=AssertionError("不能触达broker"))
    monkeypatch.setattr(service, "get_broker_adapter", lambda _name: fake_broker)
    async with factory() as db:
        result = await service.submit_order(db, service.SubmitOrderCommand(
            code="000001", side="buy", price=10, quantity=100))
        assert result["order"]["status"] == "risk_blocked"
        assert result["order"]["risk_level"] == "block"
        assert result["risk"]["evaluation_status"] == "incomplete"
        assert result["fills"] == []
        assert (await db.scalars(select(PaperTradeLog))).all() == []
        fake_broker.place_order.assert_not_awaited()


@pytest.mark.parametrize("risk", [
    {}, {"final_level": "allow"}, {"final_level": "pass", "evaluation_status": "incomplete"},
    {"final_level": "pass", "block_reasons": [{"message": "仍有拦截"}]},
])
@pytest.mark.asyncio
async def test_service_does_not_default_missing_conclusion_to_pass(trading_client, monkeypatch, risk):
    _client, factory = trading_client
    monkeypatch.setattr(service, "_pre_trade_risk_check", AsyncMock(return_value=risk))
    fake_broker = type("NoFillBroker", (), {})()
    fake_broker.place_order = AsyncMock(side_effect=AssertionError("不能触达broker"))
    monkeypatch.setattr(service, "get_broker_adapter", lambda _name: fake_broker)
    async with factory() as db:
        result = await service.submit_order(db, service.SubmitOrderCommand(
            code="000001", side="buy", price=10, quantity=100))
        assert result["order"]["status"] == "risk_blocked"
        assert result["fills"] == []
        fake_broker.place_order.assert_not_awaited()


@pytest.mark.parametrize("risk_result", [
    {}, {"final_level": "allow"},
    {"final_level": "pass", "evaluation_status": "incomplete"},
    {"final_level": "pass", "block_reasons": [{"message": "contradiction"}]},
])
@pytest.mark.asyncio
async def test_actual_deferred_recheck_blocks_unknown_risk_before_broker(
    memory_session, monkeypatch, risk_result,
):
    from test_pending_buy_validity import setup_order, reconcile, AT
    db = memory_session
    order, event, risk, broker = await setup_order(db, monkeypatch)
    original = json.loads(order.risk_json)["paper_deferred_order"]["candidate"]
    risk.return_value = risk_result
    result = await reconcile(db, monkeypatch, at=AT + timedelta(seconds=60))
    assert result[0]["event"] == "risk_blocked"
    assert order.risk_level == "block" and order.filled_quantity == 0
    assert risk.await_count == 2
    assert json.loads(order.risk_json)["paper_deferred_order"]["candidate"] == original
    broker.place_order.assert_not_awaited()
    assert (await db.scalars(select(TradeFill))).all() == []


@pytest.mark.parametrize("risk_result", [
    {}, {"final_level": "allow"},
    {"final_level": "pass", "evaluation_status": "incomplete"},
    {"final_level": "pass", "block_reasons": [{"message": "contradiction"}]},
])
@pytest.mark.asyncio
async def test_actual_limit_queue_recheck_blocks_unknown_risk_before_broker(
    trading_client, monkeypatch, risk_result,
):
    _client, factory = trading_client
    monkeypatch.setattr(service, "_requires_pending_buy_validity", lambda _order: False)
    risk = AsyncMock(return_value={"final_level": "pass", "block_reasons": [], "warnings": []})
    monkeypatch.setattr(service, "_pre_trade_risk_check", risk)
    fake_broker = type("NoFillBroker", (), {})()
    fake_broker.place_order = AsyncMock(side_effect=AssertionError("不能触达broker"))
    monkeypatch.setattr(service, "get_broker_adapter", lambda _name: fake_broker)
    at = datetime.combine(date.today(), time(10))
    async with factory() as db:
        spot = StockSpot(code="600500", name="回封排队样本", price=11, prev_close=10,
                         open=10.5, low=10.4, limit_up=11, volume=50000,
                         bid1_price=11, bid1_volume=1000, ask1_price=0, updated_at=at)
        db.add(spot)
        await db.commit()
        queued = await service.submit_order(db, service.SubmitOrderCommand(
            code="600500", side="buy", price=11, quantity=100, account_id="tenbagger",
            source="tenbagger_midline", queue_if_limit_up=True))
        assert queued["order"]["status"] == "submitted"
        risk.return_value = risk_result
        spot.volume = 51001
        spot.updated_at = at + timedelta(minutes=2)
        await db.commit()
        outcomes = await service.reconcile_paper_limit_up_orders(
            db, account_id="tenbagger", now=spot.updated_at)
        assert outcomes[0]["event"] == "risk_blocked"
        assert outcomes[0]["order"]["risk_level"] == "block"
        assert outcomes[0]["order"]["filled_quantity"] == 0
        fake_broker.place_order.assert_not_awaited()
        assert (await db.scalars(select(TradeFill))).all() == []
        assert (await db.scalars(select(PaperTradeLog))).all() == []
