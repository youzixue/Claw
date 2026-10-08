"""C3 notification isolation: temporary SQLite and fake channel, no runtime DB/API."""
import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config.settings import settings
from app.models.paper import PaperShadowEvent, PaperAutoTradeLog, PaperAccount, PaperTradeLog
from app.models.stock import StockTag, QuoteRound
from app.models.trading import TradeOrder
from app.paper import strategy_iteration_shadow as shadow
from app.push import paper_buy_points as points
from app.push.channels.base import PushMessage
from app.push.channels.feishu import feishu_category_allowed


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'c3.db'}")
    async with engine.begin() as conn:
        for model in (PaperShadowEvent, PaperAutoTradeLog, PaperAccount, PaperTradeLog,
                      StockTag, QuoteRound, TradeOrder):
            await conn.run_sync(model.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    clock = [datetime(2026, 9, 22, 10, 1)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]
    monkeypatch.setattr(points, "datetime", Clock)
    for k, v in {"C3_RESEARCH_PUSH_ENABLED": True, "PUSH_ENABLED": True,
                 "PAPER_BUY_POINT_PUSH_MAX_AGE_SEC": 180,
                 "PAPER_BUY_POINT_PUSH_RETRY_SEC": 15,
                 "PUSH_STOCK_COOLDOWN": 300, "PUSH_HOURLY_LIMIT": 30,
                 "PAPER_STRATEGY_ITERATION_CONFIRM_MIN_SAMPLES": 3,
                 "PAPER_STRATEGY_ITERATION_CONFIRM_MIN_PERSISTENCE_SEC": 60,
                 "PAPER_STRATEGY_ITERATION_CONFIRM_MAX_SAMPLE_GAP_SEC": 75}.items():
        monkeypatch.setattr(settings, k, v)
    async def fake_send(message, channels, **kwargs):
        # A separate writer can commit while transport is in progress.
        async with maker() as db:
            db.add(PaperAutoTradeLog(run_id="fake-network", trade_date=clock[0].date(),
                created_at=clock[0], action="test", decision="test"))
            await db.commit()
        return {"channels": {"feishu": True}, "status": "sent"}
    send = AsyncMock(side_effect=fake_send)
    monkeypatch.setattr(points.push_scheduler, "push_to_channels", send)
    monkeypatch.setattr(points, "_c3_lock", asyncio.Lock())
    yield maker, clock, send
    await engine.dispose()


async def seed(maker, at, *, defect=None):
    version = shadow.route_version_for(shadow.ROUTE_C3)
    quote = {"code": "600001", "price": 10.5, "source_quote_at": at.isoformat()}
    data = shadow._event(route_id=shadow.ROUTE_C3, trade_date=at.date(), observed_at=at,
        code="600001", name="隔离", event_type="confirmed", status="confirmed",
        quote=quote, prior={})
    data["created_at"] = at
    if defect == "old_version":
        data["route_version"] = "old"
    snapshot = json.loads(data["snapshot_json"])
    if defect == "missing_clock":
        snapshot["quote"].pop("source_quote_at")
    if defect == "future_source":
        snapshot["quote"]["source_quote_at"] = (at + timedelta(seconds=1)).isoformat()
    if defect == "cross_day":
        snapshot["quote"]["source_quote_at"] = (at - timedelta(days=1)).isoformat()
    if defect == "connected":
        snapshot["rule_snapshot"]["real_order_connected"] = True
    data["snapshot_json"] = json.dumps(snapshot)
    async with maker() as db:
        event = PaperShadowEvent(**data)
        db.add(event)
        db.add(StockTag(code="600001", name="隔离", board_type="main_sh", board_tag="tradeable",
                        is_st=defect == "st", is_suspended=False, is_delisting=False, is_ipo_recent=False))
        if defect != "no_frames":
            for i in (-60, -30, 0):
                stamp = at + timedelta(seconds=i)
                rid = f"q{i}"
                db.add(QuoteRound(round_id=rid, trade_date=at.date(), as_of_at=stamp,
                    committed_at=stamp, expected_count=1, received_count=1,
                    quality_status="degraded" if defect == "quality" else "ok",
                    config_version="test", code_version="test"))
                db.add(PaperShadowEvent(event_key=f"frame{i}", route_id=shadow.ROUTE_C3,
                    route_version=version, trade_date=at.date(), observed_at=stamp, created_at=stamp,
                    code="MARKET", event_type="universe_audit", status="complete",
                    snapshot_json=json.dumps({"prior_structure": {"quote_coverage": 1,
                        "current_pool": {"read_model_version": "c3_current_pool_v1",
                            "quote_round_ids": [rid], "members": [{"code": "600001", "eligible": True,
                            "confirmation_frame": True, "source_quote_at": stamp.isoformat()}]}}})))
        await db.commit()
        return event.event_key, data["snapshot_json"]


async def receipt(maker, key, now):
    async with maker() as db:
        return (await points.c3_research_notification_receipts(db, event_keys=[key], now=now)).get(key)


@pytest.mark.asyncio
async def test_success_repeat_restart_no_orders_and_no_shadow_mutation(env, monkeypatch):
    maker, clock, send = env
    key, original = await seed(maker, clock[0])
    assert (await points.dispatch_c3_research(now=clock[0], session_factory=maker))["status"] == "sent"
    r = await receipt(maker, key, clock[0])
    assert r["status"] == "sent" and r["signal_observed_at"] == clock[0].isoformat()
    assert r["research_only"] and r["execution_connected"] is False and r["user_received_at"] is None
    monkeypatch.setattr(points, "_c3_lock", asyncio.Lock())
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert send.call_count == 1
    message = send.call_args.args[0]
    assert message.extra["display_title"] == "C3研究观察信号·未下单"
    assert "原始确认时间" in message.content and "通知当前复验时点" in message.content
    async with maker() as db:
        for model in (PaperAccount, PaperTradeLog, TradeOrder):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
        event = await db.scalar(select(PaperShadowEvent).where(PaperShadowEvent.event_key == key))
        assert event.snapshot_json == original and event.status == "confirmed"
        rows = (await db.scalars(select(PaperAutoTradeLog).where(PaperAutoTradeLog.source == points.C3_ROUTE))).all()
        assert {r.action for r in rows} == {"research_signal", "research_push"}
        assert all(r.account_id is None and r.executed_trade_id is None for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["missing_clock", "future_source", "cross_day", "connected",
                                    "st", "no_frames", "quality"])
async def test_fail_closed(env, defect):
    maker, clock, send = env
    key, _ = await seed(maker, clock[0], defect=defect)
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert not send.called
    assert (await receipt(maker, key, clock[0]))["status"] == (
        "waiting" if defect in {"no_frames", "quality"} else "rejected")


@pytest.mark.asyncio
@pytest.mark.parametrize("delta,defect", [(-181, None), (-86400, None), (1, None), (0, "old_version")])
async def test_historical_future_and_other_version_never_ingress(env, delta, defect):
    maker, clock, send = env
    key, _ = await seed(maker, clock[0] + timedelta(seconds=delta), defect=defect)
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert not send.called
    assert await receipt(maker, key, clock[0]) is None


@pytest.mark.asyncio
async def test_retry_lease_restart_then_expiry(env, monkeypatch):
    maker, clock, send = env
    key, _ = await seed(maker, clock[0])
    send.side_effect = RuntimeError("fake failure")
    assert (await points.dispatch_c3_research(now=clock[0], session_factory=maker))["status"] == "failed"
    clock[0] += timedelta(seconds=5)
    monkeypatch.setattr(points, "_c3_lock", asyncio.Lock())
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert send.call_count == 1
    clock[0] += timedelta(seconds=11)
    send.side_effect = None
    send.return_value = {"channels": {"feishu": True}}
    assert (await points.dispatch_c3_research(now=clock[0], session_factory=maker))["status"] == "sent"
    assert (await receipt(maker, key, clock[0]))["signal_observed_at"] != clock[0].isoformat()


@pytest.mark.asyncio
async def test_failed_queue_expires_without_refresh(env):
    maker, clock, send = env
    key, _ = await seed(maker, clock[0])
    send.side_effect = RuntimeError("fake failure")
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    clock[0] += timedelta(seconds=181)
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert send.call_count == 1
    assert (await receipt(maker, key, clock[0]))["status"] == "expired"



@pytest.mark.asyncio
@pytest.mark.parametrize("decision,age,expected", [
    ("attempting", 10, "attempting"), ("attempting", 31, "sent"),
    ("sent", 10, "throttled"),
])
async def test_durable_lease_and_cooldown(env, monkeypatch, decision, age, expected):
    maker, clock, send = env
    key, _ = await seed(maker, clock[0])
    # Freeze a genuine queue entry and simulate an interrupted transport, or a
    # recent prior same-stock receipt from another event/version.
    original_check = points._c3_current_check
    async def interrupt(*args, **kwargs):
        raise RuntimeError("isolated stop after ingress")
    monkeypatch.setattr(points, "_c3_current_check", interrupt)
    with pytest.raises(RuntimeError):
        await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    monkeypatch.setattr(points, "_c3_current_check", original_check)
    async with maker() as db:
        row = await db.scalar(select(PaperAutoTradeLog).where(PaperAutoTradeLog.action == "research_signal"))
        item = points._item(row)
        if decision == "sent":
            item["run_id"] = "other-prior-c3-event"
        await points._audit(db, item, decision, clock[0] - timedelta(seconds=age))
        await db.commit()
    monkeypatch.setattr(points, "_c3_lock", asyncio.Lock())
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert (await receipt(maker, key, clock[0]))["status"] == expected
    assert send.call_count == (1 if expected == "sent" else 0)


@pytest.mark.asyncio
async def test_original_queue_never_invokes_research_consumer(env, monkeypatch):
    maker, clock, send = env
    order = []
    async def original(**kwargs):
        order.append("original")
        return {"status": "sent", "count": 12}
    async def research(**kwargs):
        order.append("research")
        raise RuntimeError("isolated research failure")
    monkeypatch.setattr(points, "_dispatch", original)
    monkeypatch.setattr(points, "dispatch_c3_research", research)
    assert await points.dispatch_buy_points(now=clock[0], session_factory=maker) == {"status": "sent", "count": 12}
    assert order == ["original"]


@pytest.mark.asyncio
async def test_disabled_does_not_ingress(env, monkeypatch):
    maker, clock, send = env
    key, _ = await seed(maker, clock[0])
    monkeypatch.setattr(settings, "C3_RESEARCH_PUSH_ENABLED", False)
    assert (await points.dispatch_c3_research(now=clock[0], session_factory=maker))["status"] == "disabled"
    assert await receipt(maker, key, clock[0]) is None
    assert not send.called


@pytest.mark.asyncio
@pytest.mark.parametrize("at", [datetime(2026, 9, 22, 11, 31), datetime(2026, 9, 22, 15, 1),
                                datetime(2026, 9, 25, 10), datetime(2026, 9, 26, 10)])
async def test_closed_session_rejected(env, at):
    maker, clock, send = env
    clock[0] = at
    key, _ = await seed(maker, at)
    await points.dispatch_c3_research(now=at, session_factory=maker)
    assert (await receipt(maker, key, at))["status"] == "rejected"
    assert not send.called



@pytest.mark.asyncio
async def test_cancelled_transport_keeps_durable_attempting_lease(env):
    maker, clock, send = env
    key, _ = await seed(maker, clock[0])
    send.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert (await receipt(maker, key, clock[0]))["status"] == "attempting"
    clock[0] += timedelta(seconds=29)
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert send.call_count == 1


@pytest.mark.asyncio
async def test_hourly_cap_persists_after_restart(env, monkeypatch):
    maker, clock, send = env
    key, _ = await seed(maker, clock[0])
    monkeypatch.setattr(settings, "PUSH_HOURLY_LIMIT", 1)
    async with maker() as db:
        db.add(PaperAutoTradeLog(account_id=None, run_id="other", trade_date=clock[0].date(),
            created_at=clock[0] - timedelta(seconds=60), source=points.C3_ROUTE, code="600002",
            action="research_push", decision="sent", stage_code="c3_research_delivery"))
        await db.commit()
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    r = await receipt(maker, key, clock[0])
    assert r["status"] == "throttled" and r["cause"] == "hourly_batch_limit"
    assert not send.called


@pytest.mark.asyncio
async def test_transport_false_not_sent_and_missing_identity_rejected(env):
    maker, clock, send = env
    key, _ = await seed(maker, clock[0])
    send.side_effect = None
    send.return_value = {"status": "failed", "channels": {"feishu": False}}
    assert (await points.dispatch_c3_research(now=clock[0], session_factory=maker))["status"] == "failed"
    clock[0] += timedelta(seconds=16)
    async with maker() as db:
        tag = await db.scalar(select(StockTag))
        await db.delete(tag)
        await db.commit()
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert (await receipt(maker, key, clock[0]))["status"] == "waiting"
    assert send.call_count == 1



@pytest.mark.asyncio
async def test_228_concurrent_confirmations_paginate_and_expire_with_limit_audit(env, monkeypatch):
    maker, clock, send = env
    monkeypatch.setattr(settings, "PUSH_HOURLY_LIMIT", 1)
    keys = []
    async with maker() as db:
        for i in range(228):
            code = f"{600000 + i}"
            data = shadow._event(route_id=shadow.ROUTE_C3, trade_date=clock[0].date(),
                observed_at=clock[0], code=code, name="隔离并发", event_type="confirmed",
                status="confirmed", quote={"code": code, "price": 10.5,
                    "source_quote_at": clock[0].isoformat()}, prior={})
            data["created_at"] = clock[0]
            keys.append(data["event_key"])
            db.add(PaperShadowEvent(**data))
        db.add(PaperAutoTradeLog(account_id=None, run_id="previous-hour-card", trade_date=clock[0].date(),
            created_at=clock[0] - timedelta(seconds=1), source=points.C3_ROUTE, code="600999",
            action="research_push", decision="sent", stage_code="c3_research_delivery"))
        await db.commit()
    for expected in (100, 200, 228):
        await points.dispatch_c3_research(now=clock[0], session_factory=maker)
        async with maker() as db:
            receipts = await points.c3_research_notification_receipts(db, event_keys=keys, now=clock[0])
        assert len(receipts) == expected
        assert {r["status"] for r in receipts.values()} == {"throttled"}
        assert {r["cause"] for r in receipts.values()} == {"hourly_batch_limit"}
    clock[0] += timedelta(seconds=181)
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    async with maker() as db:
        receipts = await points.c3_research_notification_receipts(db, event_keys=keys, now=clock[0])
    assert len(receipts) == 228
    assert {r["status"] for r in receipts.values()} == {"expired"}
    assert {r["previous_cause"] for r in receipts.values()} == {"hourly_batch_limit"}
    assert not send.called  # No promise that all research confirmations are delivered.



@pytest.mark.asyncio
async def test_slow_transport_over_two_seconds_does_not_hold_original_lock_or_cancel(env, monkeypatch):
    maker, clock, send = env
    key, _ = await seed(maker, clock[0])
    transport_started = asyncio.Event()
    transport_finished = asyncio.Event()
    async def slow_send(*args, **kwargs):
        transport_started.set()
        await asyncio.sleep(2.1)  # Explicit regression for the removed 2s cancellation.
        transport_finished.set()
        return {"channels": {"feishu": True}}
    send.side_effect = slow_send
    original = AsyncMock(return_value={"status": "sent", "count": 12})
    monkeypatch.setattr(points, "_dispatch", original)
    assert (await points.dispatch_buy_points(now=clock[0], session_factory=maker))["count"] == 12
    task = asyncio.create_task(points.dispatch_c3_research(now=clock[0], session_factory=maker))
    try:
        await asyncio.wait_for(transport_started.wait(), timeout=1)
        assert not points._dispatch_lock.locked()
        assert (await points.dispatch_buy_points(now=clock[0], session_factory=maker))["count"] == 12
        assert (await points.dispatch_c3_research(now=clock[0], session_factory=maker))["status"] == "busy"
        assert not task.done()
        await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert transport_finished.is_set() and send.call_count == 1 and original.call_count == 2
    assert (await receipt(maker, key, clock[0]))["status"] == "sent"


@pytest.mark.asyncio
async def test_current_pool_projected_only_once_for_multiple_rejected_candidates(env, monkeypatch):
    maker, clock, send = env
    key, _ = await seed(maker, clock[0], defect="no_frames")
    async with maker() as db:
        data = shadow._event(route_id=shadow.ROUTE_C3, trade_date=clock[0].date(),
            observed_at=clock[0], code="600002", name="隔离2", event_type="confirmed",
            status="confirmed", quote={"code": "600002", "price": 10.5,
                "source_quote_at": clock[0].isoformat()}, prior={})
        data["created_at"] = clock[0]
        db.add(PaperShadowEvent(**data))
        db.add(StockTag(code="600002", name="隔离2", board_type="main_sh", board_tag="tradeable",
            is_st=False, is_suspended=False, is_delisting=False, is_ipo_recent=False))
        await db.commit()
    project = AsyncMock(wraps=shadow.build_first_board_current_pool)
    monkeypatch.setattr(shadow, "build_first_board_current_pool", project)
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    assert project.call_count == 1
    assert not send.called



@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed,expected", [(16, "sent"), (181, "expired")])
async def test_temporary_unknown_can_recover_only_within_original_ttl(env, elapsed, expected):
    maker, clock, send = env
    original_at = clock[0]
    key, _ = await seed(maker, original_at, defect="quality")
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    r = await receipt(maker, key, clock[0])
    assert r["status"] == "waiting" and r["cause"] == "temporary_data_wait"
    assert not send.called
    # Complete/repair the same bounded quote window; never change source clocks.
    async with maker() as db:
        for row in (await db.scalars(select(QuoteRound))).all():
            row.quality_status = "ok"
        await db.commit()
    clock[0] += timedelta(seconds=elapsed)
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    r = await receipt(maker, key, clock[0])
    assert r["status"] == expected and r["signal_observed_at"] == original_at.isoformat()
    assert send.call_count == (1 if expected == "sent" else 0)
    if expected == "expired":
        assert r["previous_status"] == "waiting" and r["previous_cause"] == "temporary_data_wait"


@pytest.mark.asyncio
async def test_healthy_pool_explicit_stock_invalidation_is_terminal(env):
    maker, clock, send = env
    key, _ = await seed(maker, clock[0])
    async with maker() as db:
        frames = (await db.scalars(select(PaperShadowEvent).where(
            PaperShadowEvent.event_type == "universe_audit"))).all()
        for row in frames:
            data = json.loads(row.snapshot_json)
            data["prior_structure"]["current_pool"]["members"][0]["eligible"] = False
            row.snapshot_json = json.dumps(data)
        await db.commit()
    await points.dispatch_c3_research(now=clock[0], session_factory=maker)
    r = await receipt(maker, key, clock[0])
    assert r["status"] == "rejected" and r["cause"] == "current_stock_invalidated"
    assert not send.called


async def _transport_frame(maker, at, kind="good"):
    """Append new evidence, never rewrite the previously accepted audit."""
    version = shadow.route_version_for(shadow.ROUTE_C3)
    rid = "transport:" + at.isoformat()
    async with maker() as db:
        db.add(QuoteRound(round_id=rid, trade_date=at.date(), as_of_at=at,
            committed_at=at, expected_count=1, received_count=1,
            quality_status="degraded" if kind == "coverage" else "ok",
            config_version="test", code_version="test"))
        db.add(PaperShadowEvent(event_key=rid, route_id=shadow.ROUTE_C3,
            route_version=version, trade_date=at.date(), observed_at=at, created_at=at,
            code="MARKET", event_type="universe_audit", status="complete",
            snapshot_json=json.dumps({"prior_structure": {"quote_coverage": 1,
                "current_pool": {"read_model_version": "c3_current_pool_v1",
                    "quote_round_ids": [rid], "members": [{"code": "600001",
                        "eligible": kind != "invalid",
                        "confirmation_frame": kind not in {"invalid", "reset"},
                        "source_quote_at": at.isoformat()}]}}})))
        if kind in {"invalid", "reset"}:
            db.add(PaperShadowEvent(event_key=rid + ":reset", route_id=shadow.ROUTE_C3,
                route_version=version, trade_date=at.date(), observed_at=at,
                created_at=at, code="600001", event_type="confirmation_reset",
                status="reset", snapshot_json=json.dumps({"reason": "route_condition_failed_or_unknown"})))
        await db.commit()


def _transport_sessions(maker, monkeypatch, *, after_attempt=None, after_read=None):
    """Deterministic handoff AFTER actual commit and session release."""
    state = {"active": 0, "attempted": False, "final_reads": 0}
    original = points._c3_current_check
    async def check(db, code, now, *, pool=None):
        if pool is None:
            db.final_read = True
            state["final_reads"] += 1
        return await original(db, code, now, pool=pool)
    monkeypatch.setattr(points, "_c3_current_check", check)

    class Session:
        def __init__(self):
            self.db = maker()
            self.attempt = self.final_read = False
        async def __aenter__(self):
            await self.db.__aenter__()
            state["active"] += 1
            return self
        def __getattr__(self, name):
            return getattr(self.db, name)
        async def commit(self):
            self.attempt |= any(isinstance(r, PaperAutoTradeLog)
                and r.action == "research_push" and r.decision == "attempting"
                for r in self.db.new)
            await self.db.commit()
        async def __aexit__(self, *args):
            try:
                await self.db.__aexit__(*args)
            finally:
                state["active"] -= 1
            if self.attempt and not state["attempted"]:
                state["attempted"] = True
                if after_attempt:
                    await after_attempt()
            if self.final_read and after_read:
                await after_read()
    return Session, state


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,status,reason", [
    ("invalid", "rejected", None),
    ("reset", "waiting", "current_continuity_unproven"),
    ("coverage", "waiting", "current_confirmation_unavailable"),
    ("missing_tag", "waiting", "stock_identity_missing"),
    ("st", "rejected", None),
])
async def test_transport_rechecks_committed_new_evidence(env, monkeypatch, kind, status, reason):
    maker, clock, send = env
    original_at = clock[0]
    key, frozen = await seed(maker, original_at)
    async def mutate():
        clock[0] += timedelta(seconds=1)
        if kind in {"st", "missing_tag"}:
            async with maker() as db:
                tag = await db.scalar(select(StockTag))
                if kind == "st":
                    tag.is_st = True
                else:
                    await db.delete(tag)
                await db.commit()
        else:
            await _transport_frame(maker, clock[0], kind)
    factory, state = _transport_sessions(maker, monkeypatch, after_attempt=mutate)
    await points.dispatch_c3_research(now=original_at, session_factory=factory)
    assert send.call_count == 0
    r = await receipt(maker, key, clock[0])
    assert r["status"] == status and r["data_wait_reason"] == reason
    assert r["cause"] == ("temporary_data_wait" if status == "waiting" else
        "stock_identity_not_tradeable" if kind == "st" else "current_stock_invalidated")
    assert r["send_started_at"] is None
    assert state["active"] == 0 and state["final_reads"] == 1
    async with maker() as db:
        event = await db.scalar(select(PaperShadowEvent).where(PaperShadowEvent.event_key == key))
        assert event.snapshot_json == frozen
        signals = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.action == "research_signal"))).all())
        assert len(signals) == 1 and signals[0].price == 10.5
        assert json.loads(signals[0].candidate_json)["signal_observed_at"] == original_at.isoformat()
        assert await db.scalar(select(func.count(TradeOrder.id))) == 0
    if status == "rejected":
        # New valid evidence cannot revive the same terminal notification.
        async with maker() as db:
            tag = await db.scalar(select(StockTag))
            tag.is_st = False
            await db.commit()
        for seconds in (16, 46, 76):
            await _transport_frame(maker, original_at + timedelta(seconds=seconds))
        clock[0] = original_at + timedelta(seconds=76)
        await points.dispatch_c3_research(now=clock[0], session_factory=factory)
        assert (await receipt(maker, key, clock[0]))["status"] == "rejected"
        assert send.call_count == 0 and state["final_reads"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("delay,status", [(181, "expired"), (76, "waiting")])
async def test_transport_clock_checked_after_read_session_release(env, monkeypatch, delay, status):
    maker, clock, send = env
    at = clock[0]
    key, _ = await seed(maker, at)
    async def release():
        clock[0] = at + timedelta(seconds=delay)
    factory, state = _transport_sessions(maker, monkeypatch, after_read=release)
    await points.dispatch_c3_research(now=at, session_factory=factory)
    assert send.call_count == 0
    r = await receipt(maker, key, clock[0])
    assert r["status"] == status and r["send_started_at"] is None
    assert state["active"] == 0
    if status == "waiting":
        assert r["data_wait_reason"] == "current_confirmation_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [False, True])
async def test_transport_unknown_retry_uses_original_identity_and_ttl(env, monkeypatch, late):
    maker, clock, send = env
    at = clock[0]
    key, _ = await seed(maker, at)
    async def mutate():
        clock[0] += timedelta(seconds=1)
        await _transport_frame(maker, clock[0], "reset")
    factory, state = _transport_sessions(maker, monkeypatch, after_attempt=mutate)
    await points.dispatch_c3_research(now=at, session_factory=factory)
    assert (await receipt(maker, key, clock[0]))["status"] == "waiting"
    for seconds in (16, 46, 76):
        await _transport_frame(maker, at + timedelta(seconds=seconds))
    clock[0] = at + timedelta(seconds=181 if late else 76)
    async def fake_send(*args, **kwargs):
        assert state["active"] == 0
        return {"channels": {"feishu": True}}
    send.side_effect = fake_send
    await points.dispatch_c3_research(now=clock[0], session_factory=factory)
    r = await receipt(maker, key, clock[0])
    assert r["status"] == ("expired" if late else "sent")
    assert send.call_count == (0 if late else 1)
    assert r["signal_observed_at"] == at.isoformat()
    async with maker() as db:
        logs = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.action.in_(("research_signal", "research_push"))))).all())
        assert len({r.run_id for r in logs}) == 1
        assert sum(r.action == "research_signal" for r in logs) == 1


@pytest.mark.asyncio
async def test_transport_attempt_commit_can_exhaust_original_ttl(env, monkeypatch):
    maker, clock, send = env
    at = clock[0]
    key, _ = await seed(maker, at)
    async def delay():
        clock[0] = at + timedelta(seconds=181)
    factory, state = _transport_sessions(maker, monkeypatch, after_attempt=delay)
    await points.dispatch_c3_research(now=at, session_factory=factory)
    r = await receipt(maker, key, clock[0])
    assert r["status"] == "expired" and r["send_started_at"] is None
    assert send.call_count == 0 and state["final_reads"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_transport_read_failure_releases_session_and_retains_lease(env, monkeypatch, error):
    maker, clock, send = env
    at = clock[0]
    key, _ = await seed(maker, at)
    factory, state = _transport_sessions(maker, monkeypatch)
    check = points._c3_current_check
    failed = False
    async def fail_once(db, code, now, *, pool=None):
        nonlocal failed
        if pool is None and not failed:
            failed = True
            raise error()
        return await check(db, code, now, pool=pool)
    monkeypatch.setattr(points, "_c3_current_check", fail_once)
    with pytest.raises(error):
        await points.dispatch_c3_research(now=at, session_factory=factory)
    assert state["active"] == 0 and send.call_count == 0
    assert not points._c3_lock.locked()
    assert (await receipt(maker, key, clock[0]))["status"] == "attempting"
    clock[0] += timedelta(seconds=31)
    await points.dispatch_c3_research(now=clock[0], session_factory=factory)
    assert send.call_count == 1
    assert (await receipt(maker, key, clock[0]))["signal_observed_at"] == at.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("delay,expected", [(0, "sent"), (75, "sent"), (76, "waiting"), (-1, "expired")])
async def test_transport_release_clock_boundaries_and_bounded_projection(env, monkeypatch, delay, expected):
    maker, clock, send = env
    at = clock[0]
    key, _ = await seed(maker, at)
    async def release():
        clock[0] = at + timedelta(seconds=delay)
    factory, state = _transport_sessions(maker, monkeypatch, after_read=release)
    project = AsyncMock(wraps=shadow.build_first_board_current_pool)
    monkeypatch.setattr(shadow, "build_first_board_current_pool", project)
    async def send_without_session(*args, **kwargs):
        assert state["active"] == 0
        # Explicit session count plus independent writer, not WAL assumptions.
        async with maker() as db:
            db.add(PaperAutoTradeLog(run_id="transport-write", trade_date=at.date(),
                created_at=clock[0], action="test", decision="test"))
            await db.commit()
        return {"channels": {"feishu": True}}
    send.side_effect = send_without_session
    await points.dispatch_c3_research(now=at, session_factory=factory)
    r = await receipt(maker, key, max(at, clock[0]))
    assert r["status"] == expected
    assert project.call_count == 2 and state["final_reads"] == 1
    assert send.call_count == (1 if expected == "sent" else 0)
    async with maker() as db:
        rows = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.action == "research_push").order_by(PaperAutoTradeLog.id))).all())
        p = json.loads(rows[-1].candidate_json)
        assert p["recheck_started_at"] == at.isoformat()
        assert p["recheck_completed_at"] == clock[0].isoformat()
        assert p["final_checked_at"] == clock[0].isoformat()
        assert p["recheck_quote_round_id"] == "q0"


@pytest.mark.asyncio
async def test_transport_card_uses_new_healthy_frame_without_rewriting_origin(env, monkeypatch):
    maker, clock, send = env
    at = clock[0]
    key, frozen = await seed(maker, at)
    async def append_healthy():
        clock[0] = at + timedelta(seconds=30)
        await _transport_frame(maker, clock[0])
    factory, state = _transport_sessions(maker, monkeypatch, after_attempt=append_healthy)
    card_items = []
    build = points.build_c3_research_message
    def capture(item):
        card_items.append({**item, "payload": dict(item["payload"])})
        return build(item)
    monkeypatch.setattr(points, "build_c3_research_message", capture)
    await points.dispatch_c3_research(now=at, session_factory=factory)
    assert send.call_count == 1 and state["active"] == 0
    message = send.call_args.args[0]
    stamp = clock[0].isoformat()
    assert f"通知当前复验时点：{stamp}" in message.content
    assert f"当前池证据时点：{stamp}" in message.content
    assert f"原始确认时间：{at.isoformat()}" in message.content
    assert f"原始个股行情：{at.isoformat()}" in message.content
    assert "原确认参考价：¥10.50" in message.content
    item = card_items[-1]
    p = item["payload"]
    assert p["current_pool_expires_at"] == (clock[0] + timedelta(seconds=75)).isoformat()
    assert p["recheck_quote_round_id"] == "transport:" + stamp
    async with maker() as db:
        signal = await db.scalar(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.action == "research_signal"))
        original = points._item(signal)
        assert item["run_id"] == original["run_id"]
        assert item["price"] == original["price"]
        assert item["quote_round_id"] == original["quote_round_id"]
        # Selection already attached q0 to its transient payload; the final
        # recheck must not replace that with the new transport round.
        assert p["quote_round_id"] == "q0"
        for field in ("signal_observed_at", "source_quote_at"):
            assert p.get(field) == original["payload"].get(field)
        event = await db.scalar(select(PaperShadowEvent).where(PaperShadowEvent.event_key == key))
        assert event.snapshot_json == frozen
    r = await receipt(maker, key, clock[0])
    for field in ("checked_at", "recheck_started_at", "recheck_completed_at", "final_checked_at"):
        assert r[field] == stamp
    assert r["recheck_quote_round_id"] == p["recheck_quote_round_id"]


def test_category_whitelist_stays_narrow(monkeypatch):
    monkeypatch.setattr(settings, "FEISHU_PAPER_BUY_POINTS_ONLY", True)
    for category, expected in [("paper_buy_point", True), ("c3_research_signal", True),
                               ("research", False), ("anomaly", False), ("risk", False)]:
        assert feishu_category_allowed(PushMessage(title="t", content="c", category=category)) is expected
