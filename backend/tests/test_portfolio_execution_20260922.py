"""Real shared-wallet execution in temporary SQLite; no risk/route/broker bypass.

Accepted quote frames and seeded calendar/identity/sentiment are explicit test
inputs, not evidence of production availability. Sector-name resolution alone is
stubbed: enrichment is not an authorization, budget, risk, depth or ledger gate.
"""
import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func, event, update, text as sql_text

from app.api.v1 import paper
from app.config.settings import settings
from app.core.trade_calendar import TradeCalendar
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog, PaperPortfolioDecision
from app.models.stock import StockTag, MarketSentiment
from app.models.trading import TradeOrder, TradeFill
from app.paper import portfolio, portfolio_provenance, portfolio_wallet
from app.paper.account_policy import ACCOUNT_NAMES
from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT
from app.risk.engine import risk_engine
from app.risk.rules import register_all_rules
from app.trading import service
from paper_pending_fixture import accepted_frame
from test_quote_round_execution import quote_execution_env, _round_payload
from test_portfolio_provenance_20260922 import captured, AT, active

def completed_receipts(round_id):
    """Explicit same-round fixture inputs, not natural scheduler scan evidence."""
    return {name: {"status": "completed", "quote_round_id": round_id,
                   "source_scan_status": "completed"} for name in ACCOUNT_NAMES}


@pytest.fixture
def real_execution(monkeypatch, active):
    clock = [AT + timedelta(seconds=3)]
    monkeypatch.setattr(paper, "_TRADE_LOCK", asyncio.Lock())
    monkeypatch.setattr(settings, "PAPER_AUTO_TRADE_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_INTRADAY_AUTO_TRADE_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", False)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: clock[0])
    original_now = paper._paper_now
    monkeypatch.setattr(paper, "_paper_now", lambda:
        original_now() if paper._PAPER_FILL_CONTEXT.get() or paper._QUOTE_ROUND_CONTEXT.get() else clock[0])
    monkeypatch.setattr(paper.trade_calendar, "_cache", {
        (AT + timedelta(days=i)).date(): (AT + timedelta(days=i)).weekday() < 5
        for i in range(-40, 4)})
    async def loaded(self, year):
        assert self is paper.trade_calendar and year == 2026
    monkeypatch.setattr(TradeCalendar, "_ensure_loaded", loaded)
    network = AsyncMock(side_effect=AssertionError("calendar network forbidden"))
    monkeypatch.setattr(TradeCalendar, "_sync_from_source", network)
    # Only non-trading sector-label enrichment is isolated, never its risk rules.
    monkeypatch.setattr(paper, "_resolve_candidate_entry_sector", AsyncMock(return_value={}))
    # ORM defaults use host wall time independently of paper's decision clock.
    # Freeze creation clocks at insertion, never rewrite a persisted order.
    def order_clock(_mapper, _connection, target):
        assert target.created_at is None
        target.created_at = clock[0]
    event.listen(TradeOrder, "before_insert", order_clock)
    old_rules = risk_engine._rules
    risk_engine._rules = []
    register_all_rules()
    try:
        yield clock
    finally:
        risk_engine._rules = old_rules
        event.remove(TradeOrder, "before_insert", order_clock)
        network.assert_not_awaited()


def frame(at, key, hands=100, **changes):
    record = dict(code="600001", name="fixture", price=10., prev_close=9.75,
                  open=9.8, low=9.78, high=10.02, avg_price=9.95,
                  change_pct=(10/9.75-1)*100, ask1_price=10., ask1_volume=hands,
                  bid1_price=9.99, bid1_volume=100, volume_ratio=2.,
                  orderbook_imbalance=.2, volume=100000, amount=100000000,
                  limit_up=10.73, limit_down=8.78)
    record.update(changes)
    return _round_payload(key, at, [record])


async def accept(db, payload):
    await accepted_frame(db, payload)
    sentiment = await db.scalar(select(MarketSentiment).where(
        MarketSentiment.trade_date == payload["trade_date"]))
    if sentiment is None:
        sentiment = MarketSentiment(trade_date=payload["trade_date"],
            sentiment_cycle="recovery", sentiment_score=60,
            limit_up_count=40, limit_down_count=5, broken_limit_count=8,
            seal_rate=70, board_height=3, advance_decline_ratio=1.2,
            turnover_total=1.2, main_net_inflow=20, quality_status="ok",
            calculation_version="isolated-real-ten-rule")
        db.add(sentiment)
    sentiment.observed_at = payload["as_of_at"]
    await db.commit()


async def allocate(db, clock, origin_account="challenger_c"):
    db.add(StockTag(code="600001", name="fixture", board_type="main_sh",
                   board_tag="tradeable", is_st=False, is_suspended=False,
                   is_delisting=False, is_ipo_recent=False))
    await db.commit()
    signal, _, _ = await captured(db, origin_account)
    payload = frame(clock[0], "allocation-round")
    await accept(db, payload)
    token = paper._QUOTE_ROUND_CONTEXT.set(payload)
    try:
        result = await portfolio.run_shared_portfolio(
            db, source_receipts=completed_receipts(payload["round_id"]), manage_positions=False)
        decisions = (await db.scalars(select(PaperPortfolioDecision))).all()
        assert result["allocated"] == 1, (result, [(d.decision, d.reason) for d in decisions])
        order = (await db.scalars(select(TradeOrder))).one()
        assert order.status == "submitted", order.error_message
        assert await db.scalar(select(func.count(TradeFill.id))) == 0
        again = await portfolio.run_shared_portfolio(db, source_receipts=completed_receipts(payload["round_id"]), manage_positions=False)
        assert again["allocated"] == 0
        assert await db.scalar(select(func.count(TradeOrder.id))) == 1
        same = await service.reconcile_paper_deferred_orders(db, account_id=PORTFOLIO_ACCOUNT,
            round_id=payload["round_id"], now=clock[0])
        assert same == []
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)
    return signal, order


async def reconcile(db, clock, key, hands=100, **changes):
    clock[0] += timedelta(seconds=30)
    payload = frame(clock[0], key, hands, **changes)
    await accept(db, payload)
    token = paper._QUOTE_ROUND_CONTEXT.set(payload)
    try:
        results = await service.reconcile_paper_deferred_orders(
            db, account_id=PORTFOLIO_ACCOUNT, round_id=key, now=clock[0])
        replay = await service.reconcile_paper_deferred_orders(
            db, account_id=PORTFOLIO_ACCOUNT, round_id=key, now=clock[0])
        assert replay == []
        return results
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("fill_price", [10., 9.98], ids=["exact-cap-fee-boundary", "price-improved-depth"])
async def test_real_c2_three_round_partial_fill_cash_receipts_stop_and_idempotency(
    quote_execution_env, real_execution, fill_price,
):
    clock = real_execution
    async with quote_execution_env() as db:
        signal, order = await allocate(db, clock)
        quantity = order.quantity
        # Conservative per-100-share fee reservation leaves the original 20%
        # cap intact after all actual slices; this is NOT extra cash or a wider cap.
        assert quantity == 900
        for number, hands in enumerate((1, 1, 100), 1):
            result = await reconcile(db, clock, f"fill-{number}", hands,
                                     price=fill_price, ask1_price=fill_price, bid1_price=fill_price-.01)
            assert result and result[0]["event"] == ("filled" if number == 3 else "partial"), (number, quantity, [(r["event"], r.get("reason"), r.get("order", {}).get("error_message"), r.get("risk", {}).get("paper_portfolio_budget")) for r in result])
            position = (await db.scalars(select(PaperPosition))).one()
            assert position.stop_loss_price == 9.5
            assert position.buy_amount == (quantity if number == 3 else 100*number)
            assert position.strategy_version == order.strategy_version
        fills = (await db.scalars(select(TradeFill).order_by(TradeFill.id))).all()
        buys = (await db.scalars(select(PaperTradeLog).order_by(PaperTradeLog.id))).all()
        assert len(fills) == len(buys) == 3
        assert sum(f.quantity for f in fills) == quantity
        assert {f.broker_trade_id for f in fills} == {str(t.id) for t in buys}
        assert len({t.signal_id for t in buys}) == 3
        assert all(f.decision_round_id == "allocation-round" for f in fills)
        assert [f.fill_round_id for f in fills] == ["fill-1", "fill-2", "fill-3"]
        for fill in fills:
            proof = json.loads(fill.raw_json)["pending_execution_timing"]["locked_risk"]
            assert proof["status"] == "validated"
            assert proof["result"]["checked_rules"] == 10
            assert proof["result"]["evaluation_status"] == "complete"
            assert proof["account_name"] == PORTFOLIO_ACCOUNT
        wallet = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == PORTFOLIO_ACCOUNT))
        assert wallet.current_capital == pytest.approx(
            50000 - sum(f.price*f.quantity+f.commission+f.tax for f in fills))
        assert sum(f.commission for f in fills) == 15
        origin = await portfolio_provenance.position_origin(db, position=position, as_of=clock[0])
        assert origin["origin_account"] == "challenger_c"
        layers, _ = await portfolio_wallet.shared_buy_layers(
            db, account_id=wallet.id, code="600001", now=clock[0], origin=origin)
        assert layers == 1
        assert origin["portfolio_signal_key"] == signal.signal_key


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["st", "sentiment", "missing_quote", "route_invalid", "version"])
async def test_pending_real_negative_gates_do_not_mutate_shared_ledger(
    quote_execution_env, real_execution, monkeypatch, kind,
):
    clock = real_execution
    async with quote_execution_env() as db:
        _, order = await allocate(db, clock)
        if kind == "st":
            tag = await db.get(StockTag, "600001")
            tag.is_st = True
            await db.commit()
        elif kind == "version":
            # Real source policy rotation, not a mocked provenance decision.
            monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_SHADOW_VERSION",
                                settings.PAPER_STRATEGY_ITERATION_SHADOW_VERSION + "_rotated")
        changes = {}
        if kind == "missing_quote":
            changes["source_quote_at"] = clock[0] - timedelta(minutes=5)
        elif kind == "route_invalid":
            changes["orderbook_imbalance"] = -1.
        if kind == "sentiment":
            state = await db.scalar(select(MarketSentiment))
            state.sentiment_cycle, state.sentiment_score = "freezing", 0
            await db.commit()
        results = await reconcile(db, clock, "negative-fill", **changes)
        assert not any(r.get("fills") for r in results), results
        assert await db.scalar(select(func.count(PaperTradeLog.id))) == 0
        assert await db.scalar(select(func.count(TradeFill.id))) == 0
        assert await db.scalar(select(func.count(PaperPosition.id))) == 0
        wallet = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == PORTFOLIO_ACCOUNT))
        assert wallet.current_capital == 50000
        await db.refresh(order)
        assert order.status in ("canceled", "risk_blocked", "submitted")
        if kind in ("st", "sentiment"):
            assert order.status == "risk_blocked", order.error_message
        if kind == "route_invalid":
            assert order.status == "canceled" and "盘口失衡" in order.error_message
        if kind == "missing_quote":
            assert order.status == "submitted"


@pytest.mark.asyncio
async def test_real_fill_receipt_insert_failure_rolls_back_wallet_and_inventory(
    quote_execution_env, real_execution,
):
    clock = real_execution
    async with quote_execution_env() as db:
        await allocate(db, clock)
        def fail_receipt(_mapper, _connection, _target):
            raise RuntimeError("isolated receipt persistence failure")
        event.listen(TradeFill, "before_insert", fail_receipt)
        try:
            # Only the persistence boundary fails; all route/risk/broker/book
            # logic before it runs unchanged in the real fill transaction.
            with pytest.raises(RuntimeError, match="receipt persistence"):
                await reconcile(db, clock, "rollback-fill")
        finally:
            event.remove(TradeFill, "before_insert", fail_receipt)
        await db.rollback()
    async with quote_execution_env() as verify:
        assert await verify.scalar(select(func.count(PaperTradeLog.id))) == 0
        assert await verify.scalar(select(func.count(TradeFill.id))) == 0
        assert await verify.scalar(select(func.count(PaperPosition.id))) == 0
        wallet = await verify.scalar(select(PaperAccount).where(PaperAccount.account_name == PORTFOLIO_ACCOUNT))
        assert wallet.current_capital == 50000
        order = (await verify.scalars(select(TradeOrder))).one()
        assert order.filled_quantity == 0


@pytest.mark.asyncio
async def test_real_t_plus_one_and_frozen_c2_exit_survive_portfolio_disabled(
    quote_execution_env, real_execution, monkeypatch,
):
    from app.paper.position_policy import position_exit_policy
    clock = real_execution
    async with quote_execution_env() as db:
        signal, buy_order = await allocate(db, clock)
        result = await reconcile(db, clock, "whole-buy")
        assert result[0]["event"] == "filled", result
        position = (await db.scalars(select(PaperPosition))).one()
        entry_version = position.strategy_version
        wallet = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == PORTFOLIO_ACCOUNT))
        frozen, trace = await position_exit_policy(db, account_name=PORTFOLIO_ACCOUNT,
            position=position, defaults=paper._strategy_sell_params(wallet), as_of=clock[0])
        assert trace["basis"] == "frozen_entry_order"
        assert trace["origin_account"] == "challenger_c"
        assert frozen == json.loads(signal.entry_policy_json)["exit_parameters"]
        # Stop-trigger quote on T is real, but cannot sell today's inventory.
        clock[0] += timedelta(seconds=30)
        payload = frame(clock[0], "same-day-stop", price=9.4, low=9.4, bid1_price=9.4,
                        ask1_price=9.41, prev_close=9.75)
        await accept(db, payload)
        token = paper._QUOTE_ROUND_CONTEXT.set(payload)
        try:
            logs = await paper._run_auto_sells(db, account=wallet, run_id="test-t1-block",
                trade_date=clock[0].date(), trigger="isolated-t1", execute=True, quote_now=clock[0])
            await db.commit()
            assert not (await db.scalars(select(TradeOrder).where(TradeOrder.side == "sell"))).all()
            assert logs and any("T+1" in (row.reason or "") for row in logs)
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        # Lifecycle off and current origin-version rotation must not remove the
        # first real BUY's frozen protective exit or enable a new entry.
        monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ENABLED", False)
        monkeypatch.setattr(settings, "PAPER_STRATEGY_ITERATION_SHADOW_VERSION",
                            settings.PAPER_STRATEGY_ITERATION_SHADOW_VERSION + "_new")
        clock[0] = (AT + timedelta(days=1)).replace(hour=10, minute=0, second=0)
        payload = frame(clock[0], "next-day-stop", price=9.4, low=9.4,
                        bid1_price=9.4, ask1_price=9.41, prev_close=10., avg_price=9.5)
        await accept(db, payload)
        token = paper._QUOTE_ROUND_CONTEXT.set(payload)
        try:
            logs = await paper._run_auto_sells(db, account=wallet, run_id="test-next-day",
                trade_date=clock[0].date(), trigger="isolated-exit", execute=True, quote_now=clock[0])
            await db.commit()
            sell = await db.scalar(select(TradeOrder).where(TradeOrder.side == "sell"))
            assert sell is not None, [(l.action, l.reason) for l in logs]
            assert sell.status == "submitted", sell.error_message
            assert sell.strategy_version == entry_version
            contract = json.loads(sell.risk_json)["paper_deferred_order"]["candidate"]
            assert contract["exit_parameters"] == frozen
            assert contract["exit_policy"]["origin_account"] == "challenger_c"
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        result = await reconcile(db, clock, "next-day-fill", price=9.4, low=9.4,
                                 bid1_price=9.4, ask1_price=9.41, prev_close=10., avg_price=9.5)
        assert result[0]["event"] == "filled", result
        await db.refresh(position)
        assert position.is_closed
        fills = (await db.scalars(select(TradeFill))).all()
        assert len(fills) == 2 and {f.side for f in fills} == {"buy", "sell"}
        await db.refresh(wallet)
        expected = 50000 + sum(
            (f.price*f.quantity if f.side == "sell" else -f.price*f.quantity)
            -f.commission-f.tax for f in fills)
        assert wallet.current_capital == pytest.approx(expected)
        assert buy_order.strategy_version == sell.strategy_version == entry_version


@pytest.mark.asyncio
async def test_held_stop_raised_while_waiting_for_fill_lock_blocks_old_remainder(
    quote_execution_env, real_execution, monkeypatch,
):
    clock = real_execution
    async with quote_execution_env() as db:
        _, order = await allocate(db, clock)
        first = await reconcile(db, clock, "initial-partial", hands=1,
                                price=9.98, ask1_price=9.98, bid1_price=9.97)
        assert first[0]["event"] == "partial"
        held = (await db.scalars(select(PaperPosition))).one()
        wallet = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == PORTFOLIO_ACCOUNT))
        held_id, cash, amount = held.id, wallet.current_capital, held.buy_amount
        assert held.stop_loss_price == 9.5

        class RaiseProtectionBeforeLock:
            """A committed competing risk update, not a patched budget result."""
            def __init__(self):
                self.lock = asyncio.Lock()
                self.called = False
            async def __aenter__(self):
                if not self.called:
                    self.called = True
                    async with quote_execution_env() as writer:
                        await writer.execute(update(PaperPosition).where(
                            PaperPosition.id == held_id).values(stop_loss_price=9.7))
                        await writer.commit()
                return await self.lock.__aenter__()
            async def __aexit__(self, *args):
                return await self.lock.__aexit__(*args)

        gate = RaiseProtectionBeforeLock()
        monkeypatch.setattr(paper, "_TRADE_LOCK", gate)
        results = await reconcile(db, clock, "stale-stop-remainder", hands=1,
                                  price=9.98, ask1_price=9.98, bid1_price=9.97)
        assert gate.called, "must reach lock after real preflight succeeded"
        assert results[0]["event"] == "risk_blocked", results
        assert not any(r.get("fills") for r in results)
        await db.refresh(order)
        proof = json.loads(order.risk_json)["paper_locked_risk"]
        assert proof["status"] == "blocked"
        assert "protection" in order.error_message
        await db.refresh(held)
        await db.refresh(wallet)
        assert held.stop_loss_price == 9.7  # never restored from old pending 9.5
        assert held.buy_amount == amount == 100
        assert wallet.current_capital == cash
        assert await db.scalar(select(func.count(PaperTradeLog.id))) == 1
        assert await db.scalar(select(func.count(TradeFill.id))) == 1
        pending = json.loads(order.risk_json)["paper_deferred_order"]
        assert pending["stop_loss_price"] == 9.5  # immutable old reservation stays old


@pytest.mark.asyncio
async def test_b2_origin_uses_same_shared_wallet_but_its_own_real_route_contract(
    quote_execution_env, real_execution,
):
    clock = real_execution
    async with quote_execution_env() as db:
        signal, order = await allocate(db, clock, "challenger_b")
        results = await reconcile(db, clock, "b2-real-fill")
        assert results[0]["event"] == "filled", results
        position = (await db.scalars(select(PaperPosition))).one()
        origin = await portfolio_provenance.position_origin(db, position=position, as_of=clock[0])
        assert origin["origin_account"] == "challenger_b"
        assert origin["source"] == "b_weak_open_second_board"
        assert origin["portfolio_signal_key"] == signal.signal_key
        assert order.account_id == PORTFOLIO_ACCOUNT
        assert json.loads(order.risk_json)["paper_deferred_order"]["buy_validity"]["route_id"] == origin["source"]
        fill = (await db.scalars(select(TradeFill))).one()
        proof = json.loads(fill.raw_json)["pending_execution_timing"]["locked_risk"]
        assert proof["result"]["checked_rules"] == 10 and proof["status"] == "validated"


async def direct_c2_command(db, clock):
    """Real accepted frames and a legitimate immutable C2 key, no order yet."""
    db.add(StockTag(code="600001", name="fixture", board_type="main_sh",
                   board_tag="tradeable", is_st=False, is_suspended=False,
                   is_delisting=False, is_ipo_recent=False))
    await db.commit()
    signal, _, _ = await captured(db, "challenger_c")
    await portfolio_wallet.get_portfolio_account(db)
    await accept(db, frame(AT + timedelta(seconds=3), "persisted-old-round"))
    clock[0] = AT + timedelta(seconds=40)
    current = frame(AT + timedelta(seconds=30), "accepted-Q1")
    await accept(db, current)
    cmd = portfolio._command(signal, context=current, now=AT + timedelta(seconds=31))
    assert cmd.quantity == 100
    return signal, cmd, current


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["old_round", "backdated", "both"])
async def test_real_c2_key_cannot_forge_reservation_round_or_clock(
    quote_execution_env, real_execution, mutation,
):
    clock = real_execution
    async with quote_execution_env() as db:
        _, cmd, current = await direct_c2_command(db, clock)
        if mutation in ("old_round", "both"):
            # Even a genuinely persisted healthy round cannot impersonate Q1.
            cmd.decision_round_id = "persisted-old-round"
        if mutation in ("backdated", "both"):
            cmd.decision_at = AT + timedelta(seconds=3)
        token = paper._QUOTE_ROUND_CONTEXT.set(current)
        try:
            with pytest.raises(HTTPException) as exc:
                await service.submit_order(db, cmd)
            assert exc.value.status_code == 409
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        assert await db.scalar(select(func.count(TradeOrder.id))) == 0
        assert await db.scalar(select(func.count(TradeFill.id))) == 0
        assert await db.scalar(select(func.count(PaperPosition.id))) == 0
        assert await db.scalar(select(func.count(PaperTradeLog.id))) == 0
        wallet = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == PORTFOLIO_ACCOUNT))
        assert wallet.current_capital == 50000


@pytest.mark.asyncio
async def test_real_c2_reservation_acceptance_clock_q1_block_q2_fill_and_replay(
    quote_execution_env, real_execution,
):
    clock = real_execution
    async with quote_execution_env() as db:
        signal, cmd, current = await direct_c2_command(db, clock)
        requested_at = cmd.decision_at
        accepted_at = clock[0]
        token = paper._QUOTE_ROUND_CONTEXT.set(current)
        try:
            result = await service.submit_order(db, cmd)
            assert result["order"]["status"] == "submitted", result
            order = (await db.scalars(select(TradeOrder))).one()
            assert order.decision_at == accepted_at > requested_at
            metadata = json.loads(order.risk_json)["paper_deferred_order"]
            receipt = metadata["portfolio_reservation_clock"]
            assert receipt["requested_at"] == requested_at.isoformat()
            assert receipt["accepted_at"] == accepted_at.isoformat()
            # The immutable event's confirmation time/TTL is never renewed.
            assert metadata["confirmed_at"] == signal.confirmed_at.isoformat()
            assert metadata["buy_validity"]["confirmed_at"] == AT.isoformat()
            assert order.decision_round_id == current["round_id"]
            assert await service.reconcile_paper_deferred_orders(
                db, account_id=PORTFOLIO_ACCOUNT, round_id=current["round_id"], now=clock[0]) == []
            assert await db.scalar(select(func.count(TradeFill.id))) == 0
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        results = await reconcile(db, clock, "accepted-Q2")
        assert results[0]["event"] == "filled", results
        fill = (await db.scalars(select(TradeFill))).one()
        assert fill.decision_round_id == "accepted-Q1" and fill.fill_round_id == "accepted-Q2"
        proof = json.loads(fill.raw_json)["pending_execution_timing"]["locked_risk"]
        assert proof["result"]["checked_rules"] == 10 and proof["status"] == "validated"
        # Idempotent retrieval is before validation of a new reservation; no
        # current context is necessary to inspect an already accepted command.
        retry = await service.submit_order(db, cmd)
        assert retry["idempotent_replay"] is True
        assert retry["order"]["order_id"] == order.order_id
        await db.refresh(order)
        assert order.decision_at == accepted_at
        assert await db.scalar(select(func.count(TradeOrder.id))) == 1
        assert await db.scalar(select(func.count(TradeFill.id))) == 1


@pytest.mark.asyncio
async def test_real_q2_committed_before_service_acceptance_cannot_backfill(
    quote_execution_env, real_execution,
):
    clock = real_execution
    async with quote_execution_env() as db:
        _, cmd, q1 = await direct_c2_command(db, clock)
        requested = cmd.decision_at  # 13:30:31
        q2 = frame(AT + timedelta(seconds=35), "q2-already-visible")
        await accept(db, q2)  # real persisted Q2 exists before acceptance at :40
        token = paper._QUOTE_ROUND_CONTEXT.set(q1)
        try:
            result = await service.submit_order(db, cmd)
            assert result["order"]["status"] == "submitted", result
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        order = (await db.scalars(select(TradeOrder))).one()
        assert requested < q2["committed_at"] < order.decision_at == clock[0]
        token = paper._QUOTE_ROUND_CONTEXT.set(q2)
        try:
            result = await service.reconcile_paper_deferred_orders(
                db, account_id=PORTFOLIO_ACCOUNT, round_id=q2["round_id"], now=clock[0])
            assert result[0]["event"] == "waiting", result
            assert "行情提交不晚于原决策" in result[0]["reason"]
            assert not result[0]["fills"]
            assert await db.scalar(select(func.count(TradeFill.id))) == 0
            assert await db.scalar(select(func.count(PaperTradeLog.id))) == 0
            assert await db.scalar(select(func.count(PaperPosition.id))) == 0
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        # The wait does not destroy the original healthy confirmation. Only a
        # genuinely later Q3 can now fill, using the same immutable order.
        result = await reconcile(db, clock, "q3-after-service-acceptance")
        assert result[0]["event"] == "filled", result
        fill = (await db.scalars(select(TradeFill))).one()
        assert fill.order_id == order.order_id
        assert fill.fill_round_id == "q3-after-service-acceptance"
        assert fill.decision_round_id == "accepted-Q1"
        proof = json.loads(fill.raw_json)["pending_execution_timing"]["locked_risk"]
        assert proof["result"]["checked_rules"] == 10 and proof["status"] == "validated"


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_sla", [False, True], ids=["writer-wait-accept-clock", "writer-wait-cross-120s"])
async def test_real_sqlite_writer_wait_resamples_clock_and_keeps_original_sla(
    quote_execution_env, real_execution, cross_sla,
):
    clock = real_execution
    async with quote_execution_env() as db:
        signal, cmd, current = await direct_c2_command(db, clock)
        if cross_sla:
            # Quote remains fresh throughout; only the original signal's 120s
            # new-allocation SLA expires while the writer is held.
            clock[0] = AT + timedelta(seconds=115)
            current = frame(AT + timedelta(seconds=110), "fresh-before-sla")
            await accept(db, current)
            cmd = portfolio._command(signal, context=current, now=AT+timedelta(seconds=111))
        requested = cmd.decision_at
        before_wait = clock[0]
        await db.commit()  # no competing reader snapshot retained by test setup
        attempted = asyncio.Event()
        engine = db.get_bind()

        def saw_write_attempt(_conn, _cursor, statement, _parameters, _context, _many):
            if statement.strip().upper() == "BEGIN IMMEDIATE":
                attempted.set()

        async with quote_execution_env() as blocker:
            await blocker.execute(sql_text("BEGIN IMMEDIATE"))
            event.listen(engine, "before_cursor_execute", saw_write_attempt)
            token = paper._QUOTE_ROUND_CONTEXT.set(current)
            task = None
            try:
                # This task owns its real AsyncSession reservation, never a
                # copied owner token or patched lock implementation.
                task = asyncio.create_task(service.submit_order(db, cmd))
                await asyncio.wait_for(attempted.wait(), timeout=2)
                done, _ = await asyncio.wait({task}, timeout=.05)
                assert not done, "real SQLite writer must prevent reservation progress"
                clock[0] += timedelta(seconds=10)
                await blocker.commit()
                result = await asyncio.wait_for(task, timeout=3)
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                paper._QUOTE_ROUND_CONTEXT.reset(token)
                event.remove(engine, "before_cursor_execute", saw_write_attempt)
        order = (await db.scalars(select(TradeOrder))).one()
        assert order.decision_at == clock[0] > before_wait >= requested
        assert await db.scalar(select(func.count(TradeFill.id))) == 0
        assert await db.scalar(select(func.count(PaperTradeLog.id))) == 0
        assert await db.scalar(select(func.count(PaperPosition.id))) == 0
        wallet = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == PORTFOLIO_ACCOUNT))
        assert wallet.current_capital == 50000
        if cross_sla:
            assert result["order"]["status"] == "risk_blocked", result
            assert "allocation clock invalid" in order.error_message
            assert "stale" in order.error_message or "expired" in order.error_message
            assert signal.confirmed_at == AT
        else:
            assert result["order"]["status"] == "submitted", result
            metadata = json.loads(order.risk_json)["paper_deferred_order"]
            receipt = metadata["portfolio_reservation_clock"]
            assert receipt["requested_at"] == requested.isoformat()
            assert receipt["accepted_at"] == clock[0].isoformat()
            assert metadata["confirmed_at"] == AT.isoformat()
            completed = await reconcile(db, clock, "after-real-writer-wait")
            assert completed[0]["event"] == "filled", completed
            fill = (await db.scalars(select(TradeFill))).one()
            proof = json.loads(fill.raw_json)["pending_execution_timing"]["locked_risk"]
            assert proof["result"]["checked_rules"] == 10 and proof["status"] == "validated"
