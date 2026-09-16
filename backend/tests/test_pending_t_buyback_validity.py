"""T deferred buy contract: memory DB + fixture broker only, no live orders/messages."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.config.settings import settings
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog, PaperAutoTradeLog
from app.models.trading import TradeOrder, TradeFill
from app.trading import service
from test_paper_deferred_exit_provenance import memory_session
from paper_pending_fixture import accepted_frame

AT = datetime(2026, 9, 10, 10)


def quote(at, **changes):
    return SimpleNamespace(**{**dict(
        code="600001", name="isolated", price=10., prev_close=10., open=10.,
        high=10.2, low=9.8, limit_up=11., limit_down=9., avg_price=9.9,
        ask1_price=10., ask1_volume=10, bid1_price=9.99, bid1_volume=10,
        orderbook_imbalance=.2, volume_ratio=2., change_pct=0.,
        updated_at=at, source_quote_at=at, received_at=at, quote_round_id="fill",
    ), **changes})


async def setup(db, monkeypatch, account_name="default", quantity=100, from_loop=False,
                archived=False, future=None):
    monkeypatch.setattr(service, "experiment_active", lambda *a, **k: False)
    monkeypatch.setattr(settings, "PAPER_DEPTH_MAX_PARTICIPATION_RATIO", 1.)
    monkeypatch.setattr(settings, "PAPER_AUTO_TRADE_T_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_DEFER_AUTO_FILL_TO_NEXT_ROUND", True)
    risk = AsyncMock(return_value={"final_level": "pass", "warnings": [], "block_reasons": []})
    monkeypatch.setattr(service, "_pre_trade_risk_check", risk)
    broker = AsyncMock()
    async def fill(_db, request):
        return SimpleNamespace(accepted=True, external_order_id="fixture", fills=[SimpleNamespace(
            fill_id=f"fixture-{request.order_id}", price=request.price, quantity=request.quantity,
            commission=5., tax=0., realized_pnl=0., broker_trade_id="1",
            filled_at=request.filled_at, raw={})])
    broker.place_order.side_effect = fill
    monkeypatch.setattr(service, "get_broker_adapter", lambda _: broker)
    if archived:
        db.add(PaperAccount(account_name=account_name, initial_capital=1., status="closed"))
        await db.flush()
    account = PaperAccount(account_name=account_name, initial_capital=100000., current_capital=100000.)
    db.add(account)
    await db.flush()
    position = PaperPosition(account_id=account.id, code="600001", name="isolated",
        buy_price=10., buy_amount=500, buy_time=AT-timedelta(days=1),
        strategy_version=paper._strategy_version(account_name), is_closed=False)
    sale = PaperTradeLog(account_id=account.id, code="600001", trade_type="sell",
        price=10.5, amount=300, trade_time=AT-timedelta(minutes=2))
    db.add_all([position, sale])
    if future in {"sell", "buy"}:
        db.add(PaperTradeLog(account_id=account.id, code="600001", trade_type=future,
            price=20., amount=900, trade_time=AT+timedelta(minutes=5)))
    elif future == "count":
        db.add(PaperAutoTradeLog(account_id=account.id, code="600001", source="position-t",
            action="buy", decision="executed", run_id="future", trade_date=AT.date(),
            created_at=AT+timedelta(minutes=5)))
    await db.commit()
    ctx = dict(avg_price=9.9, min5_change=.1, orderbook_imbalance=.2, sector_retreat_reason="")
    context = AsyncMock(side_effect=lambda *a, **kw: dict(ctx))
    monkeypatch.setattr(paper, "_build_short_sell_context", context)
    stats = await paper._today_sell_stats(db, account.id, position.code, AT.date(), as_of=AT)
    candidate = {**ctx, "code": position.code, "_source": "position-t",
        "t_buyback": paper._t_buyback_identity(position, stats, 0)}
    if from_loop:
        monkeypatch.setattr(paper, "_paper_now", lambda: AT)
        monkeypatch.setattr(paper, "_spot_by_code", AsyncMock(return_value=quote(AT)))
        monkeypatch.setattr(paper, "_execution_quote_status", lambda *a, **k: (True, ""))
        monkeypatch.setattr(paper, "_risk_check_for_buy", risk)
        monkeypatch.setattr(paper, "_has_active_paper_order", AsyncMock(return_value=False))
        monkeypatch.setattr(paper, "_add_auto_log", AsyncMock(return_value=None))
        monkeypatch.setattr("app.push.paper_buy_points.record_buy_point", AsyncMock())
        token = paper._QUOTE_ROUND_CONTEXT.set({"round_id": "decision", "as_of_at": AT})
        try:
            await paper._run_auto_t_buybacks(db, account=account, run_id="isolated",
                trade_date=AT.date(), trigger="test", execute=True)
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
    else:
        await service.submit_order(db, service.SubmitOrderCommand(
            code=position.code, side="buy", price=10.02, quantity=quantity, broker="paper",
            account_id=account_name, strategy_id="paper-auto-t", strategy_version=position.strategy_version,
            signal_id="isolated-t", source="position-t", decision_at=AT+timedelta(seconds=5),
            as_of_at=AT, decision_round_id="decision", defer_until_next_round=True,
            deferred_metadata={"candidate": candidate, "confirmed_at": AT.isoformat(), "block_warn": True}))
    order = await db.scalar(select(TradeOrder))
    assert order is not None and order.status == "submitted"
    return order, position, sale, broker, ctx


async def reconcile(db, monkeypatch, order, at=None, *, accepted=False, **fields):
    at = at or AT+timedelta(seconds=30)
    round_id = fields.pop("round_id", "fill")
    spot = quote(at, quote_round_id=round_id, **fields)
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(return_value=spot))
    payload = {
        "round_id": round_id, "quality_status": "ok", "committed_at": at, "as_of_at": at,
        "config_version": "fixture-config", "code_version": "fixture-code",
        "records": [],
    }
    if accepted:
        # Positive matching frame only; original decision/confirmation stays frozen.
        payload["records"] = [vars(spot).copy()]
        await accepted_frame(db, payload)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: at)
    token = paper._QUOTE_ROUND_CONTEXT.set(payload)
    try:
        return await service.reconcile_paper_deferred_orders(
            db, account_id=order.account_id, now=at, round_id=round_id)
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ["default", "promotion", "mainline", "auction", "challenger_c"])
async def test_real_t_producer_freezes_original_identity_not_entry_route(memory_session, monkeypatch, account):
    order, pos, sale, broker, ctx = await setup(memory_session, monkeypatch, account, from_loop=True)
    meta = json.loads(order.risk_json)["paper_deferred_order"]
    contract = meta["buy_validity"]
    assert contract["status"] == "valid" and "route_id" not in contract
    assert contract["confirmed_at"] == AT.isoformat()
    assert contract["t_buyback"]["position_id"] == pos.id
    assert contract["t_buyback"]["sell_first_time"] == sale.trade_time.isoformat()
    result = await reconcile(memory_session, monkeypatch, order, accepted=True)
    assert result[0]["event"] == "filled"
    assert broker.place_order.await_count == 1


@pytest.mark.asyncio
async def test_t_original_ttl_expires_without_reconfirmation(memory_session, monkeypatch):
    order, _, _, broker, _ = await setup(memory_session, monkeypatch)
    meta = json.loads(order.risk_json)["paper_deferred_order"]
    at = datetime.fromisoformat(meta["buy_validity"]["expires_at"])+timedelta(seconds=1)
    result = await reconcile(memory_session, monkeypatch, order, at=at)
    assert result[0]["event"] == "canceled" and "buy_signal_expired" in result[0]["reason"]
    assert order.filled_quantity == 0 and broker.place_order.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cost", "drop", "vwap", "sector", "min5", "imbalance"])
async def test_t_original_predicates_rechecked_before_fill(memory_session, monkeypatch, failure):
    order, _, _, broker, ctx = await setup(memory_session, monkeypatch)
    fields = {}
    if failure == "cost":
        fields = {"price": 10.3, "ask1_price": 10.3}
    elif failure == "drop":
        fields = {"price": 10.49, "ask1_price": 10.49}
    elif failure == "vwap":
        fields = {"avg_price": 10.1}
    elif failure == "sector":
        ctx["sector_retreat_reason"] = "所属板块退潮"
    elif failure == "min5":
        ctx["min5_change"] = -1.5
    else:
        fields = {"orderbook_imbalance": -.35}
    result = await reconcile(memory_session, monkeypatch, order, **fields)
    assert result[0]["event"] == "canceled"
    assert broker.place_order.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["legacy", "missing_t", "version", "closed", "sell", "cost", "size", "clock"])
async def test_t_original_evidence_failclosed(memory_session, monkeypatch, failure):
    db = memory_session
    order, pos, sale, broker, _ = await setup(db, monkeypatch)
    meta = json.loads(order.risk_json)
    if failure == "legacy":
        meta["paper_deferred_order"].pop("buy_validity")
    elif failure == "missing_t":
        meta["paper_deferred_order"]["buy_validity"].pop("t_buyback")
    elif failure == "version":
        pos.strategy_version = "changed"
    elif failure == "closed":
        pos.is_closed = True
    elif failure == "sell":
        sale.amount += 100
    elif failure == "cost":
        pos.buy_price = 8.
    elif failure == "size":
        pos.buy_amount += 100
    else:
        meta["paper_deferred_order"]["buy_validity"]["confirmed_at"] = (AT+timedelta(days=1)).isoformat()
    order.risk_json = json.dumps(meta)
    await db.commit()
    result = await reconcile(db, monkeypatch, order)
    assert result[0]["event"] in {"canceled", "risk_blocked"}
    assert broker.place_order.await_count == 0 and not list((await db.scalars(select(TradeFill))).all())


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["avg_price", "orderbook_imbalance"])
async def test_missing_t_evidence_waits_without_ttl_refresh(memory_session, monkeypatch, field):
    order, _, _, broker, _ = await setup(memory_session, monkeypatch)
    before = json.loads(order.risk_json)["paper_deferred_order"]["buy_validity"]
    result = await reconcile(memory_session, monkeypatch, order, **{field: None})
    assert result[0]["event"] == "waiting"
    after = json.loads(order.risk_json)["paper_deferred_order"]
    assert after["buy_validity"] == before and after["last_evaluated_round_id"] == "fill"
    assert broker.place_order.await_count == 0


@pytest.mark.asyncio
async def test_t_missing_min5_wait_watermark_and_regression(memory_session, monkeypatch):
    order, _, _, broker, ctx = await setup(memory_session, monkeypatch)
    frozen = json.loads(order.risk_json)["paper_deferred_order"]["buy_validity"]
    ctx["min5_change"] = None
    result = await reconcile(memory_session, monkeypatch, order)
    assert result[0]["event"] == "waiting"
    ctx["min5_change"] = .1
    assert await reconcile(memory_session, monkeypatch, order) == []
    result = await reconcile(memory_session, monkeypatch, order,
        at=AT+timedelta(seconds=29), round_id="regressed")
    assert result[0]["event"] == "canceled" and "buy_clock_regressed" in result[0]["reason"]
    assert broker.place_order.await_count == 0
    assert json.loads(order.risk_json)["paper_deferred_order"]["buy_validity"] == frozen


@pytest.mark.asyncio
async def test_t_partial_then_expired_keeps_existing_fill(memory_session, monkeypatch):
    order, pos, sale, broker, ctx = await setup(memory_session, monkeypatch, quantity=300)
    result = await reconcile(memory_session, monkeypatch, order, accepted=True, ask1_volume=1)
    assert result[0]["event"] == "partial" and order.filled_quantity == 100
    before = await memory_session.scalar(select(TradeFill))
    identity = (before.quantity, before.price, before.commission, before.filled_at)
    expiry = json.loads(order.risk_json)["paper_deferred_order"]["buy_validity"]["expires_at"]
    result = await reconcile(memory_session, monkeypatch, order,
        at=datetime.fromisoformat(expiry)+timedelta(seconds=1), round_id="expiry")
    assert result[0]["event"] == "canceled" and order.filled_quantity == 100
    assert identity == (before.quantity, before.price, before.commission, before.filled_at)
    assert broker.place_order.await_count == 1


@pytest.mark.asyncio
async def test_t_partial_position_change_cancels_remainder_without_remint(memory_session, monkeypatch):
    order, pos, sale, broker, ctx = await setup(memory_session, monkeypatch, quantity=300)
    result = await reconcile(memory_session, monkeypatch, order, accepted=True, ask1_volume=1)
    assert result[0]["event"] == "partial"
    # Fixture broker intentionally has no ledger effects: explicitly model its position update.
    pos.buy_amount += 100
    await memory_session.commit()
    result = await reconcile(memory_session, monkeypatch, order,
        at=AT+timedelta(seconds=40), round_id="next")
    assert result[0]["event"] == "canceled" and "持仓状态已改变" in result[0]["reason"]
    assert order.filled_quantity == 100 and broker.place_order.await_count == 1


@pytest.mark.asyncio
async def test_t_expire_only_needs_no_quote_or_healthy_round(memory_session, monkeypatch):
    order, _, _, broker, _ = await setup(memory_session, monkeypatch)
    quote_mock = AsyncMock(side_effect=AssertionError("housekeeping must not request quotes"))
    monkeypatch.setattr(service, "_paper_execution_spot", quote_mock)
    expiry = json.loads(order.risk_json)["paper_deferred_order"]["buy_validity"]["expires_at"]
    result = await service.reconcile_paper_deferred_orders(memory_session,
        account_id=order.account_id, now=datetime.fromisoformat(expiry)+timedelta(seconds=1),
        expire_only=True)
    assert result[0]["event"] == "canceled" and broker.place_order.await_count == 0
    assert not quote_mock.called


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing", "source", "position", "sell", "future"])
async def test_t_submission_refuses_unverifiable_original_confirmation(memory_session, monkeypatch, mutation):
    order, _, _, broker, _ = await setup(memory_session, monkeypatch)
    meta = json.loads(order.risk_json)["paper_deferred_order"]
    if mutation == "missing":
        meta["candidate"].pop("t_buyback")
    elif mutation == "source":
        meta["candidate"]["_source"] = "c_recent_limit_relaunch"
    elif mutation == "position":
        meta["candidate"]["t_buyback"]["position_id"] = -1
    elif mutation == "sell":
        meta["candidate"]["t_buyback"]["sell_amount"] += 100
    else:
        meta["confirmed_at"] = (AT+timedelta(seconds=50)).isoformat()
    cmd = service.SubmitOrderCommand(code=order.code, side="buy", price=order.price,
        quantity=order.quantity, account_id=order.account_id, strategy_id="paper-auto-t",
        strategy_version=order.strategy_version, signal_id=order.signal_id, source="position-t")
    await service._freeze_pending_buy_validity(memory_session, cmd, meta, decision_at=AT+timedelta(seconds=5))
    assert meta["buy_validity"]["status"] == "invalid"
    assert service._pending_buy_time_reason(order, meta, now=AT+timedelta(seconds=30))
    assert broker.place_order.await_count == 0


@pytest.mark.asyncio
async def test_t_same_name_archived_account_never_shadows_frozen_active(memory_session, monkeypatch):
    order, pos, _, broker, _ = await setup(memory_session, monkeypatch, archived=True, from_loop=True)
    assert pos.account_id == 2
    contract = json.loads(order.risk_json)["paper_deferred_order"]["buy_validity"]
    assert contract["status"] == "valid" and contract["t_buyback"]["account_id"] == 2
    result = await reconcile(memory_session, monkeypatch, order, accepted=True)
    assert result[0]["event"] == "filled" and broker.place_order.await_count == 1


@pytest.mark.asyncio
async def test_t_frozen_account_closed_never_migrates_to_same_name_active(memory_session, monkeypatch):
    order, pos, _, broker, _ = await setup(memory_session, monkeypatch)
    account = await memory_session.get(PaperAccount, pos.account_id)
    account.status = "closed"
    memory_session.add(PaperAccount(account_name="default", initial_capital=100000., status="active"))
    await memory_session.commit()
    result = await reconcile(memory_session, monkeypatch, order)
    assert result[0]["event"] == "canceled" and "账户身份失效" in result[0]["reason"]
    assert broker.place_order.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("future", ["sell", "buy", "count"])
async def test_t_future_rows_do_not_pollute_generator_freeze_or_fill(memory_session, monkeypatch, future):
    order, pos, _, broker, _ = await setup(memory_session, monkeypatch, future=future, from_loop=True)
    contract = json.loads(order.risk_json)["paper_deferred_order"]["buy_validity"]
    assert contract["status"] == "valid"
    assert contract["t_buyback"]["sell_amount"] == 300 and order.quantity == 300
    assert contract["t_buyback"]["sell_avg_price"] == 10.5
    assert contract["t_buyback"]["buyback_count"] == 0
    if future == "count":
        assert await paper._today_t_buyback_count(memory_session, pos.account_id, pos.code, AT.date()) == 1
        assert await paper._today_t_buyback_count(memory_session, pos.account_id, pos.code, AT.date(), as_of=AT) == 0
    else:
        # Backward-compatible no-as_of calls still aggregate the full day.
        day = await paper._today_sell_stats(memory_session, pos.account_id, pos.code, AT.date())
        assert day["amount"] == (1200 if future == "sell" else 0)
    result = await reconcile(memory_session, monkeypatch, order, accepted=True)
    if future == "buy":
        # As-of signal inputs above remain unchanged. Executing against a current
        # book containing an unpaired future-dated buy is a separate integrity
        # failure, not permission to consume future information or ignore a debit.
        assert result[0]["event"] == "risk_blocked" and broker.place_order.await_count == 0
        proof = json.loads(order.risk_json)["paper_account_execution_integrity"]
        assert proof["status"] == "blocked" and proof["reason_code"] == "ledger_without_unique_receipt"
    else:
        assert result[0]["event"] == "filled" and broker.place_order.await_count == 1


@pytest.mark.asyncio
async def test_t_only_future_sell_is_not_available_and_cannot_freeze(memory_session, monkeypatch):
    order, pos, sale, broker, _ = await setup(memory_session, monkeypatch)
    sale.trade_time = AT+timedelta(minutes=5)
    await memory_session.commit()
    stats = await paper._today_sell_stats(memory_session, pos.account_id, pos.code, AT.date(), as_of=AT)
    assert stats == {"amount": 0, "avg_price": None, "first_time": None}
    meta = json.loads(order.risk_json)["paper_deferred_order"]
    cmd = service.SubmitOrderCommand(code=order.code, side="buy", price=order.price,
        quantity=order.quantity, account_id=order.account_id, strategy_id="paper-auto-t",
        strategy_version=order.strategy_version, signal_id=order.signal_id, source="position-t")
    await service._freeze_pending_buy_validity(memory_session, cmd, meta, decision_at=AT)
    assert meta["buy_validity"]["status"] == "invalid"
    result = await reconcile(memory_session, monkeypatch, order)
    assert result[0]["event"] == "canceled" and broker.place_order.await_count == 0


def test_t_sells_and_manual_remain_exempt():
    for strategy, side, expected in [("paper-auto-t", "buy", True),
            ("paper-auto-t", "sell", False), ("manual", "buy", False)]:
        assert service._requires_pending_buy_validity(SimpleNamespace(
            broker="paper", side=side, strategy_id=strategy)) is expected
