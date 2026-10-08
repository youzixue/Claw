"""SQLite WAL account-boundary regression; every write is pytest-isolated."""
import asyncio
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import paper
from app.data import scheduler as scheduler_module
from app.db.session import Base
from app.models.paper import PaperAccount, PaperNav

AT = datetime(2026, 9, 11, 10)


@pytest_asyncio.fixture
async def risk_env(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'risk-wal.db'}",
        connect_args={"timeout": .25},
    )
    async with engine.begin() as connection:
        await connection.execute(text("PRAGMA journal_mode=WAL"))
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add_all([
            PaperAccount(account_name=name, status="active", initial_capital=50000,
                         current_capital=50000)
            for name in ("reversal", "challenger_a")
        ])
        await db.commit()
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return AT
    monkeypatch.setattr(scheduler_module, "datetime", FixedDateTime)
    monkeypatch.setattr(scheduler_module, "async_session", factory)
    monkeypatch.setattr(paper, "PAPER_ALL_ACCOUNTS", ("reversal",))
    monkeypatch.setattr(paper, "PAPER_CHALLENGER_ACCOUNTS", ("challenger_a",))
    scheduler = scheduler_module.DataScheduler()
    payload = {"round_id": "isolated-risk-round", "committed_at": AT,
               "as_of_at": AT, "quality_status": "ok"}
    try:
        yield scheduler, factory, payload
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_account_writers_are_serialized(risk_env, monkeypatch):
    scheduler, factory, payload = risk_env
    active = peak = 0
    async def monitor(db, *, account_name, trigger):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            assert paper._quote_round_context()["round_id"] == payload["round_id"]
            await asyncio.sleep(0)  # Force overlap if the old gather is reintroduced.
            account = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == account_name))
            db.add(PaperNav(account_id=account.id, trade_date=AT.date(), nav=1))
            await db.commit()
        finally:
            active -= 1
    monkeypatch.setattr(paper, "run_paper_position_risk_monitor", monitor)
    await scheduler._run_quote_round_position_risk(payload)
    assert peak == 1
    assert scheduler.get_pipeline_runtime_status()["paper_position_risk"]["status"] == "ok"
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(PaperNav)) == 2


@pytest.mark.asyncio
async def test_real_busy_snapshot_restarts_from_fresh_session(risk_env, monkeypatch):
    scheduler, factory, payload = risk_env
    sessions, accounts_seen, original_errors = [], [], []
    async def monitor(db, *, account_name, trigger):
        sessions.append(db)
        accounts_seen.append(account_name)
        if account_name == "reversal" and accounts_seen.count(account_name) == 1:
            # A real stale read snapshot cannot be upgraded after another writer commits.
            await db.execute(text("BEGIN"))
            account = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == account_name))
            async with factory() as writer:
                await writer.execute(text(
                    "UPDATE paper_account SET current_capital=49000 WHERE account_name='reversal'"))
                await writer.commit()
            try:
                await db.execute(text(
                    "INSERT INTO paper_nav(account_id,trade_date,nav) VALUES(:id,:day,1)"),
                    {"id": account.id, "day": AT.date().isoformat()})
            except OperationalError as exc:
                original_errors.append(exc.orig.sqlite_errorcode)
                raise
            pytest.fail("fixture did not reproduce WAL snapshot contention")
        account = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == account_name))
        if account_name == "reversal":
            assert account.current_capital == 49000  # Never reuse the old identity map.
        db.add(PaperNav(account_id=account.id, trade_date=AT.date(), nav=1))
        await db.commit()
    monkeypatch.setattr(paper, "run_paper_position_risk_monitor", monitor)
    await scheduler._run_quote_round_position_risk(payload)
    assert original_errors == [sqlite3.SQLITE_BUSY_SNAPSHOT]
    assert accounts_seen == ["reversal", "reversal", "challenger_a"]
    assert len({id(db) for db in sessions}) == 3
    assert all(not db.in_transaction() for db in sessions)
    health = scheduler.get_pipeline_runtime_status()["paper_position_risk"]
    assert health["accounts"][0]["attempts"] == 2 and health["status"] == "ok"
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(PaperNav)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("busy,attempts", [(True, 2), (False, 1)])
async def test_failure_rolls_back_staged_writes_and_other_exits_still_run(risk_env, monkeypatch, busy, attempts):
    scheduler, factory, payload = risk_env
    calls = []
    async def monitor(db, *, account_name, trigger):
        calls.append(account_name)
        account = await db.scalar(select(PaperAccount).where(PaperAccount.account_name == account_name))
        db.add(PaperNav(account_id=account.id, trade_date=AT.date(), nav=1))
        await db.flush()
        if account_name == "reversal":
            if busy:
                raise OperationalError("fixture", {}, sqlite3.OperationalError("database is locked"))
            raise ValueError("fixture non-retryable error")
        await db.commit()
    monkeypatch.setattr(paper, "run_paper_position_risk_monitor", monitor)
    with pytest.raises(RuntimeError, match="跳过新增候选开仓"):
        await scheduler._run_quote_round_position_risk(payload)
    assert calls == ["reversal"] * attempts + ["challenger_a"]
    health = scheduler.get_pipeline_runtime_status()["paper_position_risk"]
    assert health["status"] == "blocked"
    assert health["accounts"][0]["attempts"] == attempts
    assert health["accounts"][1]["status"] == "ok"
    # Returned health leaves cannot mutate the actual monitoring state.
    health["accounts"][0]["status"] = "tampered"
    assert scheduler._paper_position_risk_health["accounts"][0]["status"] == "failed"
    async with factory() as db:
        rows = (await db.execute(select(PaperAccount.account_name).join(
            PaperNav, PaperNav.account_id == PaperAccount.id))).scalars().all()
        assert rows == ["challenger_a"]
    assert scheduler._last_quote_round_processed_at is None


@pytest.mark.asyncio
async def test_watchdog_does_not_dispatch_entries_after_risk_failure(risk_env, monkeypatch):
    scheduler, _, payload = risk_env
    monkeypatch.setattr(paper.trade_calendar, "get_trade_session", lambda: "morning")
    monkeypatch.setattr(scheduler, "_latest_healthy_quote_payload", AsyncMock(return_value=payload))
    monkeypatch.setattr(paper, "run_paper_position_risk_monitor", AsyncMock(side_effect=ValueError("failed")))
    entries = AsyncMock()
    monkeypatch.setattr(scheduler, "_run_paper_accounts_isolated", entries)
    await scheduler._paper_intraday_auto_trade()
    assert entries.await_count == 0
    assert scheduler._last_quote_round_processed_at is None
    assert scheduler._last_quote_round_processed_id is None
    assert scheduler._paper_auto_trading is False
    assert scheduler._paper_position_risk_health["status"] == "blocked"


@pytest.mark.asyncio
async def test_cancellation_rolls_back_and_restores_context(risk_env, monkeypatch):
    scheduler, factory, payload = risk_env
    async def monitor(db, **_kwargs):
        db.add(PaperNav(account_id=1, trade_date=AT.date(), nav=1))
        await db.flush()
        raise asyncio.CancelledError()
    monkeypatch.setattr(paper, "run_paper_position_risk_monitor", monitor)
    outer = {"round_id": "outer"}
    token = paper._QUOTE_ROUND_CONTEXT.set(outer)
    try:
        with pytest.raises(asyncio.CancelledError):
            await scheduler._run_quote_round_position_risk(payload)
        assert paper._quote_round_context() == outer
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)
    assert scheduler._paper_position_risk_health["status"] == "canceled"
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(PaperNav)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_clock", [
    None, AT+timedelta(seconds=1), AT-timedelta(hours=1),
    AT-timedelta(seconds=276), AT-timedelta(seconds=300),  # 上午两段真实休眠长度
])
async def test_missing_future_or_stale_round_never_replayed(risk_env, monkeypatch, bad_clock):
    scheduler, _, payload = risk_env
    payload["committed_at"] = bad_clock
    monitor = AsyncMock()
    monkeypatch.setattr(paper, "run_paper_position_risk_monitor", monitor)
    with pytest.raises(RuntimeError, match="跳过新增候选开仓"):
        await scheduler._run_quote_round_position_risk(payload)
    assert monitor.await_count == 0


@pytest.mark.asyncio
async def test_retry_cannot_refresh_an_expired_quote_clock(risk_env, monkeypatch):
    scheduler, _, payload = risk_env
    calls = []
    async def monitor(db, *, account_name, trigger):
        calls.append(account_name)
        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return AT+timedelta(hours=1)
        monkeypatch.setattr(scheduler_module, "datetime", Later)
        raise OperationalError("fixture", {}, sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(paper, "run_paper_position_risk_monitor", monitor)
    with pytest.raises(RuntimeError):
        await scheduler._run_quote_round_position_risk(payload)
    assert calls == ["reversal"]
    assert payload["committed_at"] == AT


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_event_loop_failure_keeps_shadow_evidence_but_never_dispatches_entries(
    risk_env, monkeypatch, failed,
):
    scheduler, _, payload = risk_env
    scheduler.scheduler = SimpleNamespace(running=True)
    scheduler._quote_round_payload = payload
    scheduler._quote_round_event.set()
    risk = AsyncMock(side_effect=ValueError("failed") if failed else None)
    entries = AsyncMock(return_value={"status": "completed"})
    shadow_modes = []
    async def shadows(_payload, *, execute_challengers=True):
        shadow_modes.append(execute_challengers)
        scheduler.scheduler.running = False
        return {"status": "completed" if execute_challengers else "evidence_only"}
    monkeypatch.setattr(scheduler, "_run_quote_round_position_risk", risk)
    monkeypatch.setattr(scheduler, "_run_paper_accounts_isolated", entries)
    monkeypatch.setattr(scheduler, "_process_quote_round_shadow", shadows)
    await scheduler._quote_round_loop()
    assert shadow_modes == [not failed]
    assert entries.await_count == int(not failed)
    assert scheduler._last_quote_round_processed_id == (None if failed else payload["round_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("execute", [False, True])
async def test_shadow_only_mode_never_calls_challenger_account_executor(risk_env, monkeypatch, execute):
    from app.paper import momentum_retest_shadow, strategy_iteration_shadow, strategy_iteration_challenger
    scheduler, _, payload = risk_env
    first = AsyncMock(return_value={})
    shapes = AsyncMock(return_value={})
    execute_accounts = AsyncMock(return_value={"status": "completed"})
    monkeypatch.setattr(momentum_retest_shadow, "scan_momentum_retest_shadow", first)
    monkeypatch.setattr(strategy_iteration_shadow, "scan_strategy_iteration_shadow", shapes)
    monkeypatch.setattr(strategy_iteration_challenger, "run_strategy_iteration_challenger_accounts", execute_accounts)
    await scheduler._process_quote_round_shadow(payload, execute_challengers=execute)
    assert first.await_count == shapes.await_count == 1
    assert execute_accounts.await_count == int(execute) * len(paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE)


def _frame(index, *, at=AT, quality="ok"):
    return {
        "round_id": f"frame-{index}", "committed_at": at + timedelta(seconds=index * 30),
        "quality_status": quality, "records": [{"code": "600001", "frame": index}],
    }


@pytest.mark.asyncio
async def test_coalesced_trading_payload_does_not_coalesce_a2_evidence(risk_env, monkeypatch):
    from app.paper import momentum_retest_shadow, strategy_iteration_shadow, strategy_iteration_challenger
    scheduler, _, _ = risk_env
    frames = [_frame(i) for i in range(3)]
    first = AsyncMock(return_value={})
    shapes = AsyncMock(return_value={})
    executor = AsyncMock(return_value={"status": "completed"})
    monkeypatch.setattr(momentum_retest_shadow, "scan_momentum_retest_shadow", first)
    monkeypatch.setattr(strategy_iteration_shadow, "scan_strategy_iteration_shadow", shapes)
    monkeypatch.setattr(strategy_iteration_challenger, "run_strategy_iteration_challenger_accounts", executor)
    for frame in frames:
        scheduler._publish_quote_round(frame)
    assert scheduler._quote_round_payload is frames[-1]
    await scheduler._process_quote_round_shadow(frames[-1])
    assert [call.args[1] for call in first.await_args_list] == [f["records"] for f in frames]
    assert [call.args[2] for call in first.await_args_list] == [f["committed_at"] for f in frames]
    assert all(call.kwargs["coverage_loss"] is None for call in first.await_args_list)
    # 历史帧只给A2纯行情路径，绝不重放资金/板块扫描或任何隔离账户订单。
    assert shapes.await_count == 1
    assert executor.await_count == len(paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE)
    assert all(call.kwargs["now"] == frames[-1]["committed_at"] for call in executor.await_args_list)
    assert {call.kwargs["account_name"] for call in executor.await_args_list} == set(paper.PAPER_CHALLENGER_ACCOUNT_BY_ROUTE.values())
    assert shapes.await_args.args[1] is frames[-1]["records"]
    assert not scheduler._momentum_quote_inbox
    await scheduler._drain_momentum_quote_rounds(frames[-1])
    assert first.await_count == 3


@pytest.mark.asyncio
async def test_frames_published_while_draining_cannot_cross_current_payload(risk_env, monkeypatch):
    from app.paper import momentum_retest_shadow
    scheduler, _, _ = risk_env
    frames = [_frame(i) for i in range(3)]
    seen = []
    async def scan(db, records, observed_at, **kwargs):
        seen.append(records[0]["frame"])
        if len(seen) == 1:
            scheduler._publish_quote_round(frames[2])
        return {}
    monkeypatch.setattr(momentum_retest_shadow, "scan_momentum_retest_shadow", scan)
    for frame in frames[:2]:
        scheduler._publish_quote_round(frame)
    await scheduler._drain_momentum_quote_rounds(frames[1])
    assert seen == [0, 1]
    assert len(scheduler._momentum_quote_inbox) == 1
    await scheduler._drain_momentum_quote_rounds(frames[2])
    assert seen == [0, 1, 2]


@pytest.mark.asyncio
async def test_failed_evidence_commit_is_retried_before_later_frames(risk_env, monkeypatch):
    from app.paper import momentum_retest_shadow
    scheduler, _, _ = risk_env
    frames = [_frame(i) for i in range(3)]
    scan = AsyncMock(side_effect=[ValueError("isolated DB failure"), {}, {}, {}])
    monkeypatch.setattr(momentum_retest_shadow, "scan_momentum_retest_shadow", scan)
    for frame in frames[:2]:
        scheduler._publish_quote_round(frame)
    await scheduler._drain_momentum_quote_rounds(frames[1])
    assert scan.await_count == 1
    assert scheduler._momentum_quote_inflight["payload"] is frames[0]
    scheduler._publish_quote_round(frames[2])
    await scheduler._drain_momentum_quote_rounds(frames[2])
    assert [call.args[1][0]["frame"] for call in scan.await_args_list] == [0, 0, 1, 2]
    assert scheduler._momentum_quote_inflight is None


@pytest.mark.asyncio
async def test_inbox_is_bounded_and_overflow_is_never_silent(risk_env, monkeypatch):
    from app.paper import momentum_retest_shadow
    scheduler, _, _ = risk_env
    monkeypatch.setattr(scheduler_module.settings, "PAPER_MOMENTUM_RETEST_QUOTE_INBOX_MAX_BATCHES", 2)
    frames = [_frame(i) for i in range(5)]
    scan = AsyncMock(return_value={})
    monkeypatch.setattr(momentum_retest_shadow, "scan_momentum_retest_shadow", scan)
    for frame in frames:
        scheduler._publish_quote_round(frame)
        assert len(scheduler._momentum_quote_inbox) <= 2
    await scheduler._drain_momentum_quote_rounds(frames[-1])
    assert [call.args[1][0]["frame"] for call in scan.await_args_list] == [3, 4]
    loss = scan.await_args_list[0].kwargs["coverage_loss"]
    assert loss == {
        "reason": "consumer_inbox_overflow", "first_dropped_round_id": "frame-0",
        "last_dropped_round_id": "frame-2", "dropped_rounds": 3,
    }


@pytest.mark.asyncio
async def test_old_day_duplicate_and_out_of_order_frames_cannot_rewind_a2(risk_env, monkeypatch):
    from app.paper import momentum_retest_shadow
    scheduler, _, _ = risk_env
    monkeypatch.setattr(scheduler_module.settings, "PAPER_MOMENTUM_RETEST_QUOTE_INBOX_MAX_BATCHES", 2)
    old, current = _frame(0, at=AT - timedelta(days=1)), _frame(1)
    scan = AsyncMock(return_value={})
    monkeypatch.setattr(momentum_retest_shadow, "scan_momentum_retest_shadow", scan)
    for frame in [old, current, current, old]:
        scheduler._publish_quote_round(frame)
    await scheduler._drain_momentum_quote_rounds(current)
    assert scan.await_count == 1
    assert scan.await_args.args[2] == current["committed_at"]
    assert scan.await_args.kwargs["coverage_loss"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("quality", ["ok", "degraded"])
async def test_trade_dedup_or_quality_block_does_not_skip_a2_audit(risk_env, monkeypatch, quality):
    from app.paper import momentum_retest_shadow
    scheduler, _, _ = risk_env
    payload = _frame(0, quality=quality)
    scheduler.scheduler = SimpleNamespace(running=True)
    scheduler._last_quote_round_processed_id = payload["round_id"]
    scheduler._publish_quote_round(payload)
    risk, entries, expiry = AsyncMock(), AsyncMock(), AsyncMock()
    seen = []
    async def scan(db, records, observed_at, **kwargs):
        seen.append(kwargs["coverage_loss"])
        scheduler.scheduler.running = False
        return {}
    monkeypatch.setattr(momentum_retest_shadow, "scan_momentum_retest_shadow", scan)
    monkeypatch.setattr(scheduler, "_run_quote_round_position_risk", risk)
    monkeypatch.setattr(scheduler, "_run_paper_accounts_isolated", entries)
    monkeypatch.setattr(scheduler, "_expire_pending_paper_buys", expiry)
    await scheduler._quote_round_loop()
    assert len(seen) == 1
    assert (seen[0] is None) == (quality == "ok")
    assert risk.await_count == entries.await_count == 0
    assert expiry.await_count == int(quality != "ok")
