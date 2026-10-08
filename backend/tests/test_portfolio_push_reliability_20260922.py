"""Committed shared outbox -> original fake transport, isolated SQLite only."""
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import settings
from app.models.paper import (PaperAccount, PaperPortfolioSignal, PaperPortfolioDecision,
                              PaperAutoTradeLog, PaperTradeLog)
from app.models.trading import TradeOrder, TradeFill
from app.paper.portfolio_contract import make_signal_key, entry_version
from app.paper.portfolio_wallet import order_key
from app.push import paper_buy_points as points
from test_paper_buy_points import setup, logs, record
from test_portfolio_ingress_20260922 import memory_session, active as ingress_active, arguments
from test_portfolio_execution_20260922 import (real_execution, quote_execution_env,
    active, allocate, reconcile)
from app.paper import portfolio_ingress as ingress


@pytest_asyncio.fixture
async def shared(setup, monkeypatch):
    maker, now, send = setup
    async with maker.kw["bind"].begin() as conn:
        for model in (PaperPortfolioSignal, PaperPortfolioDecision, TradeOrder, TradeFill, PaperTradeLog):
            await conn.run_sync(model.__table__.create, checkfirst=True)
    async with maker() as db:
        db.add_all([PaperAccount(id=50, account_name="shared_50k", status="active", initial_capital=50000, current_capital=50000),
                    PaperAccount(id=1, account_name="default", status="active", initial_capital=50000, current_capital=50000)])
        await db.commit()
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_SIGNAL_TTL_SECONDS", 120)
    return maker, now, send


def signal(now, **changes):
    fields = dict(origin_account="default", origin_account_id=1, origin_version="original-v1",
                  portfolio_version="portfolio-v4", source="next_day_plan", code="600001",
                  name="测试共享", source_signal_id="source-1", shadow_event_key=None,
                  confirmed_at=now, observed_at=now, as_of_at=now, decision_round_id="round-1",
                  candidate_json='{"code":"600001"}', exit_policy_json="{}", created_at=now)
    policy = {"price": 10., "max_execution_delay_sec": 120, "notification": {
        "schema": "portfolio_buy_point_ingress_v1", "notification_allowed": True,
        "price_basis": "original_order_reference"}}
    fields["entry_policy_json"] = json.dumps(policy)
    fields.update(changes)
    fields["signal_key"] = make_signal_key(**{k: fields[k] for k in (
        "source", "origin_account", "origin_version", "decision_round_id", "source_signal_id",
        "shadow_event_key", "origin_account_id", "portfolio_version")})
    return PaperPortfolioSignal(**fields)


async def publish(maker, row):
    async with maker() as db:
        db.add(row)
        await db.commit()
    return row


@pytest.mark.asyncio
async def test_shared_durable_commit_window_dedup_original_queue(shared):
    maker, now, send = shared
    row = await publish(maker, signal(now))
    result = await points.reconcile_portfolio_buy_points(now=now, session_factory=maker)
    assert result["recovered"] == 1 and not send.called
    points._runtime.clear()
    assert (await points.reconcile_portfolio_buy_points(now=now, session_factory=maker))["recovered"] == 0
    await points.dispatch_buy_points(now=now, session_factory=maker)
    assert send.call_count == 1
    message = send.call_args.args[0]
    assert "组合实验 独立账本" in message.content and "原委托参考价" in message.content
    assert "非成交价" in message.content and "VWAP" not in message.content
    await points.dispatch_buy_points(now=now, session_factory=maker)
    assert send.call_count == 1
    rows = await logs(maker)
    assert len([r for r in rows if r.action == points.SIGNAL]) == 1
    assert all(r.account_id == 50 for r in rows)
    assert json.loads(rows[0].candidate_json)["signal_key"] == row.signal_key


@pytest.mark.asyncio
async def test_terminal_signal_bridge_commit_failure_recovers_without_transaction_changes(shared):
    maker, now, send = shared
    row = await publish(maker, signal(now))
    async with maker() as db:
        db.add(PaperPortfolioDecision(decision_key="terminal", signal_id=row.id,
            portfolio_version=row.portfolio_version, account_id=50, decision_round_id="later",
            as_of_at=now, observed_at=now, decision="budget_skipped", reason_code="no_cash",
            budget_json="{}"))
        await db.commit()
    class Broken(AsyncSession):
        async def commit(self):
            raise RuntimeError("injected independent commit failure")
    broken = async_sessionmaker(maker.kw["bind"], class_=Broken, expire_on_commit=False)
    assert (await points.reconcile_portfolio_buy_points(now=now, session_factory=broken))["failed"] == 1
    assert not await logs(maker)
    assert (await points.reconcile_portfolio_buy_points(now=now, session_factory=maker))["recovered"] == 1
    await points.dispatch_buy_points(now=now, session_factory=maker)
    assert "no_cash" in send.call_args.args[0].content
    async with maker() as db:
        assert len((await db.scalars(select(PaperPortfolioDecision))).all()) == 1
        assert (await db.get(PaperAccount, 50)).current_capital == 50000


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected", [
    ("disabled", "disabled"), ("legacy", "rejected"), ("missing_switch", "rejected"),
    ("bad_clock", "rejected"), ("future", "rejected"), ("expired", "expired"),
    ("crossday", "rejected"), ("bad_price", "rejected"),
])
async def test_fail_closed_contracts_never_send(shared, kind, expected):
    maker, now, send = shared
    changes = {}
    base = signal(now)
    policy = json.loads(base.entry_policy_json)
    if kind == "disabled": policy["notification"]["notification_allowed"] = False
    if kind == "legacy": policy.pop("notification")
    if kind == "missing_switch": policy["notification"].pop("notification_allowed")
    if kind == "bad_price": policy["price"] = float("nan")
    if kind == "bad_clock": changes["as_of_at"] = now+timedelta(seconds=1)
    if kind == "future": changes.update(confirmed_at=now+timedelta(seconds=1),
                                         observed_at=now+timedelta(seconds=1))
    if kind == "expired": changes.update(confirmed_at=now-timedelta(seconds=121),
                                          as_of_at=now-timedelta(seconds=121))
    if kind == "crossday": changes["confirmed_at"] = now-timedelta(days=1)
    changes["entry_policy_json"] = json.dumps(policy)
    await publish(maker, signal(now, **changes))
    assert (await points.reconcile_portfolio_buy_points(now=now, session_factory=maker))[expected] == 1
    await points.dispatch_buy_points(now=now, session_factory=maker)
    assert not send.called and all(r.action != points.SIGNAL for r in await logs(maker))


@pytest.mark.asyncio
async def test_duplicate_shared_wallet_fails_closed(shared):
    maker, now, send = shared
    await publish(maker, signal(now))
    async with maker() as db:
        db.add(PaperAccount(id=51, account_name="shared_50k", status="active",
                           initial_capital=50000, current_capital=50000))
        await db.commit()
    result = await points.reconcile_portfolio_buy_points(now=now, session_factory=maker)
    assert result["status"] == "wallet_ambiguous"
    assert not await logs(maker) and not send.called


@pytest.mark.asyncio
@pytest.mark.parametrize("hands_by_round", [(100,), (1, 1, 100)], ids=["real-full", "real-partial-full"])
async def test_real_allocator_broker_ledger_projects_notification(
    quote_execution_env, real_execution, monkeypatch, hands_by_round,
):
    from datetime import datetime
    clock = real_execution
    monkeypatch.setattr(settings, "PAPER_BUY_POINT_PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "PUSH_ENABLED", True)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr(points, "datetime", Clock)
    send = AsyncMock(return_value={"status": "sent", "channels": {"feishu": True}})
    monkeypatch.setattr(points.push_scheduler, "push_to_channels", send)
    async with quote_execution_env() as db:
        source, order = await allocate(db, clock)
        # Real allocation commits; bridge never borrows this trading session.
        result = await points.reconcile_portfolio_buy_points(now=clock[0], session_factory=quote_execution_env)
        assert result["recovered"] == 1
        for index, hands in enumerate(hands_by_round):
            outcome = await reconcile(db, clock, f"notify-real-fill-{index}", hands)
            expected = "filled" if index == len(hands_by_round)-1 else "partial"
            assert outcome and outcome[0]["event"] == expected
            async with quote_execution_env() as read_db:
                notification = (await read_db.scalars(select(PaperAutoTradeLog).where(
                    PaperAutoTradeLog.action == points.SIGNAL))).one()
                item = points._item(notification)
                await points._portfolio_execution(read_db, item, clock[0])
                assert item.get("execution_state") == expected, item
        fills = (await db.scalars(select(TradeFill).order_by(TradeFill.id))).all()
        trades = (await db.scalars(select(PaperTradeLog).order_by(PaperTradeLog.id))).all()
        assert {f.broker_trade_id for f in fills} == {str(t.id) for t in trades}
        assert all(t.signal_id != source.source_signal_id for t in trades)
        assert len({t.signal_id for t in trades}) == len(hands_by_round)
        for fill in fills:
            proof = json.loads(fill.raw_json)["pending_execution_timing"]["locked_risk"]
            assert proof["status"] == "validated"
            assert proof["result"]["checked_rules"] == 10
            assert proof["result"]["evaluation_status"] == "complete"
        await points.dispatch_buy_points(now=clock[0], session_factory=quote_execution_env)
        assert send.call_count == 1
        assert "已有模拟成交记录" in send.call_args.args[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["round", "missing_decision_round", "time", "broker_trade_id", "strategy",
                                       "quantity", "price", "wallet_ambiguous"])
async def test_real_fill_broken_binding_stays_unknown(
    quote_execution_env, real_execution, monkeypatch, corruption,
):
    clock = real_execution
    monkeypatch.setattr(settings, "PAPER_BUY_POINT_PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "PUSH_ENABLED", True)
    async with quote_execution_env() as db:
        source, order = await allocate(db, clock)
        assert (await points.reconcile_portfolio_buy_points(
            now=clock[0], session_factory=quote_execution_env))["recovered"] == 1
        assert (await reconcile(db, clock, "real-negative-full"))[0]["event"] == "filled"
        fill = (await db.scalars(select(TradeFill))).one()
        trade = (await db.scalars(select(PaperTradeLog))).one()
        if corruption == "round": fill.fill_round_id = "wrong-round"
        elif corruption == "missing_decision_round":
            order.decision_round_id = fill.decision_round_id = trade.decision_round_id = ""
        elif corruption == "time": trade.trade_time += timedelta(microseconds=1)
        elif corruption == "broker_trade_id": fill.broker_trade_id = "999999"
        elif corruption == "strategy": trade.strategy_version = "unrelated-version"
        elif corruption == "quantity": fill.quantity -= 100
        elif corruption == "price": fill.price += .01
        else:
            db.add(PaperAccount(account_name="shared_50k", status="active",
                               initial_capital=50000, current_capital=50000))
        await db.commit()
    async with quote_execution_env() as read_db:
        notification = (await read_db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.action == points.SIGNAL))).one()
        item = points._item(notification)
        # A reused owned view must not retain a former success on a broken join.
        item["execution_state"] = "filled"
        await points._portfolio_execution(read_db, item, clock[0])
        assert "execution_state" not in item and "待核对" in item["execution_note"]


@pytest.mark.asyncio
async def test_all_twelve_origins_bridge_without_expanding_roster(shared):
    from app.paper.account_policy import ACCOUNT_NAMES, ROUTE_ACCOUNT_NAMES
    maker, now, send = shared
    for index, account in enumerate(ACCOUNT_NAMES):
        account_id = 100+index
        async with maker() as db:
            if account != "default":
                db.add(PaperAccount(id=account_id, account_name=account, status="active", initial_capital=50000))
                await db.commit()
        route = next((r for r, a in ROUTE_ACCOUNT_NAMES.items() if a == account), None)
        await publish(maker, signal(now, origin_account=account,
            origin_account_id=1 if account == "default" else account_id,
            source=route or "next_day_plan", shadow_event_key=("event-"+account) if route else None,
            source_signal_id="signal-"+account))
    assert (await points.reconcile_portfolio_buy_points(now=now, session_factory=maker))["recovered"] == 12
    assert len(ACCOUNT_NAMES) == 12 and "shared_50k" not in ACCOUNT_NAMES
    assert not send.called


@pytest.mark.asyncio
async def test_original_queue_survives_bridge_timeout(setup, monkeypatch):
    import asyncio
    maker, now, send = setup
    await record(maker, now)
    monkeypatch.setattr(points, "reconcile_portfolio_buy_points", AsyncMock(side_effect=asyncio.TimeoutError))
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))["status"] == "sent"
    assert send.call_count == 1


@pytest.mark.asyncio
async def test_portfolio_ttl_caps_long_source_ttl(shared):
    maker, now, send = shared
    row = signal(now, confirmed_at=now-timedelta(seconds=121), as_of_at=now-timedelta(seconds=121))
    policy = json.loads(row.entry_policy_json)
    policy["max_execution_delay_sec"] = 600
    row.entry_policy_json = json.dumps(policy)
    await publish(maker, row)
    assert (await points.reconcile_portfolio_buy_points(now=now, session_factory=maker))["expired"] == 1
    assert not send.called


@pytest.mark.asyncio
async def test_uncommitted_or_rolled_back_source_not_consumed(shared):
    maker, now, send = shared
    async with maker() as owner:
        owner.add(signal(now))
        await owner.flush()
        assert (await points.reconcile_portfolio_buy_points(now=now, session_factory=maker))["recovered"] == 0
        await owner.rollback()
    assert not await logs(maker) and not send.called


@pytest.mark.asyncio
async def test_transport_failure_original_retry_and_expiration(shared):
    maker, now, send = shared
    await publish(maker, signal(now))
    send.return_value = {"status": "failed", "channels": {"feishu": False}}
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))["status"] == "failed"
    assert (await points.dispatch_buy_points(now=now+timedelta(seconds=5), session_factory=maker))["count"] == 0
    send.return_value = {"status": "sent", "channels": {"feishu": True}}
    assert (await points.dispatch_buy_points(now=now+timedelta(seconds=16), session_factory=maker))["status"] == "sent"
    assert send.call_count == 2


@pytest.mark.asyncio
async def test_original_scope_kept_and_shared_cooldown_uses_same_budget(shared, monkeypatch):
    maker, now, send = shared
    await record(maker, now)
    await publish(maker, signal(now))
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))["count"] == 2
    await publish(maker, signal(now, source_signal_id="source-2", decision_round_id="round-2"))
    await points.dispatch_buy_points(now=now+timedelta(seconds=1), session_factory=maker)
    assert send.call_count == 1
    assert any(r.decision == "throttled" for r in await logs(maker))


@pytest.mark.asyncio
async def test_notification_switch_duplicate_preserves_envelope(memory_session, ingress_active, monkeypatch):
    monkeypatch.setattr(settings, "PAPER_BUY_POINT_PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "PUSH_ENABLED", False)
    row = await ingress.capture_confirmed_signal(memory_session, **arguments())
    await memory_session.commit()
    original = row.entry_policy_json
    monkeypatch.setattr(settings, "PUSH_ENABLED", True)
    again = await ingress.capture_confirmed_signal(memory_session, **arguments())
    assert again.id == row.id and again.entry_policy_json == original
    assert json.loads(original)["notification"]["notification_allowed"] is False
    with pytest.raises(ValueError, match="conflicts"):
        await ingress.capture_confirmed_signal(memory_session, **arguments(price=10.01))


async def order_for(maker, row, now, status="pending", wrong=False):
    version = entry_version(row.origin_version, policy_version=row.portfolio_version)
    origin = {k: getattr(row, k) for k in ("origin_account", "origin_account_id", "origin_version", "portfolio_version")}
    origin.update(schema="paper_portfolio_origin_v1", portfolio_signal_key=row.signal_key, entry_version=version)
    order = TradeOrder(order_id="order-1", account_id="default" if wrong else "shared_50k",
        broker="paper", code=row.code, side="buy", order_type="limit", price=10, quantity=100, filled_quantity=0,
        status=status, strategy_version=version, signal_id=row.source_signal_id, source=row.source,
        idempotency_key=order_key(row.signal_key), risk_json=json.dumps({"paper_portfolio_origin": origin}),
        trade_date=now.date(), created_at=now, decision_round_id="allocation-round")
    async with maker() as db:
        db.add(order)
        await db.commit()
    return order


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong,status,label", [(False,"pending","等待下一轮撮合"),
                                                (True,"filled","状态待核对"),
                                                (False,"filled","状态待核对")])
async def test_execution_exact_order_no_fake_fill(shared, wrong, status, label):
    maker, now, send = shared
    row = await publish(maker, signal(now))
    await order_for(maker, row, now, status, wrong)
    await points.dispatch_buy_points(now=now, session_factory=maker)
    assert label in send.call_args.args[0].content
    assert "✅ 已有模拟成交记录" not in send.call_args.args[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_account,status", [(False, "filled"), (True, "filled"), (False, "partial")])
async def test_shared_fill_requires_exact_ledger(shared, bad_account, status):
    maker, now, send = shared
    row = await publish(maker, signal(now))
    order = await order_for(maker, row, now, status)
    async with maker() as db:
        stored = await db.get(TradeOrder, order.id)
        stored.filled_quantity = 100
        if status == "partial":
            stored.quantity = 200
        trade = PaperTradeLog(account_id=1 if bad_account else 50, code=row.code,
            trade_type="buy", price=10., amount=100, trade_time=now,
            strategy_version=order.strategy_version, signal_id="slice-request-not-source",
            decision_round_id="allocation-round", fill_round_id="later-fill-round")
        db.add(trade)
        await db.flush()
        db.add(TradeFill(fill_id="fill-1", order_id=order.order_id, broker="paper",
            code=row.code, side="buy", price=10., quantity=100,
            broker_trade_id=str(trade.id), filled_at=now, trade_date=now.date(),
            decision_round_id="allocation-round", fill_round_id="later-fill-round"))
        await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=maker)
    text = send.call_args.args[0].content
    if bad_account:
        assert "状态待核对" in text and "已有模拟成交记录" not in text
    else:
        assert "100 股" in text
        assert ("已有部分模拟成交记录" if status == "partial" else "已有模拟成交记录") in text


@pytest.mark.asyncio
async def test_source_ttl_not_extended_by_bridge_or_transport_retry(shared):
    maker, now, send = shared
    await publish(maker, signal(now, confirmed_at=now-timedelta(seconds=119),
                                     as_of_at=now-timedelta(seconds=119)))
    send.side_effect = TimeoutError("fake channel")
    await points.dispatch_buy_points(now=now, session_factory=maker)
    await points.dispatch_buy_points(now=now+timedelta(seconds=16), session_factory=maker)
    assert send.call_count == 1
    assert any(r.decision == "expired" for r in await logs(maker))


@pytest.mark.asyncio
async def test_bridge_bounded_cleanup_does_not_starve_fresh(shared):
    maker, now, send = shared
    for index in range(3):
        await publish(maker, signal(now-timedelta(days=1), source_signal_id=f"old-{index}"))
    await publish(maker, signal(now, source_signal_id="fresh"))
    result = await points.reconcile_portfolio_buy_points(now=now, session_factory=maker, limit=1)
    assert result["recovered"] == 1 and result["expired"] == 1
    assert len(await logs(maker)) == 2 and not send.called


@pytest.mark.asyncio
async def test_cancelled_commit_leaves_durable_source_retryable(shared):
    import asyncio
    maker, now, send = shared
    await publish(maker, signal(now))
    class Cancelled(AsyncSession):
        async def commit(self):
            raise asyncio.CancelledError()
    broken = async_sessionmaker(maker.kw["bind"], class_=Cancelled, expire_on_commit=False)
    with pytest.raises(asyncio.CancelledError):
        await points.reconcile_portfolio_buy_points(now=now, session_factory=broken)
    assert not await logs(maker)
    assert (await points.reconcile_portfolio_buy_points(now=now, session_factory=maker))["recovered"] == 1


@pytest.mark.asyncio
async def test_stock_tag_gate_preserved(shared):
    from app.models.stock import StockTag
    maker, now, send = shared
    await publish(maker, signal(now))
    async with maker() as db:
        tag = await db.scalar(select(StockTag))
        tag.is_st = True
        await db.commit()
    assert (await points.reconcile_portfolio_buy_points(now=now, session_factory=maker))["rejected"] == 1
    await points.dispatch_buy_points(now=now, session_factory=maker)
    assert not send.called
