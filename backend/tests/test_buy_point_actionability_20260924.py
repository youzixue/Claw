"""Notification state boundaries; isolated SQLite, fake Feishu, no orders."""
import json
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.paper import PaperAutoTradeLog
from app.push import paper_buy_points as points
from test_paper_buy_points import setup, record, logs
from test_paper_buy_point_template import item


def receipt(now, **overrides):
    values = dict(account_id=1, run_id="real-decision", trade_date=now.date(),
        created_at=now, source="test_route", code="600001", strategy_version="version-one",
        action="deferred_buy", decision="wait", reason="原委托待撮合",
        candidate_json=json.dumps({"pending_order_id": "own-order"}))
    values.update(overrides)
    return PaperAutoTradeLog(**values)


def after_lease(maker, now, callback):
    class LeaseInterleaving(AsyncSession):
        async def commit(self):
            is_lease = any(isinstance(row, PaperAutoTradeLog)
                and row.action == points.DELIVERY and row.decision == "attempting"
                for row in self.new)
            await super().commit()
            if is_lease:
                await callback()
    return async_sessionmaker(maker.kw["bind"], class_=LeaseInterleaving, expire_on_commit=False)


@pytest.mark.asyncio
async def test_terminal_receipt_after_lease_replaces_stale_pending_card(setup):
    maker, now, send = setup
    await record(maker, now)
    async with maker() as db:
        db.add(receipt(now))
        await db.commit()
    async def terminal():
        async with maker() as db:
            db.add(receipt(now, run_id="later-consumer", action="skip_terminal", decision="skipped",
                reason="原委托已超过有效期", candidate_json=json.dumps({
                    "deferred_order_observation": {"order_response": {
                        "order_id": "own-order", "status": "canceled"}}})))
            await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=after_lease(maker, now, terminal))
    message = send.call_args.args[0]
    assert message.extra["signal_audits"][0]["execution_state"] == "canceled"
    assert "原委托已超过有效期" in message.content
    assert "已提交模拟委托 · 未确认成交" not in message.content
    delivered = [row for row in await logs(maker) if row.decision == "sent"][-1]
    payload = json.loads(delivered.candidate_json)
    assert payload["execution_snapshot"]["state"] == "canceled"
    assert payload["execution_snapshot"]["checked_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_decision_committed_after_lease_is_visible_without_fabricating_order(setup):
    maker, now, send = setup
    await record(maker, now)
    async def blocked():
        async with maker() as db:
            db.add(receipt(now, action="skip_buy", decision="skipped",
                reason="本账户每日新开仓额度已用尽", candidate_json="{}"))
            await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=after_lease(maker, now, blocked))
    message = send.call_args.args[0]
    assert message.extra["signal_audits"][0]["execution_state"] == "blocked"
    assert message.extra["display_title"].startswith("执行已拦截｜")
    assert "不得据此追买" in message.content
    assert "信号参考价 ¥10.50" in message.content


@pytest.mark.parametrize("state,prefix", [
    ("blocked", "执行已拦截"), ("expired", "原信号已失效"),
    ("waiting", "执行等待"), ("pending", "模拟委托·待成交"),
    ("filled", "模拟成交回执"), ("partial", "模拟部分成交"),
    ("canceled", "模拟委托·余量已撤"), ("unconfirmed", "条件确认·执行未核实"),
])
def test_header_does_not_market_every_state_as_buy_now(state, prefix):
    sample = item(execution_state=state)
    message = points.build_message([sample])
    assert message.extra["display_title"].startswith(prefix + "｜")
    assert "不是当前买入或加仓指令" in message.content
    assert "当前买入条件未重新认证" in message.content
    assert "原确认依据" in message.content
    assert message.title == "Claw 策略买点确认 · 1条 · 1"  # throttle unchanged


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["account", "code", "source", "version", "date", "future", "order", "missing"])
async def test_final_refresh_never_borrows_other_decision(setup, mismatch):
    maker, now, send = setup
    await record(maker, now)
    async with maker() as db:
        db.add(receipt(now))
        await db.commit()
    changes = {
        "account": {"account_id": 2}, "code": {"code": "600002"},
        "source": {"source": "other"}, "version": {"strategy_version": "other"},
        "date": {"trade_date": now.date() - timedelta(days=1)},
        "future": {"created_at": now + timedelta(seconds=1)},
        "order": {"candidate_json": json.dumps({"pending_order_id": "other-order"})},
        "missing": {"candidate_json": "{}"},
    }
    async def changed():
        async with maker() as db:
            db.add(receipt(now, run_id="other-consumer", action="skip_buy",
                decision="blocked", reason="不得借用其他关联", **changes[mismatch]))
            await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=after_lease(maker, now, changed))
    message = send.call_args.args[0]
    assert message.extra["signal_audits"][0]["execution_state"] == "pending"
    assert "不得借用其他关联" not in message.content


@pytest.mark.asyncio
async def test_final_bounded_read_unknown_clears_previous_pending(setup):
    maker, now, send = setup
    await record(maker, now)
    async with maker() as db:
        db.add(receipt(now))
        await db.commit()
    async def overflow():
        async with maker() as db:
            for n in range(129):
                db.add(receipt(now, run_id=f"unrelated-{n}",
                    candidate_json=json.dumps({"pending_order_id": f"other-{n}"})))
            await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=after_lease(maker, now, overflow))
    message = send.call_args.args[0]
    assert message.extra["signal_audits"][0]["execution_state"] == "unconfirmed"
    assert "原委托待撮合" not in message.content
    assert "尚未核实" in message.content


@pytest.mark.asyncio
async def test_final_read_failure_cannot_fall_back_to_old_card(setup, monkeypatch):
    maker, now, send = setup
    signal = await record(maker, now)
    original = points._refresh_execution_snapshot
    calls = 0
    async def fail_final(db, item, *, now):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("isolated final read failure")
        await original(db, item, now=now)
    monkeypatch.setattr(points, "_refresh_execution_snapshot", fail_final)
    with pytest.raises(RuntimeError, match="isolated final read failure"):
        await points.dispatch_buy_points(now=now, session_factory=maker)
    assert not send.called
    assert (await logs(maker))[-1].decision == "attempting"
    assert (await logs(maker))[0].candidate_json == signal.candidate_json
    monkeypatch.setattr(points, "_refresh_execution_snapshot", original)
    assert (await points.dispatch_buy_points(now=now+timedelta(seconds=31), session_factory=maker))["status"] == "sent"


@pytest.mark.asyncio
async def test_clock_expiry_during_final_execution_read_stops_transport(setup, monkeypatch):
    maker, now, send = setup
    await record(maker, now)
    from test_paper_buy_point_delivery_clock import advancing_clock
    clock = advancing_clock(monkeypatch)
    original = points._refresh_execution_snapshot
    calls = 0
    async def slow(db, item, *, now):
        nonlocal calls
        calls += 1
        await original(db, item, now=now)
        if calls == 2:
            clock["seconds"] = 181
    monkeypatch.setattr(points, "_refresh_execution_snapshot", slow)
    result = await points.dispatch_buy_points(now=now, session_factory=maker)
    assert result["count"] == 0 and not send.called
    assert (await logs(maker))[-1].decision == "expired"


@pytest.mark.asyncio
async def test_all_sessions_closed_before_transport_and_original_signal_unchanged(setup):
    maker, now, send = setup
    signal = await record(maker, now)
    active = 0
    class Tracked(AsyncSession):
        async def __aenter__(self):
            nonlocal active
            active += 1
            return await super().__aenter__()
        async def __aexit__(self, *args):
            nonlocal active
            try:
                return await super().__aexit__(*args)
            finally:
                active -= 1
    async def transport(*args, **kwargs):
        assert active == 0
        return {"channels": {"feishu": True}, "status": "sent"}
    send.side_effect = transport
    factory = async_sessionmaker(maker.kw["bind"], class_=Tracked, expire_on_commit=False)
    await points.dispatch_buy_points(now=now, session_factory=factory)
    assert active == 0 and send.call_count == 1
    saved = next(row for row in await logs(maker) if row.id == signal.id)
    assert saved.candidate_json == signal.candidate_json
    assert saved.price == 10.5 and saved.as_of_at == now


@pytest.mark.asyncio
async def test_final_repack_leaves_untransported_signal_retryable(setup, monkeypatch):
    maker, now, send = setup
    await record(maker, now)
    await record(maker, now, account="challenger_a")
    original = points._fit_batch
    calls = 0
    def shrink_final(items):
        nonlocal calls
        calls += 1
        fitted = original(items)
        return fitted[:1] if calls == 2 else fitted
    monkeypatch.setattr(points, "_fit_batch", shrink_final)
    first = await points.dispatch_buy_points(now=now, session_factory=maker)
    assert first["count"] == 1
    rows = await logs(maker)
    assert sum(row.decision == "attempting" for row in rows) == 2
    assert sum(row.decision == "sent" for row in rows) == 1
    second = await points.dispatch_buy_points(now=now+timedelta(seconds=31), session_factory=maker)
    assert second["count"] == 1
    ids = [call.args[0].extra["signal_ids"][0] for call in send.call_args_list]
    assert len(set(ids)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("account", points.ACCOUNT_NAMES)
async def test_every_original_account_reads_its_own_late_block(setup, account):
    maker, now, send = setup
    signal = await record(maker, now, account=account)
    async def blocked():
        async with maker() as db:
            db.add(receipt(now, account_id=signal.account_id, action="skip_buy",
                decision="blocked", candidate_json="{}", reason=f"本户限制:{account}"))
            await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=after_lease(maker, now, blocked))
    audit = send.call_args.args[0].extra["signal_audits"][0]
    assert audit["account"] == account and audit["execution_state"] == "blocked"
    assert f"本户限制:{account}" in send.call_args.args[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["filled", "partial"])
async def test_real_fill_committed_after_lease_requires_all_three_ledgers(setup, state):
    from app.models.paper import PaperTradeLog
    from app.models.trading import TradeOrder, TradeFill
    maker, now, send = setup
    signal = await record(maker, now)
    async with maker() as db:
        db.add(receipt(now))
        await db.commit()
    quantity = 200 if state == "filled" else 100
    async def filled():
        async with maker() as db:
            db.add(PaperTradeLog(id=123, account_id=signal.account_id, code=signal.code,
                trade_type="buy", price=10.4, amount=quantity, trade_time=now,
                strategy_version=signal.strategy_version, decision_round_id="round-current",
                fill_round_id="next-round"))
            db.add(TradeOrder(order_id="own-order", broker="paper", account_id="default",
                code=signal.code, source="test_route", side="buy", order_type="limit", price=10.5,
                quantity=200, filled_quantity=quantity, status=state,
                strategy_version=signal.strategy_version, trade_date=now.date(), created_at=now,
                decision_at=now, decision_round_id="round-current"))
            db.add(TradeFill(fill_id="isolated-fill", order_id="own-order", broker="paper",
                broker_trade_id="123", code=signal.code, side="buy", price=10.4, quantity=quantity,
                trade_date=now.date(), filled_at=now, decision_round_id="round-current",
                fill_round_id="next-round"))
            db.add(receipt(now, run_id="later-round", action="buy", decision="executed",
                executed_trade_id=123, reason="可核实实际账本",
                candidate_json=json.dumps({"deferred_order_observation": {
                    "order_response": {"order_id": "own-order", "status": state}}})))
            await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=after_lease(maker, now, filled))
    message = send.call_args.args[0]
    assert message.extra["signal_audits"][0]["execution_state"] == state
    assert "信号参考价 ¥10.50" in message.content
    assert "非成交价、非当前报价" in message.content
    async with maker() as db:
        assert (await db.get(PaperTradeLog, 123)).price == 10.4
