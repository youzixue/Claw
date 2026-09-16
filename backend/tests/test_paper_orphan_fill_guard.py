"""Legacy crash-window containment; explicitly seed the old split-commit defect.

Current booking flushes inside one atomic service unit. These tests deliberately
commit the ledger early to represent evidence left by an older deployment; the
history guard must still refuse replay or a fabricated new receipt.
"""
import asyncio
import json
import uuid
from types import SimpleNamespace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from paper_pending_fixture import accepted_frame

import pytest
from sqlalchemy import func, select

from app.api.v1 import paper
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog
from app.models.trading import TradeFill, TradeOrder
from app.trading import service
from app.trading.broker import PaperBrokerAdapter
from test_quote_round_execution import quote_execution_env, _round_payload

AT = datetime(2026, 9, 9, 10)
ACCOUNTS = (*paper.PAPER_ALL_ACCOUNTS, *paper.PAPER_CHALLENGER_ACCOUNTS)


@pytest.fixture(autouse=True)
def isolate_crash_window_from_entry_signal_policy(monkeypatch):
    # These fixtures deliberately have no route evidence: exercise real booking
    # and orphan containment, not buy selection. Buy validity has its own suite.
    monkeypatch.setattr(service, "_requires_pending_buy_validity", lambda _order: False)


def identity(account, side):
    if side == "sell":
        return "paper-auto-short", "position", "auto-sell-fixture"
    if account == paper.PAPER_ACCOUNT_CHALLENGER_E:
        return "paper-auto-short", "tenbagger_midline", "auto-tenbagger_midline-fixture"
    if account in paper.PAPER_CHALLENGER_ACCOUNTS:
        route = next(k for k,v in paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.items() if v == account)
        return "paper-challenger-forward", route, "chlg-fixture"
    return "paper-auto-short", "next_day_plan", "auto-fixture"


def quote(round_id, at, *, queued=False):
    row = dict(code="600001", price=10.0, ask1_price=10.0, ask1_volume=1,
               bid1_price=10.0, bid1_volume=1, volume=1000, limit_up=11.0)
    if queued:
        row.update(price=11.0, ask1_price=0, bid1_price=11.0, bid1_volume=10)
    return _round_payload(round_id, at, [row])


async def seed(db, account, side):
    a = await paper._get_or_create_account(db, account)
    if side == "sell":
        db.add(PaperPosition(account_id=a.id, code="600001", buy_price=9.0,
            buy_amount=500, buy_time=AT - timedelta(days=1), is_closed=False,
            current_price=10, strategy_version=paper._strategy_version(account)))
        db.add(PaperTradeLog(account_id=a.id, code="600001", trade_type="buy",
            price=9.0, amount=500, trade_time=AT-timedelta(days=1), commission=5, tax=0,
            signal_id="prior-session-fixture", strategy_version=paper._strategy_version(account)))
    await db.commit()


async def place_deferred(sessions, account, side, *, queued=False):
    async with sessions() as db:
        await seed(db, account, side)
        strategy, source, signal = identity(account, side)
        token = paper._QUOTE_ROUND_CONTEXT.set(quote("decision", AT, queued=queued))
        try:
            r = await service.submit_order(db, service.SubmitOrderCommand(
                code="600001", side=side, price=11 if queued else 10, quantity=300,
                account_id=account, strategy_id=strategy, source=source, signal_id=signal,
                idempotency_key=f"fixture:{account}:{side}", decision_at=AT,
                queue_if_limit_up=queued,
                queue_metadata={"cancel_time":"14:50","block_warn":True} if queued else None,
                defer_until_next_round=not queued, deferred_metadata={"block_warn":side=="buy"}))
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        assert r["order"]["status"] == "submitted"
        return r["order"]["order_id"]


async def reconcile(sessions, account, round_id, at, *, queued=False):
    async with sessions() as db:
        payload = quote(round_id, at)
        if queued:
            # Queue book/rollback tests require a real full 300-share opening book.
            # Do not enlarge the ordinary deferred one-hand partial fixture.
            payload["records"][0].update(ask1_volume=3, bid1_price=9.99)
        await accepted_frame(db, payload)
        token = paper._QUOTE_ROUND_CONTEXT.set(payload)
        try:
            # Explicit fixture observation time, not the machine's present date.
            # These mechanics cases opt in through this helper; clock-race tests
            # use their own independent mutable clock and do not call it.
            with patch.object(paper, "_public_order_clock", return_value=at):
                if queued:
                    return await service.reconcile_paper_limit_up_orders(db, account_id=account, now=at)
                return await service.reconcile_paper_deferred_orders(db, account_id=account, round_id=round_id, now=at)
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_cancel_after_durable_book_commit_cannot_fill_again_in_new_round(
    quote_execution_env, monkeypatch, account, side,
):
    sessions = quote_execution_env
    # Isolate the commit-window behavior only; production risk code is NOT changed.
    monkeypatch.setattr(service, "_pre_trade_risk_check",
                        AsyncMock(return_value={"final_level":"pass","block_reasons":[],"warnings":[]}))
    order_id = await place_deferred(sessions, account, side)
    real = PaperBrokerAdapter.place_order
    async def cancel_after_booking(self, db, req):
        await real(self, db, req)
        await db.commit()  # test-only: reproduce a legacy pre-receipt durable book
        raise asyncio.CancelledError()
    with monkeypatch.context() as patch:
        patch.setattr(PaperBrokerAdapter, "place_order", cancel_after_booking)
        with pytest.raises(asyncio.CancelledError):
            await reconcile(sessions, account, "fill-1", AT+timedelta(seconds=30))
    async with sessions() as db:
        trades = list((await db.scalars(select(PaperTradeLog).where(PaperTradeLog.trade_type==side))).all())
        assert len(trades) == 1
        orphan = trades[0]
        original = (orphan.id, orphan.amount, orphan.price, orphan.signal_id,
                    orphan.trade_time, orphan.fill_round_id, orphan.commission, orphan.tax)
        assert await db.scalar(select(func.count(TradeFill.id))) == 0
        order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id==order_id))
        assert order.status == "submitted" and order.filled_quantity == 0
    results = await reconcile(sessions, account, "fill-2", AT+timedelta(seconds=60))
    assert results[0]["event"] == "risk_blocked"
    assert results[0]["order"]["risk_level"] == "block"
    assert results[0]["risk"]["final_level"] == "block"
    assert results[0]["reason"] in results[0]["risk"]["block_reasons"]
    assert results[0]["fills"] == []
    assert results[0]["risk"]["paper_execution_integrity"]["reason_code"] == "paper_trade_without_fill"
    async with sessions() as db:
        trades = list((await db.scalars(select(PaperTradeLog).where(PaperTradeLog.trade_type==side))).all())
        assert len(trades) == 1
        t=trades[0]
        assert (t.id,t.amount,t.price,t.signal_id,t.trade_time,t.fill_round_id,t.commission,t.tax) == original
        assert await db.scalar(select(func.count(TradeFill.id))) == 0
        position = await db.scalar(select(PaperPosition).where(PaperPosition.code=="600001"))
        assert position.buy_amount == (100 if side=="buy" else 400)
    assert await reconcile(sessions, account, "fill-3", AT+timedelta(seconds=90)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("account", [paper.PAPER_ACCOUNT_TENBAGGER, paper.PAPER_ACCOUNT_CHALLENGER_E])
async def test_queue_orphan_must_not_be_replayed_with_later_fill_timestamp(
    quote_execution_env, monkeypatch, account,
):
    sessions = quote_execution_env
    monkeypatch.setattr(service, "_pre_trade_risk_check",
                        AsyncMock(return_value={"final_level":"pass","block_reasons":[],"warnings":[]}))
    await place_deferred(sessions, account, "buy", queued=True)
    real = PaperBrokerAdapter.place_order
    async def cancel_after_booking(self, db, req):
        await real(self, db, req)
        await db.commit()  # test-only: reproduce a legacy pre-receipt durable book
        raise asyncio.CancelledError()
    with monkeypatch.context() as patch:
        patch.setattr(PaperBrokerAdapter, "place_order", cancel_after_booking)
        with pytest.raises(asyncio.CancelledError):
            await reconcile(sessions, account, "fill-1", AT+timedelta(seconds=30), queued=True)
    result=await reconcile(sessions, account, "fill-2", AT+timedelta(seconds=60), queued=True)
    assert result[0]["event"]=="risk_blocked"
    assert result[0]["risk"]["final_level"] == "block"
    assert result[0]["fills"]==[]
    async with sessions() as db:
        trade=(await db.scalars(select(PaperTradeLog))).one()
        assert trade.trade_time==AT+timedelta(seconds=30)
        assert await db.scalar(select(func.count(TradeFill.id)))==0
        assert (await db.scalars(select(PaperPosition))).one().buy_amount==300


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("side", ["buy", "sell"])
async def test_orphan_after_legitimate_partial_preserves_both_books_and_first_fill(
    quote_execution_env, monkeypatch, account, side,
):
    sessions = quote_execution_env
    monkeypatch.setattr(service, "_pre_trade_risk_check",
                        AsyncMock(return_value={"final_level":"pass","block_reasons":[],"warnings":[]}))
    order_id = await place_deferred(sessions, account, side)
    first = await reconcile(sessions, account, "fill-1", AT+timedelta(seconds=30))
    assert first[0]["event"] == "partial"
    async with sessions() as db:
        order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == order_id))
        payload = json.loads(order.risk_json)
        payload["experiment_entry"] = {"original": True, "observed_at": "decision-clock"}
        order.risk_json = json.dumps(payload)
        await db.commit()
    real = PaperBrokerAdapter.place_order
    async def cancel_after_booking(self, db, req):
        await real(self, db, req)
        await db.commit()  # test-only: reproduce a legacy pre-receipt durable book
        raise asyncio.CancelledError()
    with monkeypatch.context() as patch:
        patch.setattr(PaperBrokerAdapter, "place_order", cancel_after_booking)
        with pytest.raises(asyncio.CancelledError):
            await reconcile(sessions, account, "fill-2", AT+timedelta(seconds=60))
    result = await reconcile(sessions, account, "fill-3", AT+timedelta(seconds=90))
    assert result[0]["event"] == "risk_blocked"
    assert result[0]["order"]["filled_quantity"] == 100  # do NOT fabricate acknowledgement
    assert result[0]["risk"]["experiment_entry"] == {"original": True, "observed_at": "decision-clock"}
    assert result[0]["risk"]["paper_execution_integrity"]["original_fill_round_id"] == "fill-2"
    async with sessions() as db:
        trades = list((await db.scalars(select(PaperTradeLog).where(
            PaperTradeLog.trade_type == side).order_by(PaperTradeLog.id))).all())
        assert len(trades) == 2
        assert [t.amount for t in trades] == [100, 100]
        assert [t.trade_time for t in trades] == [AT+timedelta(seconds=30), AT+timedelta(seconds=60)]
        fills = list((await db.scalars(select(TradeFill))).all())
        assert len(fills) == 1
        assert fills[0].broker_trade_id == str(trades[0].id)
        assert fills[0].filled_at == AT+timedelta(seconds=30)
        position = (await db.scalars(select(PaperPosition))).one()
        assert position.buy_amount == (200 if side == "buy" else 300)


@pytest.mark.parametrize("quantity", [None, 0, 100, 200, 2**63 - 100])
@pytest.mark.parametrize("queued", [False, True])
def test_request_key_is_backward_compatible(quantity, queued):
    order = SimpleNamespace(order_id="existing-order", filled_quantity=quantity)
    namespace = "limit-queue:existing-order" if queued else f"existing-order:original-round:{quantity or 0}"
    expected = ("lqf-" if queued else "pf-") + uuid.uuid5(uuid.NAMESPACE_URL, namespace).hex[:24]
    assert service._paper_fill_request_id(order, round_id="original-round", queued=queued) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("case,blocked", [
    ("orphan", True), ("linked", False), ("wrong_fill_id", True),
    ("duplicate_links", True), ("other_account", False), ("other_code", False),
    ("other_side", False), ("other_request", False), ("legacy_no_round", False),
    ("wrong_prefix", False), ("other_broker_fill", True), ("other_order_fill", True),
])
async def test_exact_identity_guard_does_not_guess_historical_links(
    quote_execution_env, monkeypatch, case, blocked,
):
    sessions = quote_execution_env
    monkeypatch.setattr(service, "_pre_trade_risk_check",
                        AsyncMock(return_value={"final_level":"pass","block_reasons":[],"warnings":[]}))
    order_id = await place_deferred(sessions, paper.PAPER_ACCOUNT_DEFAULT, "buy")
    async with sessions() as db:
        order = await db.scalar(select(TradeOrder).where(TradeOrder.order_id == order_id))
        account = (await db.scalars(select(PaperAccount))).one()
        request = service._paper_fill_request_id(order, round_id="fill-1")
        trade = PaperTradeLog(account_id=account.id + (1 if case == "other_account" else 0),
            code="600002" if case == "other_code" else "600001",
            trade_type="sell" if case == "other_side" else "buy",
            price=10, amount=100, commission=5, tax=0,
            trade_time=AT+timedelta(seconds=30),
            signal_id=("unrelated-request" if case == "other_request" else
                       f"not-an-approved-prefix-{request}" if case == "wrong_prefix" else request),
            fill_round_id=None if case == "legacy_no_round" else "fill-1")
        db.add(trade)
        await db.flush()
        if case in {"linked", "wrong_fill_id", "duplicate_links", "other_broker_fill", "other_order_fill"}:
            for n in range(2 if case == "duplicate_links" else 1):
                db.add(TradeFill(
                    fill_id=("wrong-id" if case == "wrong_fill_id" else f"fill-{request}") + ("-duplicate" if n else ""),
                    order_id="different-order" if case == "other_order_fill" else order_id,
                    broker="not-paper" if case == "other_broker_fill" else "paper",
                    code="600001", side="buy", price=10, quantity=100,
                    broker_trade_id=str(trade.id), filled_at=trade.trade_time))
        await db.commit()
        before_trade = (trade.id, trade.signal_id, trade.trade_time, trade.fill_round_id)
        before_fills = await db.scalar(select(func.count(TradeFill.id)))
        outcome = await service._block_unreconciled_paper_order(db, order)
        assert (outcome is not None) is blocked
        assert order.status == ("risk_blocked" if blocked else "submitted")
        if blocked:
            stored_risk = json.loads(order.risk_json)
            assert stored_risk["final_level"] == outcome["risk"]["final_level"] == "block"
            assert stored_risk["paper_execution_integrity"]["previous_final_level"] == "pass"
        assert await db.scalar(select(func.count(TradeFill.id))) == before_fills
        await db.refresh(trade)
        assert (trade.id, trade.signal_id, trade.trade_time, trade.fill_round_id) == before_trade
        if case in {"wrong_fill_id", "duplicate_links"}:
            assert outcome["risk"]["paper_execution_integrity"]["reason_code"] == "paper_trade_fill_identity_conflict"
