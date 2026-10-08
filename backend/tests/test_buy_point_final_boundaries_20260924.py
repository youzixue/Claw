"""Independent-review findings: real late partial block and actual UTF-8 packing."""
import json
from datetime import timedelta

import pytest

from app.models.paper import PaperTradeLog, PaperAutoTradeLog
from app.models.trading import TradeOrder, TradeFill
from app.push import paper_buy_points as points
from test_paper_buy_points import setup, record, logs
from test_buy_point_actionability_20260924 import receipt, after_lease
from test_paper_buy_point_delivery_clock import advancing_clock


@pytest.mark.asyncio
async def test_partial_fill_then_risk_block_must_not_deny_existing_fill(setup):
    maker, now, send = setup
    signal = await record(maker, now)
    async with maker() as db:
        db.add(receipt(now))
        db.add(PaperTradeLog(id=123, account_id=signal.account_id, code=signal.code,
            trade_type="buy", price=10.4, amount=100, trade_time=now,
            strategy_version=signal.strategy_version, decision_round_id="round-current",
            fill_round_id="next-round"))
        db.add(TradeOrder(order_id="own-order", broker="paper", account_id="default",
            code=signal.code, source="test_route", side="buy", order_type="limit", price=10.5,
            quantity=200, filled_quantity=100, status="partial",
            strategy_version=signal.strategy_version, trade_date=now.date(), created_at=now,
            decision_at=now, decision_round_id="round-current"))
        db.add(TradeFill(fill_id="isolated-fill", order_id="own-order", broker="paper",
            broker_trade_id="123", code=signal.code, side="buy", price=10.4, quantity=100,
            trade_date=now.date(), filled_at=now, decision_round_id="round-current",
            fill_round_id="next-round"))
        await db.commit()
    async def blocked():
        async with maker() as db:
            from sqlalchemy import select
            order = await db.scalar(select(TradeOrder))
            order.status = "risk_blocked"
            db.add(receipt(now, run_id="late-risk", action="skip_buy", decision="blocked",
                candidate_json=json.dumps({"deferred_order_observation": {
                    "order_response": {"order_id": "own-order", "status": "risk_blocked"}}}),
                reason="余量被本轮锁后风控阻断"))
            await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=after_lease(maker, now, blocked))
    message = send.call_args.args[0]
    assert message.extra["signal_audits"][0]["execution_state"] == "blocked"
    assert "未买入" not in message.extra["display_title"]
    assert "本轮未下单" not in message.content
    assert "历史成交以账本为准" in message.content
    async with maker() as db:
        assert (await db.get(PaperTradeLog, 123)).amount == 100


@pytest.mark.asyncio
async def test_expired_card_cannot_occupy_actual_bytes_and_starve_fresh_card(setup, monkeypatch):
    maker, now, send = setup
    a = await record(maker, now, market_context={
        "price": 10.5, "quote_round_id": "round-current",
        "source_quote_at": (now-timedelta(seconds=175)).isoformat()})
    b = await record(maker, now, account="challenger_a", market_context={
        "price": 10.5, "quote_round_id": "round-current",
        "source_quote_at": (now-timedelta(seconds=160)).isoformat()})
    samples = [points._item(a), points._item(b)]
    async with maker() as db:
        for sample in samples:
            await points._refresh_execution_snapshot(db, sample, now=now)
    def size(items):
        return len(json.dumps(points.feishu_channel._build_card(
            points.build_message(items)), ensure_ascii=False).encode("utf-8"))
    cap = size(samples) + 10
    longer = {**samples[1], "execution_state": "blocked", "execution_note": "约束" * 120}
    assert size([samples[0], longer]) > cap
    assert size([longer]) <= cap
    monkeypatch.setattr(points, "FEISHU_CARD_BUDGET_BYTES", cap)
    clock = advancing_clock(monkeypatch)
    async def late():
        clock["seconds"] = 10
        async with maker() as db:
            db.add(receipt(now, account_id=b.account_id, action="skip_buy", decision="blocked",
                created_at=now+timedelta(seconds=10), reason="约束" * 120, candidate_json="{}"))
            await db.commit()
    result = await points.dispatch_buy_points(now=now, session_factory=after_lease(maker, now, late))
    assert result["count"] == 1 and send.call_count == 1
    message = send.call_args.args[0]
    assert message.extra["signal_ids"] == [b.id]
    assert size([longer]) <= cap
    rows = await logs(maker)
    assert any(row.run_id == a.run_id and row.decision == "expired" for row in rows)
    assert any(row.run_id == b.run_id and row.decision == "sent" for row in rows)
    assert not any(row.run_id == b.run_id and row.decision == "expired" for row in rows)
