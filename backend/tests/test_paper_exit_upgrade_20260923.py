"""A real auto-sell -> service -> risk/deferred/broker chain, isolated SQLite only."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from app.api.v1 import paper
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog
from app.models.stock import StockTag, MarketSentiment
from app.models.trading import TradeOrder, TradeFill
from app.paper.position_policy import position_exit_policy
from app.risk.engine import risk_engine
from app.risk.rules import register_all_rules
from app.trading import service, paper_authorization
from test_quote_round_execution import quote_execution_env, _round_payload
from paper_pending_fixture import accepted_frame

AT = datetime(2026, 9, 23, 10)
CODE = "600001"


@pytest_asyncio.fixture
async def environment(monkeypatch, quote_execution_env):
    # Decision holding-age now requires registered local calendar evidence, not
    # a manually primed valuation cache. Keep the original weak-rung scenario.
    from app.db import session as session_module
    from app.core import trade_calendar as calendar_module
    from app.models.governance import TradeCalendarModel
    monkeypatch.setattr(session_module, 'async_session', quote_execution_env)
    monkeypatch.setattr(calendar_module, 'async_session', quote_execution_env)
    async with quote_execution_env() as calendar_db:
        first = AT.replace(month=1, day=1).date()
        calendar_db.add_all([TradeCalendarModel(
            trade_date=first+timedelta(days=i),
            is_trade_day=(first+timedelta(days=i)).weekday()<5
                and not calendar_module.is_official_closed_day(first+timedelta(days=i)),
            session_type='full' if (first+timedelta(days=i)).weekday()<5
                and not calendar_module.is_official_closed_day(first+timedelta(days=i)) else 'closed',
        ) for i in range(365)])
        await calendar_db.commit()
    clock = [AT]
    monkeypatch.setattr(paper, "_TRADE_LOCK", asyncio.Lock())
    monkeypatch.setattr(paper, "_public_order_clock", lambda: clock[0])
    monkeypatch.setattr(paper, "_paper_now", lambda: clock[0])
    monkeypatch.setattr(paper.settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", False)
    monkeypatch.setattr(paper.trade_calendar, "_cache", {
        (AT-timedelta(days=i)).date(): (AT-timedelta(days=i)).weekday() < 5
        for i in range(45)})
    from app.core.trade_calendar import TradeCalendar
    async def loaded(self, year):
        assert self is paper.trade_calendar and year == 2026
    monkeypatch.setattr(TradeCalendar, "_ensure_loaded", loaded)
    monkeypatch.setattr(risk_engine, "_rules", [])
    register_all_rules()
    checked = []
    original = risk_engine.check
    def check(ctx):
        result = original(ctx)
        checked.append((paper_authorization.paper_transaction_active(current_db[0])
                        if current_db[0] else False, result))
        return result
    current_db = [None]
    monkeypatch.setattr(risk_engine, "check", check)
    # Keep real trigger/quote/T+1/sizing/risk. Only unrelated context IO is local.
    async def context(db, position, day):
        spot = await paper._spot_by_code(db, position.code)
        return dict(price=spot.price, open=12.05, high=12.1, low=spot.price,
                    avg_price=12.09, ma5=12, change_pct=-3, volume_ratio=2,
                    min5_change=-1, orderbook_imbalance=-.2)
    monkeypatch.setattr(paper, "_build_short_sell_context", context)
    def order_clock(_mapper, _connection, target):
        target.created_at = target.decision_at
    event.listen(TradeOrder, "before_insert", order_clock)
    yield clock, checked, current_db
    event.remove(TradeOrder, "before_insert", order_clock)
    assert checked and all(r["checked_rules"] == 10 and r["evaluation_status"] == "complete"
                           for _, r in checked)


async def seed(db, *, amount=300, reduction=100, account_name="default"):
    account = PaperAccount(account_name=account_name, initial_capital=50000,
        current_capital=50000-12.23*amount-5, total_assets=49995, status="active")
    db.add(account)
    await db.flush()
    version = paper._strategy_version(account_name)
    position = PaperPosition(account_id=account.id, code=CODE, name="隔离退出",
        buy_time=AT-timedelta(days=1), buy_price=12.23, buy_amount=amount,
        current_price=12.02, stop_loss_price=11.62, hold_days=1,
        strategy_version=version, is_closed=False)
    db.add(PaperTradeLog(account_id=account.id, code=CODE, trade_type="buy",
        price=12.23, amount=amount, commission=5, tax=0,
        trade_time=position.buy_time, strategy_version=version, signal_id="prior-fixture-buy"))
    db.add_all([position, StockTag(code=CODE, name="隔离退出", board_type="main_sh",
        board_tag="tradeable", is_st=False, is_suspended=False, is_delisting=False,
        is_ipo_recent=False), MarketSentiment(trade_date=AT.date(),
        sentiment_cycle="recovery", sentiment_score=60, quality_status="ok",
        observed_at=AT, calculation_version="isolated-exit-upgrade")])
    await db.commit()
    params, policy = await position_exit_policy(db, account_name=account_name,
        position=position, defaults=paper._strategy_sell_params(account), as_of=AT)
    candidate = dict(exit_trigger_reason="跌破分时均价：现价12.02 < 均价12.09，短线转弱",
        exit_policy=policy, exit_parameters=params, available_sell_amount=amount)
    result = await service.submit_order(db, service.SubmitOrderCommand(
        code=CODE, side="sell", price=12, quantity=reduction, broker="paper",
        account_id=account_name, strategy_id="paper-auto-short", strategy_version=version,
        source="position", reason=f"T减仓{reduction}股："+candidate["exit_trigger_reason"],
        decision_at=AT, decision_round_id="old-reduction", as_of_at=AT,
        idempotency_key="old:"+account_name, defer_until_next_round=True,
        deferred_metadata=dict(position_id=position.id, candidate=candidate,
            exit_decision_strategy_version=version, block_warn=False)))
    assert result["order"]["status"] == "submitted", result
    old = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == result["order"]["order_id"]))
    return account, position, old


def payload(clock, *, price=11.35, hands=10):
    return _round_payload("round-"+clock.isoformat(), clock, [
        dict(code=CODE, name="隔离退出", price=price, prev_close=12.23,
             open=12.05, high=12.1, low=price, limit_up=13.45, limit_down=11.01,
             bid1_price=price-.01, bid1_volume=hands, ask1_price=price+.01, ask1_volume=10)])


async def scan(db, account, frame, *, execute=True, **kwargs):
    token = paper._QUOTE_ROUND_CONTEXT.set(frame)
    try:
        return await paper._run_auto_sells(db, account=account, run_id=frame["round_id"],
            trade_date=AT.date(), trigger="test", execute=execute,
            quote_now=frame["committed_at"], log_holds=False, **kwargs)
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("old_entry", [False, True])
async def test_reduction_is_canceled_for_real_full_stop_without_repricing_history(
    quote_execution_env, environment, monkeypatch, old_entry,
):
    clock, checked, current_db = environment
    async with quote_execution_env() as db:
        current_db[0] = db
        account, position, old = await seed(db)
        old_price, old_quantity, old_created = old.price, old.quantity, old.created_at
        if old_entry:
            # New execution code must not migrate the original position version.
            monkeypatch.setattr(paper, "_strategy_version", lambda name: "new-exit-fixture-version")
        clock[0] = AT+timedelta(seconds=30)
        frame = payload(clock[0])
        await accepted_frame(db, frame)
        logs = await scan(db, account, frame)
        await db.refresh(old)
        assert old.status == "canceled", [(r.action, r.reason) for r in logs]
        assert (old.price, old.quantity, old.filled_quantity, old.created_at) == (old_price, old_quantity, 0, old_created)
        orders = list((await db.scalars(select(TradeOrder).order_by(TradeOrder.id))).all())
        assert len(orders) == 2
        replacement = orders[1]
        assert replacement.status == "submitted" and replacement.quantity == 300
        assert replacement.price < 11.35 and replacement.strategy_version == position.strategy_version
        assert json.loads(replacement.risk_json)["paper_deferred_order"]["candidate"]["exit_upgrade"]["old_order_id"] == old.order_id
        assert await db.scalar(select(func.count(TradeFill.id))) == 0
        assert position.buy_amount == 300
        await scan(db, account, frame)
        assert await db.scalar(select(func.count(TradeOrder.id))) == 2
        # New order MUST wait for a later real fixture quote, then original ten-rule
        # locked risk and T+1/fee/book/receipt chain, never rewritten old price.
        clock[0] += timedelta(seconds=30)
        next_frame = payload(clock[0])
        await accepted_frame(db, next_frame)
        token = paper._QUOTE_ROUND_CONTEXT.set(next_frame)
        try:
            result = await service.reconcile_paper_deferred_orders(
                db, account_id="default", round_id=next_frame["round_id"], now=clock[0])
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        assert len(result) == 1 and result[0]["event"] == "filled", [(r["event"], r.get("reason"), r.get("order", {}).get("error_message")) for r in result]
        await db.refresh(position)
        assert position.is_closed and position.buy_amount == 0
        assert old.filled_quantity == 0
        fill = await db.scalar(select(TradeFill))
        assert fill.order_id == replacement.order_id and fill.quantity == 300
        assert fill.commission == 5 and fill.tax > 0
        assert any(locked for locked, _ in checked)
        assert await db.scalar(select(func.count(PaperTradeLog.id)).where(PaperTradeLog.trade_type == "sell")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("execute,price", [(False, 11.35), (True, 12.02)])
async def test_dry_run_or_same_weak_trigger_does_not_cancel(
    quote_execution_env, environment, execute, price,
):
    clock, _, current_db = environment
    async with quote_execution_env() as db:
        current_db[0] = db
        account, _, old = await seed(db)
        clock[0] += timedelta(seconds=30)
        await scan(db, account, payload(clock[0], price=price), execute=execute)
        await db.refresh(old)
        assert old.status == "submitted" and old.filled_quantity == 0
        assert await db.scalar(select(func.count(TradeOrder.id))) == 1


@pytest.mark.asyncio
async def test_partial_fill_is_preserved_and_only_remainder_is_canceled(
    quote_execution_env, environment,
):
    clock, checked, current_db = environment
    async with quote_execution_env() as db:
        current_db[0] = db
        account, position, old = await seed(db, amount=600, reduction=200)
        clock[0] += timedelta(seconds=15)
        frame = payload(clock[0], price=12.02, hands=1)
        await accepted_frame(db, frame)
        token = paper._QUOTE_ROUND_CONTEXT.set(frame)
        try:
            partial = await service.reconcile_paper_deferred_orders(
                db, account_id="default", round_id=frame["round_id"], now=clock[0])
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        assert partial[0]["event"] == "partial", partial[0].get("reason")
        await db.refresh(position)
        assert old.filled_quantity == 100 and position.buy_amount == 500
        prior_fill = await db.scalar(select(TradeFill))
        prior_snapshot = (prior_fill.fill_id, prior_fill.raw_json, prior_fill.commission,
                          prior_fill.tax, prior_fill.quantity, prior_fill.filled_at)
        cash = account.current_capital
        clock[0] += timedelta(seconds=15)
        logs = await scan(db, account, payload(clock[0]))
        await db.refresh(old)
        assert old.status == "canceled" and old.filled_quantity == 100 and old.quantity == 200, [(r.reason, json.loads(r.candidate_json or "{}").get("exit_upgrade")) for r in logs]
        assert old.price == 12 and old.avg_fill_price == 12.01
        assert json.loads(old.risk_json)["paper_exit_upgrade"]["canceled_remaining_quantity"] == 100
        replacement = await db.scalar(select(TradeOrder).where(TradeOrder.id != old.id))
        assert replacement.status == "submitted" and replacement.quantity == 500
        await db.refresh(prior_fill)
        assert prior_snapshot == (prior_fill.fill_id, prior_fill.raw_json, prior_fill.commission,
                                  prior_fill.tax, prior_fill.quantity, prior_fill.filled_at)
        assert position.buy_amount == 500 and account.current_capital == cash
        clock[0] += timedelta(seconds=30)
        frame = payload(clock[0])
        await accepted_frame(db, frame)
        token = paper._QUOTE_ROUND_CONTEXT.set(frame)
        try:
            result = await service.reconcile_paper_deferred_orders(
                db, account_id="default", round_id=frame["round_id"], now=clock[0])
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        assert result[0]["event"] == "filled", result[0].get("reason")
        fills = list((await db.scalars(select(TradeFill))).all())
        assert sum(f.quantity for f in fills) == 600
        assert sum(f.commission for f in fills) == 10
        assert old.filled_quantity == 100 and replacement.filled_quantity == 500
        assert any(locked for locked, _ in checked)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "manual", "binding", "parameters", "policy", "version", "external", "full_old",
    "other_source", "unknown_state", "missing_fills", "multiple", "same_round",
    "future_old", "same_day_position", "orphan_ledger", "stale_quote", "limit_down",
    "missing_decision_version", "wrong_metadata_clock", "wrong_metadata_version",
])
async def test_ambiguous_or_invalid_upgrade_never_cancels(quote_execution_env, environment, bad):
    clock, _, current_db = environment
    async with quote_execution_env() as db:
        current_db[0] = db
        account, position, old = await seed(db)
        risk = json.loads(old.risk_json)
        d = risk["paper_deferred_order"]
        if bad == "missing_decision_version":
            d.pop("exit_decision_strategy_version")
        elif bad == "wrong_metadata_clock":
            d["decision_at"] = (AT+timedelta(seconds=1)).isoformat()
        elif bad == "wrong_metadata_version":
            d["strategy_version"] = "unrelated"
        elif bad == "manual":
            old.reason = "人工减仓"
        elif bad == "binding":
            d["position_id"] += 99
        elif bad == "parameters":
            d["candidate"]["exit_parameters"] = {}
        elif bad == "policy":
            d["candidate"]["exit_policy"]["position_strategy_version"] = "other"
        elif bad == "version":
            old.strategy_version = "other"
        elif bad == "external":
            old.external_order_id = "paper-queue-unknown"
        elif bad == "full_old":
            d["candidate"]["exit_trigger_reason"] = "触发硬止损"
        elif bad == "other_source":
            old.source = "manual"
        elif bad == "unknown_state":
            old.status = "pending"
        elif bad == "missing_fills":
            old.quantity = 200
            old.reason = "T减仓200股：跌破分时均价"
            old.filled_quantity = 100
            old.status = "partial"
        elif bad == "multiple":
            db.add(TradeOrder(order_id="other", broker="paper", account_id="default",
                code=CODE, side="sell", order_type="limit", price=12, quantity=100, status="submitted",
                trade_date=AT.date(), decision_at=AT))
        elif bad == "future_old":
            old.decision_at = AT+timedelta(minutes=5)
        elif bad == "same_day_position":
            position.buy_time = AT
        elif bad == "orphan_ledger":
            db.add(PaperTradeLog(account_id=account.id, code=CODE, trade_type="sell",
                price=12, amount=100, commission=5, tax=.6, trade_time=AT,
                signal_id="orphan", decision_round_id="old", fill_round_id="unknown"))
        old.risk_json = json.dumps(risk)
        await db.commit()
        clock[0] += timedelta(seconds=30)
        frame = payload(clock[0])
        if bad == "same_round":
            frame["round_id"] = old.decision_round_id
        if bad == "stale_quote":
            frame["records"][0]["source_quote_at"] = AT-timedelta(minutes=10)
        if bad == "limit_down":
            frame["records"][0].update(price=11.01, bid1_price=0, bid1_volume=0)
        prior_status = old.status
        await scan(db, account, frame)
        await db.refresh(old)
        assert old.status == prior_status
        assert "paper_exit_upgrade" not in json.loads(old.risk_json)
        assert await db.scalar(select(func.count(TradeOrder.id))) == (2 if bad == "multiple" else 1)


@pytest.mark.asyncio
async def test_replacement_still_obeys_original_risk_and_never_claims_a_fill(
    quote_execution_env, environment,
):
    clock, _, current_db = environment
    async with quote_execution_env() as db:
        current_db[0] = db
        account, position, old = await seed(db)
        tag = await db.get(StockTag, CODE)
        tag.is_suspended = True
        await db.commit()
        clock[0] += timedelta(seconds=30)
        logs = await scan(db, account, payload(clock[0]))
        await db.refresh(old)
        assert old.status == "canceled"
        new = await db.scalar(select(TradeOrder).where(TradeOrder.id != old.id))
        assert new.status == "risk_blocked" and "停牌" in new.error_message
        assert any(row.action == "skip_sell" and row.decision == "blocked" for row in logs)
        assert all(row.executed_trade_id is None for row in logs)
        assert position.buy_amount == 300
        assert await db.scalar(select(func.count(TradeFill.id))) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["order", "position", "late", "orphan"])
async def test_waiting_for_fill_lock_revalidates_order_position_clock_and_receipts(
    quote_execution_env, environment, monkeypatch, mutation,
):
    from sqlalchemy import update
    from test_paper_locked_risk_20260914 import MutationBeforeAcquisition
    clock, _, current_db = environment
    async with quote_execution_env() as db:
        current_db[0] = db
        account, position, old = await seed(db)
        clock[0] += timedelta(seconds=30)
        frame = payload(clock[0])
        old_id = old.id
        async def changed():
            if mutation == "late":
                clock[0] += timedelta(minutes=10)
                return
            async with quote_execution_env() as other:
                if mutation == "order":
                    await other.execute(update(TradeOrder).where(TradeOrder.id == old_id).values(status="canceled"))
                elif mutation == "position":
                    await other.execute(update(PaperPosition).where(PaperPosition.id == position.id).values(stop_loss_price=11.5))
                else:
                    other.add(PaperTradeLog(account_id=account.id, code=CODE, trade_type="sell",
                        price=12, amount=100, commission=5, tax=.6, trade_time=AT,
                        signal_id="orphan", decision_round_id="old", fill_round_id="unknown"))
                await other.commit()
        gate = MutationBeforeAcquisition(changed)
        monkeypatch.setattr(paper, "_TRADE_LOCK", gate)
        if mutation == "order":
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as caught:
                await scan(db, account, frame)
            assert caught.value.status_code == 409
            assert paper_authorization.paper_execution_requires_reconciliation(caught.value)
        else:
            await scan(db, account, frame)
        assert gate.called
        await db.refresh(old)
        assert old.status == ("canceled" if mutation == "order" else "submitted")
        assert "paper_exit_upgrade" not in json.loads(old.risk_json)
        assert await db.scalar(select(func.count(TradeOrder.id))) == 1


@pytest.mark.asyncio
async def test_lost_cancel_ack_propagates_without_fake_rejection_or_second_order(
    quote_execution_env, environment, monkeypatch,
):
    clock, _, current_db = environment
    async with quote_execution_env() as db:
        current_db[0] = db
        account, _, old = await seed(db)
        clock[0] += timedelta(seconds=30)
        commit = db.commit
        async def lost_ack():
            active = paper_authorization.paper_transaction_active(db)
            await commit()
            if active:
                raise RuntimeError("fixture lost COMMIT acknowledgment")
        monkeypatch.setattr(db, "commit", lost_ack)
        with pytest.raises(RuntimeError) as caught:
            await scan(db, account, payload(clock[0]))
        assert paper_authorization.paper_execution_requires_reconciliation(caught.value)
        await db.refresh(old)
        assert old.status == "canceled"
        assert await db.scalar(select(func.count(TradeOrder.id))) == 1
        assert await db.scalar(select(func.count(TradeFill.id))) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("account_name", [
    "default", "promotion", "mainline", "auction", "tenbagger", "reversal",
    "challenger_a", "challenger_b", "challenger_c", "challenger_d", "challenger_e", "challenger_f2",
])
async def test_original_t_reduction_remains_account_isolated_for_all_routes(
    quote_execution_env, environment, account_name,
):
    clock, _, current_db = environment
    async with quote_execution_env() as db:
        current_db[0] = db
        account, position, old = await seed(db, account_name=account_name)
        # Unrelated account order of the same code is never canceled or pooled.
        db.add(TradeOrder(order_id="independent", broker="paper", account_id="unrelated",
            code=CODE, side="sell", order_type="limit", price=12, quantity=100,
            status="submitted", trade_date=AT.date(), decision_at=AT))
        await db.commit()
        clock[0] += timedelta(seconds=30)
        await scan(db, account, payload(clock[0]))
        await db.refresh(old)
        assert old.status == "canceled"
        own = list((await db.scalars(select(TradeOrder).where(TradeOrder.account_id == account_name))).all())
        assert len(own) == 2 and own[1].quantity == 300 and own[1].status == "submitted"
        assert position.buy_amount == 300 and position.strategy_version == old.strategy_version
        other = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == "independent"))
        assert other.status == "submitted" and other.price == 12 and other.filled_quantity == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["late_during_check", "rollback", "partial_receipt_external"])
async def test_final_cancel_boundary_preserves_old_order_on_fault(
    quote_execution_env, environment, monkeypatch, fault,
):
    clock, _, current_db = environment
    async with quote_execution_env() as db:
        current_db[0] = db
        account, _, old = await seed(db, amount=600, reduction=200)
        if fault == "partial_receipt_external":
            clock[0] += timedelta(seconds=15)
            frame = payload(clock[0], price=12.02, hands=1)
            await accepted_frame(db, frame)
            token = paper._QUOTE_ROUND_CONTEXT.set(frame)
            try:
                result = await service.reconcile_paper_deferred_orders(
                    db, account_id="default", round_id=frame["round_id"], now=clock[0])
            finally:
                paper._QUOTE_ROUND_CONTEXT.reset(token)
            assert result[0]["event"] == "partial"
            old.external_order_id = "paper-pf-unrelated"
            await db.commit()
        original_status = old.status
        if fault == "late_during_check":
            original = service.account_execution_integrity_evidence
            async def slow(*args, **kwargs):
                result = await original(*args, **kwargs)
                clock[0] += timedelta(minutes=10)
                return result
            monkeypatch.setattr(service, "account_execution_integrity_evidence", slow)
        if fault == "rollback":
            original_commit = db.commit
            async def rejected_commit():
                if paper_authorization.paper_transaction_active(db):
                    raise RuntimeError("fixture commit not written")
                return await original_commit()
            monkeypatch.setattr(db, "commit", rejected_commit)
        clock[0] = AT+timedelta(seconds=30)
        if fault == "rollback":
            with pytest.raises(RuntimeError):
                await scan(db, account, payload(clock[0]))
        else:
            await scan(db, account, payload(clock[0]))
        await db.refresh(old)
        assert old.status == original_status
        assert "paper_exit_upgrade" not in json.loads(old.risk_json)
        assert await db.scalar(select(func.count(TradeOrder.id))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("old_is_watchdog", [False, True])
async def test_same_bound_challenger_position_may_cross_original_exit_call_paths(
    quote_execution_env, environment, old_is_watchdog,
):
    from app.paper.account_policy import ROUTE_ACCOUNT_NAMES
    clock, _, current_db = environment
    account_name = "challenger_b"
    route = next(k for k, v in ROUTE_ACCOUNT_NAMES.items() if v == account_name)
    async with quote_execution_env() as db:
        current_db[0] = db
        account, _, old = await seed(db, account_name=account_name)
        if not old_is_watchdog:
            old.strategy_id, old.source = "paper-challenger-forward", route
            await db.commit()
        clock[0] += timedelta(seconds=30)
        kwargs = (dict(order_strategy_id="paper-challenger-forward", order_source=route)
                  if old_is_watchdog else {})
        await scan(db, account, payload(clock[0]), **kwargs)
        await db.refresh(old)
        assert old.status == "canceled"
        assert await db.scalar(select(func.count(TradeOrder.id))) == 2


def test_all_account_exit_identities_rotate_but_parameters_do_not(monkeypatch):
    from app.paper import experiment, account_policy
    for version in (experiment.execution_version, experiment.standard_execution_version):
        for name in experiment.EXPERIMENT_ACCOUNTS:
            current = version("base", name)
            params = account_policy.account_parameter_snapshot(name)
            with monkeypatch.context() as patch:
                patch.setattr(experiment, "EXIT_UPGRADE_CONTRACT_VERSION", "prior")
                assert version("base", name) != current
                assert account_policy.account_parameter_snapshot(name) == params
