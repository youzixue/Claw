"""P0 pending buy regression: isolated DB, real contract/reconcile, no live orders."""
import copy
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.config.settings import settings
from app.models.paper import PaperShadowEvent
from app.models.trading import TradeFill, TradeOrder
from app.paper import strategy_iteration_challenger as challenger
from app.trading import service
from test_paper_deferred_exit_provenance import memory_session
from paper_pending_fixture import accepted_frame

AT = datetime(2026, 9, 10, 9, 36, 49)
ROUTE = "c_recent_limit_relaunch"
ACCOUNT = "challenger_c"


def quote(at, **kwargs):
    return SimpleNamespace(
        **{**dict(code="002988", name="隔离豪美情景", price=30.82, prev_close=30.,
                  avg_price=30.50, high=30.9, low=30., open=30., limit_up=33.,
                  volume_ratio=2., orderbook_imbalance=.2, ask1_price=30.82,
                  ask1_volume=10, updated_at=at, source_quote_at=at, received_at=at,
                  quote_round_id="fill", change_pct=2.7333), **kwargs})


async def setup_order(db, monkeypatch, *, quantity=100, side="buy", account=ACCOUNT):
    monkeypatch.setattr(service, "experiment_active", lambda *_a, **_kw: False)
    risk = AsyncMock(return_value={"final_level":"pass", "block_reasons":[], "warnings":[]})
    broker = AsyncMock()
    async def fill(_db, request):
        return SimpleNamespace(accepted=True, external_order_id="fixture", fills=[
            SimpleNamespace(fill_id=f"fill-{request.order_id}", price=request.price,
                            quantity=request.quantity, commission=5., tax=0., realized_pnl=0.,
                            broker_trade_id="1", filled_at=request.filled_at, raw={})])
    broker.place_order.side_effect = fill
    monkeypatch.setattr(service, "_pre_trade_risk_check", risk)
    monkeypatch.setattr(service, "get_broker_adapter", lambda _: broker)
    monkeypatch.setattr(settings, "PAPER_DEPTH_MAX_PARTICIPATION_RATIO", 1.)
    event = PaperShadowEvent(event_key="regression-c2-confirmed", route_id=ROUTE,
        route_version=challenger._route_shadow_version(ROUTE), trade_date=AT.date(),
        observed_at=AT, created_at=AT, code="002988", name="fixture",
        event_type="confirmed", status="confirmed", price=30.82, snapshot_json=json.dumps({
            "quote":{"price":30.82, "avg_price":30.5}, "prior_structure":{},
            "rule_snapshot":{}, "state":{}}))
    db.add(event)
    await db.commit()
    frozen = challenger._event_candidate(event, json.loads(event.snapshot_json))
    cmd = service.SubmitOrderCommand(code="002988", side=side, price=30.88,
        quantity=quantity, account_id=account, strategy_id="paper-challenger-forward",
        strategy_version=paper._strategy_version(account),
        source=ROUTE, signal_id=challenger._signal_token(event.event_key, ROUTE),
        decision_at=AT+timedelta(seconds=28), as_of_at=AT, decision_round_id="decision",
        defer_until_next_round=True, deferred_metadata={"candidate":frozen, "block_warn":True})
    result = await service.submit_order(db, cmd)
    assert result["order"]["status"] == "submitted"
    order = await db.scalar(select(TradeOrder))
    return order, event, risk, broker


async def reconcile(db, monkeypatch, *, at, spot=None, quality="ok", round_id="fill", qualified=False):
    if spot is None:
        spot = quote(at, quote_round_id=round_id)
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(return_value=spot))
    payload = {"round_id":round_id, "quality_status":quality,
        "committed_at":at, "as_of_at":at}
    if qualified:
        payload.update(config_version="validity-fixture", code_version="validity-fixture",
                       records=[vars(spot)])
        await accepted_frame(db, payload)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: at)
    token = paper._QUOTE_ROUND_CONTEXT.set(payload)
    try:
        return await service.reconcile_paper_deferred_orders(
            db, account_id=ACCOUNT, now=at, round_id=round_id)
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_haomei_29_minute_old_confirmation_expires_even_with_healthy_depth(memory_session, monkeypatch):
    db = memory_session
    order, event, risk, broker = await setup_order(db, monkeypatch)
    frozen = json.loads(order.risk_json)["paper_deferred_order"]
    assert frozen["buy_validity"]["confirmed_at"] == AT.isoformat()
    at = datetime(2026,9,10,10,5,47)
    result = await reconcile(db, monkeypatch, at=at, spot=quote(at, price=30.80, avg_price=30.98))
    assert result[0]["event"] == "canceled"
    assert "buy_signal_expired" in result[0]["reason"]
    assert order.filled_quantity == 0 and order.price == 30.88
    assert broker.place_order.await_count == 0 and risk.await_count == 1
    assert json.loads(order.risk_json)["paper_deferred_order"]["candidate"] == frozen["candidate"]
    assert list((await db.scalars(select(TradeFill))).all()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("quality,round_id", [("bad","fill"), ("missing",""), ("ok","decision")])
async def test_timeout_without_new_healthy_round_releases_pending_capacity(memory_session, monkeypatch, quality, round_id):
    db = memory_session
    order, event, risk, broker = await setup_order(db, monkeypatch)
    at = AT+timedelta(seconds=721)
    result = await reconcile(db, monkeypatch, at=at, quality=quality, round_id=round_id)
    assert result[0]["event"] == "canceled"
    assert list((await db.scalars(select(TradeOrder).where(
        TradeOrder.account_id==ACCOUNT, TradeOrder.status.in_(("pending","submitted","partial"))))).all()) == []
    # Crash between order cancellation and audit log must not resurrect the event.
    assert await challenger._already_processed(db, account_id=9, account_name=ACCOUNT,
        run_id=challenger._run_id(event.event_key), signal_token=order.signal_id, quote_round_id="later")
    assert broker.place_order.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fields,expected", [
    ({"price":30.40, "avg_price":30.5}, "canceled"),
    ({"high":32.}, "canceled"),
    ({"volume_ratio":.01}, "canceled"),
    ({"orderbook_imbalance":-.9}, "canceled"),
    ({"prev_close":29.}, "canceled"),
    ({"avg_price":None}, "waiting"),
    ({"ask1_volume":0}, "waiting"),
    ({"orderbook_imbalance":None}, "waiting"),
])
async def test_rechecks_route_before_depth_or_common_risk(memory_session, monkeypatch, fields, expected):
    db = memory_session
    order, _, risk, broker = await setup_order(db, monkeypatch)
    at = AT+timedelta(seconds=60)
    result = await reconcile(db, monkeypatch, at=at, spot=quote(at, **fields))
    assert result[0]["event"] == expected
    assert broker.place_order.await_count == 0 and risk.await_count == 1
    assert order.filled_quantity == 0


@pytest.mark.asyncio
async def test_partial_then_expired_keeps_fill_and_cancels_only_remainder(memory_session, monkeypatch):
    db = memory_session
    order, _, risk, broker = await setup_order(db, monkeypatch, quantity=300)
    at = AT+timedelta(seconds=60)
    result = await reconcile(db, monkeypatch, at=at, spot=quote(at, ask1_volume=1), qualified=True)
    assert result[0]["event"] == "partial" and order.filled_quantity == 100
    assert await reconcile(db, monkeypatch, at=at, spot=quote(at, ask1_volume=10)) == []
    frozen_fill = await db.scalar(select(TradeFill))
    before = (frozen_fill.price, frozen_fill.quantity, frozen_fill.filled_at, frozen_fill.commission)
    final = await reconcile(db, monkeypatch, at=AT+timedelta(seconds=721), round_id="later")
    assert final[0]["event"] == "canceled" and order.filled_quantity == 100
    assert (frozen_fill.price, frozen_fill.quantity, frozen_fill.filled_at, frozen_fill.commission) == before
    assert len(list((await db.scalars(select(TradeFill))).all())) == 1
    assert broker.place_order.await_count == 1


@pytest.mark.asyncio
async def test_limit_without_depth_waits_but_does_not_extend_original_expiry(memory_session, monkeypatch):
    db = memory_session
    order, _, _, broker = await setup_order(db, monkeypatch)
    at = AT+timedelta(seconds=60)
    initial = json.loads(order.risk_json)["paper_deferred_order"]["buy_validity"]
    response = await reconcile(db, monkeypatch, at=at, spot=quote(at, ask1_price=31.))
    assert response[0]["event"] == "waiting" and "原限价" in response[0]["reason"]
    after = json.loads(order.risk_json)["paper_deferred_order"]["buy_validity"]
    assert after == initial
    final = await reconcile(db, monkeypatch, at=AT+timedelta(seconds=721), round_id="later")
    assert final[0]["event"] == "canceled" and broker.place_order.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["legacy", "event_version", "event_snapshot", "event_removed", "clock_future"])
async def test_missing_or_changed_original_evidence_never_fills(memory_session, monkeypatch, mutation):
    db = memory_session
    order, event, _, broker = await setup_order(db, monkeypatch)
    if mutation == "legacy":
        data = json.loads(order.risk_json)
        data["paper_deferred_order"].pop("buy_validity")
        order.risk_json = json.dumps(data)
    elif mutation == "event_version":
        event.route_version = "old-semantics"
    elif mutation == "event_snapshot":
        event.snapshot_json = "{}"
    elif mutation == "event_removed":
        await db.delete(event)
    else:
        event.observed_at = AT+timedelta(hours=1)
    await db.commit()
    response = await reconcile(db, monkeypatch, at=AT+timedelta(seconds=60))
    assert response[0]["event"] == "canceled" and broker.place_order.await_count == 0


@pytest.mark.asyncio
async def test_sell_never_requires_buy_contract(memory_session, monkeypatch):
    db = memory_session
    order, _, _, _ = await setup_order(db, monkeypatch, side="sell")
    metadata = json.loads(order.risk_json)["paper_deferred_order"]
    assert "buy_validity" not in metadata
    assert service._pending_buy_time_reason(order, metadata, now=AT+timedelta(hours=3)) == ""
    assert await service._pending_buy_current_status(db, order, metadata, None, now=AT) == ("valid","")


@pytest.mark.asyncio
async def test_primary_rechecks_vwap_and_fails_closed_on_missing_quote(memory_session):
    candidate = {"code":"002988", "_source":"next_day_plan"}
    args = dict(account_name="default", source="next_day_plan",
        candidate=candidate, limit_price=30.88, now=AT)
    result = await paper._pending_primary_buy_confirmation(
        memory_session, spot=quote(AT, price=30.40, avg_price=30.5), **args)
    assert result[0] == "canceled" and "VWAP" in result[1]
    result = await paper._pending_primary_buy_confirmation(
        memory_session, spot=quote(AT, avg_price=None), **args)
    assert result[0] == "waiting"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [
    "fill", "vwap_invalid", "expired", "quote_missing", "legacy",
    "no_round", "bad_round", "same_round", "wait_then_regress", "missing_then_regress",
])
async def test_e2_queue_reuses_real_highboard_predicate_and_never_assumes_ask_fill(
    memory_session, monkeypatch, mode,
):
    from app.models.stock import LimitUpPool
    db = memory_session
    at = AT+timedelta(minutes=30)
    prior = AT.date()-timedelta(days=1)
    monkeypatch.setattr(settings, "PAPER_TENBAGGER_ENABLED", True)
    monkeypatch.setattr(service, "experiment_active", lambda *_a, **_kw: False)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=prior))
    risk = AsyncMock(return_value={"final_level":"pass", "block_reasons":[], "warnings":[]})
    broker = AsyncMock()
    broker.place_order.return_value = SimpleNamespace(
        accepted=True, external_order_id="queue-fill", status="filled", error_message="", fills=[SimpleNamespace(fill_id="queue-fill",
            price=11., quantity=100, commission=5., tax=0., realized_pnl=0.,
            broker_trade_id="1", filled_at=at+timedelta(seconds=30), raw={})])
    monkeypatch.setattr(service, "_pre_trade_risk_check", risk)
    monkeypatch.setattr(service, "get_broker_adapter", lambda _: broker)
    spot = quote(at, code="600001", price=11., prev_close=10., high=11.,
        low=10.4, open=10.5, avg_price=10.7, change_pct=10., limit_up=11.,
        bid1_price=11., bid1_volume=100, ask1_price=0., ask1_volume=0., volume=50000,
        quote_round_id="queue-decision")
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(side_effect=lambda *_: spot))
    monkeypatch.setattr(paper, "_spot_by_code", AsyncMock(side_effect=lambda *_: spot))
    db.add(LimitUpPool(code="600001", trade_date=prior, consecutive_days=4,
        seal_amount=200_000_000., break_count=0, quarantined=False))
    await db.commit()
    candidate = {"code":"600001", "_source":"tenbagger_midline", "signal_date":prior.isoformat()}
    cmd = service.SubmitOrderCommand(code="600001", side="buy", quantity=100, price=11.,
        account_id="challenger_e", strategy_id="paper-auto-short", source="tenbagger_midline",
        strategy_version=paper._strategy_version("challenger_e"), signal_id="auto-queue-test",
        decision_at=at, as_of_at=at, decision_round_id="queue-decision", queue_if_limit_up=True,
        queue_metadata={"candidate":candidate, "confirmed_at":at.isoformat(), "cancel_time":"14:50"})
    initial = await service.submit_order(db, cmd)
    assert initial["order"]["status"] == "submitted"
    order = await db.scalar(select(TradeOrder))
    if mode == "legacy":
        meta = json.loads(order.risk_json)
        meta["paper_limit_up_queue"].pop("buy_validity")
        order.risk_json = json.dumps(meta)
        await db.commit()
    now = at+timedelta(seconds=721 if mode=="expired" else 30)
    spot.volume = 50101  # 100 ahead + 1 own lot, even with zero visible ask.
    spot.quote_round_id = "queue-fill"
    spot.updated_at = spot.received_at = spot.source_quote_at = now
    if mode == "vwap_invalid":
        spot.price, spot.avg_price = 10.6, 10.7
    elif mode in {"quote_missing", "missing_then_regress"}:
        spot.source_quote_at = at-timedelta(minutes=5)
    elif mode == "wait_then_regress":
        spot.volume = 50000
    payload = {
        "round_id":("" if mode=="no_round" else "queue-decision" if mode=="same_round" else "queue-fill"),
        "quality_status":"bad" if mode=="bad_round" else "ok",
        "as_of_at":now, "committed_at":now}
    if mode == "fill":
        payload.update(config_version="validity-queue", code_version="validity-queue", records=[vars(spot)])
        await accepted_frame(db, payload)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: now)
    token = paper._QUOTE_ROUND_CONTEXT.set(payload)
    try:
        result = await service.reconcile_paper_limit_up_orders(
            db, account_id="challenger_e", now=now)
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)
    if mode in {"no_round", "bad_round", "same_round"}:
        assert result == [] and order.status == "submitted"
    else:
        expected = ("filled" if mode=="fill" else "waiting"
                    if mode in {"quote_missing", "wait_then_regress", "missing_then_regress"} else "canceled")
        assert result[0]["event"] == expected, result
    if mode in {"wait_then_regress", "missing_then_regress"}:
        saved = json.loads(order.risk_json)["paper_limit_up_queue"]
        assert saved["buy_validity_evaluation"]["evaluated_at"] == now.isoformat()
        assert saved["last_evaluated_round_id"] == "queue-fill"
        # Healthy same-round repeat cannot consume newly appearing depth/volume.
        spot.volume = 50101
        spot.source_quote_at = now
        token = paper._QUOTE_ROUND_CONTEXT.set({"round_id":"queue-fill", "quality_status":"ok"})
        try:
            assert await service.reconcile_paper_limit_up_orders(
                db, account_id="challenger_e", now=now) == []
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        earlier = now-timedelta(seconds=1)
        spot.source_quote_at = spot.received_at = spot.updated_at = earlier
        spot.quote_round_id = "out-of-order"
        token = paper._QUOTE_ROUND_CONTEXT.set({"round_id":"out-of-order", "quality_status":"ok"})
        try:
            older = await service.reconcile_paper_limit_up_orders(
                db, account_id="challenger_e", now=earlier)
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        assert older[0]["event"] == "canceled" and "buy_clock_regressed" in older[0]["reason"]
    assert broker.place_order.await_count == int(mode=="fill")
    assert risk.await_count == 1+2*int(mode=="fill")  # accepted fill also rechecks under lock
    assert json.loads(order.risk_json)["paper_limit_up_queue"]["candidate"] == candidate


@pytest.mark.asyncio
async def test_no_quote_expiry_housekeeping_never_fills_or_touches_sell(memory_session, monkeypatch):
    from app.models.paper import PaperAccount, PaperAutoTradeLog
    db = memory_session
    order, _, _, broker = await setup_order(db, monkeypatch)
    db.add(PaperAccount(account_name=ACCOUNT, initial_capital=100000,
        current_capital=100000, total_assets=100000))
    sell = TradeOrder(order_id="existing-sell", broker="paper", account_id=ACCOUNT,
        code=order.code, side="sell", order_type="limit", quantity=100, price=30., status="submitted",
        strategy_id="paper-auto-short", strategy_version="old-exit-version", source="position",
        decision_at=AT, trade_date=AT.date()-timedelta(days=1),
        risk_json=json.dumps({"paper_deferred_order":{"candidate":{"exit_trigger_reason":"止损"}}}))
    db.add(sell)
    await db.commit()
    frozen_sell = sell.risk_json
    monkeypatch.setattr(service, "_paper_execution_spot", AsyncMock(side_effect=AssertionError("quote must not be read")))
    assert await paper.expire_pending_paper_buys(db, account_name=ACCOUNT, now=AT+timedelta(seconds=60)) == 0
    assert order.status == "submitted" and sell.status == "submitted"
    assert await paper.expire_pending_paper_buys(db, account_name=ACCOUNT, now=AT+timedelta(seconds=721)) == 1
    assert order.status == "canceled" and sell.status == "submitted"
    assert sell.risk_json == frozen_sell and broker.place_order.await_count == 0
    rows = list((await db.scalars(select(PaperAutoTradeLog))).all())
    assert len(rows) == 1 and rows[0].reason_code == "pending_buy_expiry_guard"


@pytest.mark.asyncio
async def test_watchdog_without_quotes_dispatches_expiry_only(monkeypatch):
    from app.data.scheduler import DataScheduler
    scheduler = DataScheduler()
    monkeypatch.setattr(paper.trade_calendar, "get_trade_session", lambda: "morning")
    monkeypatch.setattr(scheduler, "_latest_healthy_quote_payload", AsyncMock(return_value=None))
    expiry = AsyncMock()
    risk = AsyncMock(side_effect=AssertionError("no quote must not dispatch position fills"))
    entry = AsyncMock(side_effect=AssertionError("no quote must not dispatch entries"))
    monkeypatch.setattr(scheduler, "_expire_pending_paper_buys", expiry)
    monkeypatch.setattr(scheduler, "_run_quote_round_position_risk", risk)
    monkeypatch.setattr(scheduler, "_run_paper_accounts_isolated", entry)
    await scheduler._paper_intraday_auto_trade()
    assert expiry.await_count == 1 and risk.await_count == entry.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_deferred_wait_persists_clock_and_blocks_older_round(memory_session, monkeypatch, missing):
    db = memory_session
    order, _, _, broker = await setup_order(db, monkeypatch)
    now = AT+timedelta(seconds=90)
    spot = quote(now, ask1_price=31.)
    if missing:
        spot.source_quote_at = AT-timedelta(minutes=5)
    result = await reconcile(db, monkeypatch, at=now, spot=spot)
    assert result[0]["event"] == "waiting"
    saved = json.loads(order.risk_json)["paper_deferred_order"]
    assert saved["buy_validity_evaluation"]["evaluated_at"] == now.isoformat()
    assert await reconcile(db, monkeypatch, at=now) == []
    result = await reconcile(db, monkeypatch, at=now-timedelta(seconds=1), round_id="earlier")
    assert result[0]["event"] == "canceled" and "buy_clock_regressed" in result[0]["reason"]
    assert broker.place_order.await_count == 0


def test_manual_and_nonpaper_orders_are_not_changed():
    for broker, strategy, side in [("paper","","buy"), ("paper","paper-auto-short","sell"),
                                   ("live","external","buy")]:
        order = SimpleNamespace(broker=broker, strategy_id=strategy, side=side)
        assert not service._requires_pending_buy_validity(order)
