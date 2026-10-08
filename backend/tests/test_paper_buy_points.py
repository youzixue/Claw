"""通知单测只用隔离SQLite和假飞书，不向真实群发消息。"""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config.settings import settings
from app.models.paper import PaperAutoTradeLog, PaperAccount, PaperTradeLog
from app.models.trading import TradeOrder, TradeFill
from app.models.stock import StockTag
from app.push import paper_buy_points as points
from app.paper.account_policy import ACCOUNT_NAMES


@pytest_asyncio.fixture
async def setup(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'push.db'}")
    async with engine.begin() as conn:
        for table in (PaperAutoTradeLog.__table__, PaperAccount.__table__, StockTag.__table__,
                      PaperTradeLog.__table__, TradeOrder.__table__, TradeFill.__table__):
            await conn.run_sync(table.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now().replace(microsecond=0)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(points, "datetime", Clock)
    monkeypatch.setattr(settings, "PAPER_BUY_POINT_PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", False)
    monkeypatch.setattr(settings, "PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_BUY_POINT_PUSH_MAX_AGE_SEC", 180)
    monkeypatch.setattr(settings, "PAPER_BUY_POINT_PUSH_RETRY_SEC", 15)
    monkeypatch.setattr(settings, "PUSH_STOCK_COOLDOWN", 300)
    monkeypatch.setattr(settings, "PUSH_HOURLY_LIMIT", 30)
    # 参数传入的datetime仍通过模块isinstance；用Clock创建时间。
    now = Clock.fromisoformat(now.isoformat())
    send = AsyncMock(return_value={"sent": True, "channels": {"feishu": True}, "status": "sent"})
    monkeypatch.setattr(points.push_scheduler, "push_to_channels", send)
    async with maker() as db:
        db.add(StockTag(code="600001", name="隔离测试", board_type="main_sh", board_tag="tradeable",
                        is_st=False, is_suspended=False, is_delisting=False))
        await db.commit()
    yield maker, now, send
    await engine.dispose()


async def record(maker, now, *, account="default", version="version-one", key="point", **overrides):
    account_obj = SimpleNamespace(id=ACCOUNT_NAMES.index(account)+1, account_name=account, status="active")
    args = dict(account=account_obj, strategy_version=version, label=f"策略-{account}",
        code="600001", name="隔离测试", source="test_route", signal_key=key,
        reason="真实形态且持续确认通过，VWAP上方", price=10.5,
        observed_at=now, decision_run_id="real-decision", quote_round_id="round-current", as_of_at=now)
    args.update(overrides)
    async with maker() as db:
        row = await points.record_buy_point(db, **args)
        await db.commit()
        return row


async def logs(maker):
    async with maker() as db:
        return list((await db.scalars(select(PaperAutoTradeLog).order_by(PaperAutoTradeLog.id))).all())


@pytest.mark.asyncio
async def test_all_twelve_accounts_same_stock_are_isolated_and_batched(setup):
    maker, now, send = setup
    for account in ACCOUNT_NAMES:
        assert await record(maker, now, account=account)
        assert await record(maker, now, account=account) is None
    assert not send.called  # 交易写日志不联网
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))["count"] == 6
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))["count"] == 6
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))["count"] == 0
    assert send.call_count == 2
    assert sum(x.action == points.SIGNAL for x in await logs(maker)) == 12
    contents = "\n".join(call.args[0].content for call in send.call_args_list)
    assert "主账户" in contents and "次账户" in contents
    for name in ACCOUNT_NAMES:
        assert f"策略-{name}" in contents
    assert "VWAP" in contents and "北京时间" in contents and "非下单或成交" in contents


@pytest.mark.asyncio
async def test_delivery_failure_retries_and_dedup_survives_worker_restart(setup):
    maker, now, send = setup
    await record(maker, now)
    send.return_value = {"sent": False, "channels": {"feishu": False}, "status": "failed"}
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))["status"] == "failed"
    assert (await points.dispatch_buy_points(now=now+timedelta(seconds=5), session_factory=maker))["count"] == 0
    send.return_value = {"sent": True, "channels": {"feishu": True}, "status": "sent"}
    assert (await points.dispatch_buy_points(now=now+timedelta(seconds=16), session_factory=maker))["status"] == "sent"
    points._runtime.clear()  # 状态不是内存去重
    assert (await points.dispatch_buy_points(now=now+timedelta(seconds=32), session_factory=maker))["count"] == 0
    assert send.call_count == 2
    states = [x.decision for x in await logs(maker) if x.action == points.DELIVERY]
    assert states == ["attempting", "failed", "attempting", "sent"]


@pytest.mark.asyncio
async def test_stale_events_expire_without_historical_push(setup):
    maker, now, send = setup
    assert await record(maker, now, observed_at=now-timedelta(days=1)) is None
    assert await record(maker, now, observed_at=now+timedelta(seconds=1)) is None
    assert await record(maker, now, as_of_at=now-timedelta(seconds=181)) is None
    await record(maker, now)
    await points.dispatch_buy_points(now=now+timedelta(seconds=181), session_factory=maker)
    assert not send.called
    assert (await logs(maker))[-1].decision == "expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"quote_round_id":""}, {"as_of_at":None}, {"price":float("nan")},
    {"price":0}, {"strategy_version":""}, {"signal_key":""}, {"reason":""},
])
async def test_incomplete_signal_never_queued(setup, overrides):
    maker, now, send = setup
    assert await record(maker, now, **overrides) is None
    assert await logs(maker) == []


@pytest.mark.asyncio
async def test_ineligible_and_missing_security_tags_fail_closed(setup):
    maker, now, _ = setup
    assert await record(maker, now, code="300001") is None
    async with maker() as db:
        tag = await db.get(StockTag, "600001")
        tag.is_st = True
        await db.commit()
    assert await record(maker, now) is None


@pytest.mark.asyncio
async def test_version_isolation_and_per_account_cooldown(setup):
    maker, now, send = setup
    await record(maker, now, key="one")
    await points.dispatch_buy_points(now=now, session_factory=maker)
    await record(maker, now, key="two")
    await record(maker, now, version="version-two")
    assert (await points.dispatch_buy_points(now=now+timedelta(seconds=1), session_factory=maker))["count"] == 1
    rows = await logs(maker)
    assert any(x.decision == "throttled" for x in rows)
    assert send.call_count == 2


@pytest.mark.asyncio
async def test_persistent_hourly_cap(setup, monkeypatch):
    maker, now, send = setup
    monkeypatch.setattr(settings, "PUSH_HOURLY_LIMIT", 1)
    await record(maker, now)
    await points.dispatch_buy_points(now=now, session_factory=maker)
    await record(maker, now, account="challenger_a")
    assert (await points.dispatch_buy_points(now=now+timedelta(seconds=1), session_factory=maker))["count"] == 0
    assert send.call_count == 1
    assert (await logs(maker))[-1].decision == "throttled"


@pytest.mark.asyncio
async def test_lease_prevents_immediate_duplicate_after_crash(setup):
    maker, now, send = setup
    row = await record(maker, now)
    async with maker() as db:
        await points._audit(db, points._item(row), "attempting", now, batch_id="crash")
        await db.commit()
    assert (await points.dispatch_buy_points(now=now+timedelta(seconds=20), session_factory=maker))["count"] == 0
    assert (await points.dispatch_buy_points(now=now+timedelta(seconds=31), session_factory=maker))["count"] == 1


@pytest.mark.asyncio
async def test_network_has_no_active_write_transaction_and_worker_is_nonreentrant(setup):
    maker, now, send = setup
    await record(maker, now)
    started, release = asyncio.Event(), asyncio.Event()
    async def slow(*args, **kwargs):
        # HTTP期间另一个写事务正常完成
        async with maker() as db:
            db.add(PaperAccount(account_name="test", initial_capital=1))
            await db.commit()
        started.set()
        await release.wait()
        return {"channels":{"feishu": True}, "status":"sent"}
    send.side_effect = slow
    task = asyncio.create_task(points.dispatch_buy_points(now=now, session_factory=maker))
    await asyncio.wait_for(started.wait(), 3)
    try:
        assert (await points.dispatch_buy_points(now=now, session_factory=maker))["status"] == "busy"
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_execution_audit_does_not_claim_unfilled_order_is_filled(setup):
    maker, now, send = setup
    await record(maker, now, queue_order=True)
    async with maker() as db:
        db.add(PaperAutoTradeLog(account_id=1, run_id="real-decision", trade_date=now.date(),
            created_at=now, code="600001", action="deferred_buy", decision="wait",
            source="test_route", strategy_version="version-one", reason="等待下一轮撮合"))
        await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=maker)
    message = send.call_args.args[0]
    assert "已提交模拟委托" in message.content and "未确认成交" in message.content
    assert "排队不等于成交" in message.content
    assert "已有模拟成交记录" not in message.content


@pytest.mark.asyncio
async def test_cross_day_pending_is_expired_not_delivered(setup):
    maker, now, send = setup
    await record(maker, now)
    await points.dispatch_buy_points(now=now+timedelta(days=1), session_factory=maker)
    assert not send.called
    assert (await logs(maker))[-1].decision == "expired"


@pytest.mark.asyncio
async def test_missing_feishu_does_not_become_delivered(setup):
    maker, now, send = setup
    await record(maker, now)
    send.return_value = {"sent":True, "channels":{"websocket":True, "feishu":False}, "status":"partial"}
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))["status"] == "failed"


@pytest.mark.asyncio
async def test_notification_switch_does_not_call_network(setup, monkeypatch):
    maker, now, send = setup
    monkeypatch.setattr(settings, "PAPER_BUY_POINT_PUSH_ENABLED", False)
    assert await record(maker, now) is None
    assert (await points.dispatch_buy_points(now=now, session_factory=maker))["status"] == "disabled"
    assert not send.called


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["source", "version", "future"])
async def test_execution_note_never_borrows_another_signal_or_future_decision(setup, kind):
    maker, now, send = setup
    await record(maker, now)
    async with maker() as db:
        db.add(PaperAutoTradeLog(account_id=1, run_id="real-decision", trade_date=now.date(),
            created_at=now+timedelta(seconds=1) if kind == "future" else now,
            code="600001", action="buy", decision="executed", executed_trade_id=123,
            source="position-t" if kind == "source" else "test_route",
            strategy_version="another-version" if kind == "version" else "version-one",
            reason="不相关的模拟成交"))
        await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=maker)
    content = send.call_args.args[0].content
    assert "已有模拟成交" not in content and "不相关" not in content


def test_candidate_reason_keeps_strategy_evidence_not_generic_score():
    for source in ("promotion_promotion", "promotion_mainline", "promotion_auction", "tenbagger_midline", "reversal_pullback"):
        text = points.candidate_reason({"_source":source, "strategy_label":"指定策略",
            "entry_condition":"昨日二板后今早放量，VWAP已收复", "confirmation_persistence_sec":60}, "泛化评分")
        assert "指定策略" in text and "VWAP已收复" in text and "持续60" in text
        assert "泛化评分" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("action,decision,trade_id,expected", [
    # A bare ID is not a verified ledger receipt. Real filled/partial positives
    # live in test_execution_loop_boundary_20260923 with all three entity types.
    ("buy", "executed", 123, "🔎 原条件曾确认 · 执行未核实 · 非下单或成交"),
    ("buy", "executed", None, "🔎 原条件曾确认 · 执行未核实 · 非下单或成交"),
    ("buy", "dry_run", 123, "🔎 原条件曾确认 · 执行未核实 · 非下单或成交"),
    ("deferred_buy", "wait", None, "⏳ 已提交模拟委托 · 未确认成交"),
    ("wait_buy", "wait", None, "⏸ 本轮未下单 · 等待条件"),
    ("skip_buy", "blocked", None, "⛔ 本轮执行已拦截"),
    ("skip_terminal", "skipped", None, "⌛ 原买点已失效 · 本轮未下单"),
])
async def test_card_execution_badge_uses_scoped_audit_not_reason_text(
    setup, action, decision, trade_id, expected,
):
    maker, now, send = setup
    await record(maker, now)
    async with maker() as db:
        db.add(PaperAutoTradeLog(
            account_id=1, run_id="real-decision", trade_date=now.date(),
            created_at=now, code="600001", action=action, decision=decision,
            executed_trade_id=trade_id, source="test_route", strategy_version="version-one",
            reason="原始执行原因完整保留",
        ))
        await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=maker)
    content = send.call_args.args[0].content
    assert expected in content
    assert "原始执行原因完整保留" in content
    if action != "buy" or decision != "executed" or not trade_id:
        assert "✅ 已有模拟成交记录" not in content
