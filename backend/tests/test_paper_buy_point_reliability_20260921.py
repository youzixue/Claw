"""入口故障/补偿与合批：独立SQLite，假渠道，禁止应用生命周期。"""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.paper import PaperAccount, PaperAutoTradeLog
from app.config.settings import settings
from test_paper_buy_point_delivery_clock import advancing_clock
from app.push import paper_buy_points as points
from test_paper_buy_points import setup, record, logs
from test_paper_buy_point_template import item


async def failed_entry(maker, now):
    async with maker() as db:
        db.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
        await db.commit()
    class Broken(AsyncSession):
        async def scalar(self, *args, **kwargs):
            raise OperationalError("isolated injected failure", {}, Exception("locked"))
    broken = async_sessionmaker(maker.kw["bind"], class_=Broken, expire_on_commit=False)
    db = broken()
    assert await points.record_buy_point(db,
        account=SimpleNamespace(id=1, account_name="default", status="active"),
        strategy_version="version-one", label="策略", code="600001", name="隔离",
        source="test_route", signal_key="immutable-event-001", reason="确认通过",
        price=10.5, observed_at=now, decision_run_id="real-decision",
        quote_round_id="round-current", as_of_at=now) is None
    await db.rollback()
    return db


@pytest.mark.asyncio
async def test_failed_entry_retries_exact_identity_after_transaction_release(setup):
    maker, now, send = setup
    db = await failed_entry(maker, now)
    try:
        assert await logs(maker) == []  # 原代码确实吞掉入口写失败
        result = await points.retry_failed_buy_points(db, session_factory=maker)
        assert result["recovered"] == 1 and result["failed"] == 0
        signal = [r for r in await logs(maker) if r.action == points.SIGNAL]
        assert len(signal) == 1
        assert json.loads(signal[0].candidate_json)["signal_key"] == "immutable-event-001"
        labels = json.loads(signal[0].candidate_json)["signal_labels"]
        assert labels["status"] == "unknown"
        assert labels["reason"] == "ingress_recovered_original_labels_unavailable"
        assert json.loads(signal[0].candidate_json)["ingress_recovery"] == {
            "schema": "paper_buy_point_ingress_recovery_v1",
            "original_signal_observed_at": now.isoformat(),
            "recovered_at": now.isoformat(),
            "decision_run_id": "real-decision",
            "original_error_type": "OperationalError",
        }
        assert (await points.retry_failed_buy_points(db, session_factory=maker))["recovered"] == 0
        assert not send.called
    finally:
        await db.close()


def test_batch_does_not_let_oversize_head_block_small_cards(monkeypatch):
    samples = [item("default", 1), item("challenger_a", 2), item("challenger_b", 3)]
    def card(message):
        return {"text": "x" * (200 if 1 in message.extra["signal_ids"] else 30)}
    monkeypatch.setattr(points.feishu_channel, "_build_card", card)
    monkeypatch.setattr(points, "FEISHU_CARD_BUDGET_BYTES", 100)
    assert [r["id"] for r in points._fit_batch(samples)] == [2, 3]


@pytest.mark.asyncio
async def test_continuing_database_failure_is_not_claimed_durable(setup):
    maker, now, send = setup
    db = await failed_entry(maker, now)
    class BrokenCommit(AsyncSession):
        async def commit(self):
            raise OperationalError("commit injection", {}, Exception("locked"))
    broken = async_sessionmaker(maker.kw["bind"], class_=BrokenCommit, expire_on_commit=False)
    try:
        result = await points.retry_failed_buy_points(db, session_factory=broken)
        assert result["failed"] == 1 and result["recovered"] == 0
        # 独立补偿不使用savepoint，失败commit不被SQLite RELEASE提前持久化。
        assert await logs(maker) == []
        assert points._runtime["last_ingress_retry"]["volatile_pending"] == 1
        assert not send.called
        result = await points.retry_failed_buy_points(db, session_factory=maker)
        assert result["recovered"] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_retry_expired_entry_is_audit_only_not_buy_signal(setup, monkeypatch):
    maker, now, send = setup
    db = await failed_entry(maker, now)
    clock = advancing_clock(monkeypatch)
    clock["seconds"] = 181
    try:
        result = await points.retry_failed_buy_points(db, session_factory=maker)
        assert result["expired"] == 1
        rows = await logs(maker)
        assert len(rows) == 1 and rows[0].action == "signal_ingress"
        assert json.loads(rows[0].candidate_json)["event_key"] == "immutable-event-001"
        await points.dispatch_buy_points(now=now+timedelta(seconds=181), session_factory=maker)
        assert not send.called
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_retry_rejects_active_original_transaction_without_commit(setup):
    maker, now, _ = setup
    db = await failed_entry(maker, now)
    try:
        await db.begin()
        result = await points.retry_failed_buy_points(db, session_factory=maker)
        assert result["status"] == "transaction_active" and db.in_transaction()
        assert await logs(maker) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_recovery_idempotency_after_ambiguous_caller_retry(setup):
    maker, now, _ = setup
    db = await failed_entry(maker, now)
    pending = dict(db.info[points._FAILED_INGRESS])
    try:
        assert (await points.retry_failed_buy_points(db, session_factory=maker))["recovered"] == 1
        points._runtime.clear()
        db.info[points._FAILED_INGRESS] = pending
        assert (await points.retry_failed_buy_points(db, session_factory=maker))["recovered"] == 1
        assert len([r for r in await logs(maker) if r.action == points.SIGNAL]) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_changed_account_is_not_reactivated_by_recovery(setup):
    maker, now, send = setup
    db = await failed_entry(maker, now)
    try:
        async with maker() as session:
            account = await session.get(PaperAccount, 1)
            account.status = "closed"
            await session.commit()
        assert (await points.retry_failed_buy_points(db, session_factory=maker))["rejected"] == 1
        assert all(r.action != points.SIGNAL for r in await logs(maker))
        assert not send.called
    finally:
        await db.close()


async def quota(maker, now, count):
    async with maker() as db:
        for n in range(count):
            db.add(PaperAutoTradeLog(account_id=100+n, run_id=f"prior-{n}", trade_date=now.date(),
                created_at=now-timedelta(seconds=60), source="feishu", code="600999",
                action=points.DELIVERY, decision="sent", stage_code="notification_delivery",
                candidate_json=json.dumps({"batch_id": f"batch-{n}"})))
        await db.commit()


@pytest.mark.asyncio
async def test_original_thirty_batch_cap_and_expiry_cause_visible(setup, monkeypatch):
    maker, now, send = setup
    await quota(maker, now, 30)
    await record(maker, now)
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))["count"] == 0
    payload = json.loads((await logs(maker))[-1].candidate_json)
    assert payload["cause"] == "hourly_batch_limit" and payload["hourly_limit"] == 30
    await points.dispatch_buy_points(now=now+timedelta(seconds=181), session_factory=maker)
    payload = json.loads((await logs(maker))[-1].candidate_json)
    assert payload["previous_cause"] == "hourly_batch_limit"
    assert not send.called
    status = await points.notification_status(session_factory=maker)
    assert status["counts_today"]["expired"] == 1
    assert status["recent"][-1]["previous_cause"] == "hourly_batch_limit"


@pytest.mark.asyncio
async def test_quota_pressure_coalesces_at_most_one_original_poll_interval(setup, monkeypatch):
    maker, now, send = setup
    monkeypatch.setattr(settings, "PAPER_BUY_POINT_PUSH_INTERVAL_SEC", 5)
    await quota(maker, now, 15)
    await record(maker, now)
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))["status"] == "coalescing"
    await record(maker, now, account="challenger_a")
    assert (await points.dispatch_buy_points(now=now+timedelta(seconds=5), session_factory=maker))["count"] == 2
    assert send.call_count == 1


@pytest.mark.asyncio
async def test_merge_never_waits_when_source_quote_near_ttl(setup, monkeypatch):
    maker, now, send = setup
    monkeypatch.setattr(settings, "PAPER_BUY_POINT_PUSH_INTERVAL_SEC", 5)
    await quota(maker, now, 15)
    await record(maker, now, market_context={
        "price": 10.5, "quote_round_id": "round-current",
        "source_quote_at": (now-timedelta(seconds=178)).isoformat()})
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))["count"] == 1
    assert send.call_count == 1


@pytest.mark.asyncio
async def test_real_sqlite_writer_lock_failure_recovers_exact_event(setup):
    maker, now, send = setup
    async with maker() as seed:
        seed.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
        await seed.commit()
    async with maker() as holder, maker() as entry:
        await entry.execute(text("PRAGMA busy_timeout=10"))
        holder.add(PaperAccount(id=999, account_name="lock-holder", initial_capital=1))
        await holder.flush()  # 隔离库真实写锁，不伪造OperationalError
        row = await points.record_buy_point(entry,
            account=SimpleNamespace(id=1, account_name="default", status="active"),
            strategy_version="version-one", label="策略", code="600001", name="隔离",
            source="test_route", signal_key="real-lock-event", reason="确认通过",
            price=10.5, observed_at=now, decision_run_id="real-decision",
            quote_round_id="round-current", as_of_at=now)
        assert row is None
        failure = next(iter(entry.info[points._FAILED_INGRESS].values()))
        assert failure["error_type"] == "OperationalError"
        await entry.rollback()
        await holder.rollback()
        assert (await points.retry_failed_buy_points(entry, session_factory=maker))["recovered"] == 1
    rows = await logs(maker)
    signals = [r for r in rows if r.action == points.SIGNAL]
    assert len(signals) == 1
    assert json.loads(signals[0].candidate_json)["signal_key"] == "real-lock-event"
    assert not send.called


@pytest.mark.asyncio
async def test_retry_keeps_original_source_clock_not_recovery_clock(setup, monkeypatch):
    maker, now, send = setup
    db = await failed_entry(maker, now)
    # 保持fixture原Clock类型，避免换子类导致isinstance(as_of_at, datetime)假失败。
    clock = {"seconds": 175}
    monkeypatch.setattr(points.datetime, "now", classmethod(
        lambda cls, tz=None: now + timedelta(seconds=clock["seconds"])))
    try:
        assert (await points.retry_failed_buy_points(db, session_factory=maker))["recovered"] == 1
        clock["seconds"] = 181
        await points.dispatch_buy_points(now=now+timedelta(seconds=181), session_factory=maker)
        assert not send.called
        assert (await logs(maker))[-1].decision == "expired"
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_kind", ["new", "dirty", "deleted"])
async def test_savepoint_cannot_preflush_outer_trading_writes(setup, pending_kind):
    maker, now, send = setup
    async with maker() as seed:
        seed.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
        await seed.commit()
    async with maker() as db:
        account = await db.get(PaperAccount, 1)
        if pending_kind == "new":
            # 如果begin_nested预刷，此无initial_capital对象将IntegrityError并污染outer。
            pending = PaperAccount(account_name="invalid-outer-write")
            db.add(pending)
        elif pending_kind == "dirty":
            account.current_capital = 12345
        else:
            await db.delete(account)
        row = await points.record_buy_point(db, account=account,
            strategy_version="version-one", label="策略", code="600001", name="隔离",
            source="test_route", signal_key="preflush-event", reason="确认通过",
            price=10.5, observed_at=now, decision_run_id="real-decision",
            quote_round_id="round-current", as_of_at=now)
        assert row is None and db.is_active and db.in_transaction()
        assert getattr(db, pending_kind)
        failure = next(iter(db.info[points._FAILED_INGRESS].values()))
        assert failure["error_type"] == "OuterWritesDeferred"
        assert await logs(maker) == []
        # 交易所有者决定rollback；通知hook不替它commit或改变账户。
        await db.rollback()
        assert (await points.retry_failed_buy_points(db, session_factory=maker))["recovered"] == 1
        assert not send.called
    async with maker() as check:
        assert (await check.get(PaperAccount, 1)).current_capital != 12345


@pytest.mark.asyncio
async def test_rolled_back_execution_never_becomes_filled_notification(setup):
    maker, now, send = setup
    async with maker() as seed:
        seed.add(PaperAccount(id=1, account_name="default", initial_capital=50000, status="active"))
        await seed.commit()
    async with maker() as db:
        db.add(PaperAutoTradeLog(
            account_id=1, run_id="rolled-back-decision", trade_date=now.date(),
            created_at=now, source="test_route", code="600001", action="buy",
            decision="executed", executed_trade_id=321, strategy_version="version-one",
            reason="这条执行记录将被整个事务回滚"))
        assert await points.record_buy_point(db,
            account=SimpleNamespace(id=1, account_name="default", status="active"),
            strategy_version="version-one", label="策略", code="600001", name="隔离",
            source="test_route", signal_key="rollback-event", reason="技术确认已通过",
            price=10.5, observed_at=now, decision_run_id="rolled-back-decision",
            quote_round_id="round-current", as_of_at=now) is None
        await db.flush()  # 由交易所有者刷写，确保真正写入事务后rollback而不只是expunge
        await db.rollback()
        assert await logs(maker) == []
        assert (await points.retry_failed_buy_points(db, session_factory=maker))["recovered"] == 1
    assert {r.action for r in await logs(maker)} == {points.SIGNAL, "signal_ingress"}
    await points.dispatch_buy_points(now=now, session_factory=maker)
    assert send.call_count == 1
    message = send.call_args.args[0]
    assert "原条件曾确认 · 执行未核实 · 非下单或成交" in message.content
    assert "已有模拟成交记录" not in message.content
    assert "这条执行记录将被整个事务回滚" not in message.content


@pytest.mark.asyncio
async def test_recovery_marker_never_backwrites_preexisting_signal(setup):
    maker, now, _ = setup
    db = await failed_entry(maker, now)
    try:
        original = await record(maker, now, key="immutable-event-001")
        assert original is not None
        before = original.candidate_json
        assert "ingress_recovery" not in json.loads(before)
        assert (await points.retry_failed_buy_points(db, session_factory=maker))["recovered"] == 1
        signals = [r for r in await logs(maker) if r.action == points.SIGNAL]
        assert len(signals) == 1 and signals[0].candidate_json == before
    finally:
        await db.close()
