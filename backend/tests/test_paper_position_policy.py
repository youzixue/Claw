"""退出必须读取首次实际建仓订单；不同账户、未来证据、旧轮次不串用。"""
import json
from datetime import datetime, timedelta

import pytest

from app.api.v1 import paper
from app.models.paper import PaperPosition, PaperTradeLog
from app.models.trading import TradeOrder, TradeFill
from app.paper.position_policy import position_exit_policy
from test_paper_api import paper_client


async def seed(db, *, evidence=None, order_account="tenbagger", order_version="entry-v1"):
    at = datetime(2026, 9, 7, 14, 42)
    account = await paper._get_or_create_account(db, "tenbagger")
    position = PaperPosition(account_id=account.id, code="600001", buy_price=10,
                             buy_amount=100, buy_time=at, strategy_version="entry-v1",
                             stop_loss_price=9.4, hold_days=1, is_closed=False)
    trade = PaperTradeLog(account_id=account.id, code="600001", trade_type="buy",
                         price=10, amount=100, trade_time=at, strategy_version="entry-v1")
    db.add_all([position, trade])
    await db.flush()
    evidence = evidence if evidence is not None else {
        "observed_at": (at-timedelta(seconds=30)).isoformat(),
        "strategy_version": "entry-v1",
        "exit_parameters": {"take_profit_pct":18, "stop_loss_pct":6, "max_hold_days":3},
    }
    order = TradeOrder(order_id="entry-order", broker="paper", account_id=order_account,
                       code="600001", side="buy", order_type="limit", price=10, quantity=100,
                       filled_quantity=100, status="filled", strategy_version=order_version,
                       trade_date=at.date(), created_at=at-timedelta(seconds=30),
                       risk_json=json.dumps({"experiment_entry":evidence}))
    fill = TradeFill(fill_id="entry-fill", order_id=order.order_id, broker="paper",
                     code="600001", side="buy", price=10, quantity=100,
                     broker_trade_id=str(trade.id), filled_at=at, trade_date=at.date())
    db.add_all([order, fill])
    await db.flush()
    return position, at, order


@pytest.mark.asyncio
async def test_entry_exit_policy_survives_changed_current_settings(paper_client):
    _, maker = paper_client
    async with maker() as db:
        position, at, order = await seed(db)
        original_risk = order.risk_json
        params, trace = await position_exit_policy(
            db, account_name="tenbagger", position=position,
            defaults={"take_profit_pct":1, "stop_loss_pct":1, "max_hold_days":1},
            as_of=at+timedelta(days=1))
        assert params == {"take_profit_pct":18, "stop_loss_pct":6, "max_hold_days":3}
        assert trace["basis"] == "frozen_entry_order"
        assert trace["order_id"] == "entry-order"
        assert paper._midline_sell_reason(position, {"price":11}, 10, 1, params=params) == ""
        assert order.risk_json == original_risk
        assert position.strategy_version == "entry-v1"


@pytest.mark.parametrize("account,version,expected", [
    ("promotion", "entry-v1", "legacy_or_missing_entry_evidence"),
    ("tenbagger", "other-version", "entry_version_mismatch"),
])
@pytest.mark.asyncio
async def test_other_account_or_version_never_supplies_exit_policy(paper_client, account, version, expected):
    _, maker = paper_client
    defaults = {"take_profit_pct":8, "stop_loss_pct":5, "max_hold_days":3}
    async with maker() as db:
        position, at, _ = await seed(db, order_account=account, order_version=version)
        params, trace = await position_exit_policy(db, account_name="tenbagger", position=position,
                                                  defaults=defaults, as_of=at+timedelta(days=1))
        assert params == defaults
        assert trace["basis"] == expected


@pytest.mark.parametrize("clock", ["2026-09-08T15:00:00", "invalid", "2026-09-07T14:41:30+08:00"])
@pytest.mark.asyncio
async def test_future_or_invalid_entry_clock_is_not_frozen_evidence(paper_client, clock):
    _, maker = paper_client
    async with maker() as db:
        position, at, _ = await seed(db, evidence={
            "strategy_version":"entry-v1", "observed_at":clock,
            "exit_parameters":{"take_profit_pct":99}})
        params, trace = await position_exit_policy(db, account_name="tenbagger", position=position,
                                                  defaults={"take_profit_pct":8}, as_of=at+timedelta(days=1))
        assert params["take_profit_pct"] == 8
        assert trace["basis"] == "entry_clock_invalid"


@pytest.mark.asyncio
async def test_partial_legacy_snapshot_is_explicit_and_preserves_zero(paper_client):
    _, maker = paper_client
    async with maker() as db:
        position, at, _ = await seed(db, evidence={
            "strategy_version":"entry-v1", "observed_at":"2026-09-07T14:41:30",
            "exit_parameters":{"next_day_min_profit_pct":0, "take_profit_pct":float("nan")}})
        params, trace = await position_exit_policy(db, account_name="tenbagger", position=position,
            defaults={"next_day_min_profit_pct":1, "take_profit_pct":18}, as_of=at+timedelta(days=1))
        assert params == {"next_day_min_profit_pct":0, "take_profit_pct":18}
        assert trace["basis"] == "partial_legacy_snapshot"
        assert trace["missing_keys"] == ["take_profit_pct"]
