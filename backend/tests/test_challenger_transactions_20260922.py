"""Real isolated SQLite WAL contention plus route/ranking/idempotency contracts."""
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import paper
from app.models.paper import PaperAccount, PaperAutoTradeLog, PaperPosition, PaperTradeLog
from app.models.trading import TradeOrder
from app.paper import strategy_iteration_challenger as c
from test_strategy_iteration_challenger import challenger_env, _seed_confirmed, ROUTE_B, ROUTE_C
from challenger_execution_fixture import qualified_challenger_execution
from test_paper_buy_points import setup as push_setup
from app.push import paper_buy_points as points


@pytest_asyncio.fixture
async def wal_store(tmp_path):
    path = tmp_path / "real-wal.db"
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    connection.execute("CREATE TABLE counter (id INTEGER PRIMARY KEY, value INTEGER)")
    connection.execute("INSERT INTO counter VALUES (1, 0)")
    connection.commit()
    connection.close()
    engine = create_async_engine("sqlite+aiosqlite:///" + str(path),
                                 connect_args={"timeout": 0.05})
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield path, maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_real_wal_snapshot_upgrade_fails_even_after_writer_finishes(wal_store):
    _, maker = wal_store
    async with maker() as reader, maker() as writer:
        await reader.execute(text("BEGIN"))
        assert await reader.scalar(text("SELECT value FROM counter")) == 0
        await writer.execute(text("UPDATE counter SET value=1"))
        await writer.commit()
        # Ordinary savepoints do not repair a stale outer read snapshot.
        with pytest.raises(OperationalError) as caught:
            async with reader.begin_nested():
                await reader.execute(text("UPDATE counter SET value=value+1"))
        assert caught.value.orig.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
        await reader.rollback()  # Owner, never a low-level logger, rolls back.
    async with maker() as db:
        assert await db.scalar(text("SELECT value FROM counter")) == 1


@pytest.mark.asyncio
async def test_clean_phase_drops_stale_snapshot_then_reserves_writer(wal_store):
    _, maker = wal_store
    async with maker() as reader, maker() as writer:
        await reader.execute(text("BEGIN"))
        await reader.execute(text("SELECT value FROM counter"))
        await writer.execute(text("UPDATE counter SET value=1"))
        await writer.commit()
        await c._begin_challenger_write(reader)
        assert await reader.scalar(text("SELECT value FROM counter")) == 1
        async with reader.begin_nested():
            await reader.execute(text("UPDATE counter SET value=value+1"))
        # Reservation remains owned outside the notification SAVEPOINT.
        with pytest.raises(OperationalError) as busy:
            await writer.execute(text("UPDATE counter SET value=99"))
        assert busy.value.orig.sqlite_errorcode == sqlite3.SQLITE_BUSY
        await writer.rollback()
        await reader.commit()
        await writer.execute(text("UPDATE counter SET value=value+1"))
        await writer.commit()
    async with maker() as db:
        assert await db.scalar(text("SELECT value FROM counter")) == 3


@pytest.mark.asyncio
async def test_writer_busy_propagates_and_only_owner_retries(wal_store):
    _, maker = wal_store
    async with maker() as holder, maker() as scanner:
        await holder.execute(text("BEGIN IMMEDIATE"))
        with pytest.raises(OperationalError) as caught:
            await c._begin_challenger_write(scanner)
        assert caught.value.orig.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert scanner.in_transaction()  # Helper did not secretly roll back.
        await scanner.rollback()
        await holder.rollback()
        await c._begin_challenger_write(scanner)
        await scanner.execute(text("UPDATE counter SET value=7"))
        await scanner.commit()
    async with maker() as db:
        assert await db.scalar(text("SELECT value FROM counter")) == 7


@pytest.mark.asyncio
async def test_boundary_refuses_dirty_caller_work_without_commit_or_rollback(wal_store):
    _, maker = wal_store
    async with maker() as db:
        row = PaperAutoTradeLog(run_id="uncommitted", trade_date=datetime(2026,9,22).date(),
                               action="wait_buy", decision="wait")
        db.add(row)
        with pytest.raises(RuntimeError, match="clean owned session"):
            await c._begin_challenger_write(db)
        assert row in db.new
        await db.rollback()


@pytest.mark.asyncio
async def test_single_account_scope_does_not_initialize_other_routes(challenger_env, monkeypatch):
    monkeypatch.setattr(paper.settings, "PAPER_CHALLENGER_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(paper, "_paper_order_window_status",
                        _async_value((True, "")))
    async with challenger_env() as db:
        result = await c.run_strategy_iteration_challenger_accounts(
            db, now=datetime(2026,9,22,10), account_name="challenger_b")
        assert result["status"] == "completed"
        assert result["failed_accounts"] == []
        assert result["accounts"] == ["challenger_b"]
        accounts = list((await db.scalars(select(PaperAccount))).all())
        assert [a.account_name for a in accounts] == ["challenger_b"]
        logs = list((await db.scalars(select(PaperAutoTradeLog))).all())
        assert len(logs) == 1 and logs[0].action == "scan"
        await db.rollback()
        with pytest.raises(ValueError, match="unknown"):
            await c.run_strategy_iteration_challenger_accounts(
                db, now=datetime(2026,9,22,10), account_name="default")


@pytest.mark.asyncio
async def test_disabled_and_window_skip_never_claim_completed(challenger_env, monkeypatch):
    async with challenger_env() as db:
        monkeypatch.setattr(paper.settings, "PAPER_CHALLENGER_ACCOUNT_ENABLED", False)
        result = await c.run_strategy_iteration_challenger_accounts(
            db, now=datetime(2026,9,22,10), account_name="challenger_b")
        assert result["status"] == "skipped" and not result["enabled"]
        monkeypatch.setattr(paper.settings, "PAPER_CHALLENGER_ACCOUNT_ENABLED", True)
        monkeypatch.setattr(paper, "_paper_order_window_status", _async_value((False, "closed")))
        result = await c.run_strategy_iteration_challenger_accounts(
            db, now=datetime(2026,9,22,10), account_name="challenger_b")
        assert result["status"] == "skipped" and result["reason"] == "closed"
        assert await db.scalar(select(func.count()).select_from(PaperAccount)) == 0


def _async_value(value):
    async def get(*args, **kwargs):
        return value
    return get


@pytest.mark.asyncio
async def test_ranked_batch_survives_boundaries_and_retry_does_not_duplicate_fill(
    challenger_env, qualified_challenger_execution, monkeypatch,
):
    at = datetime(2026,9,1,9,40)
    monkeypatch.setattr(paper, "_strategy_buy_limits", lambda name: (1, 1))
    monkeypatch.setattr(paper, "_risk_check_for_buy",
                        _async_value({"final_level":"pass", "block_reasons":[], "warnings":[]}))
    async with challenger_env() as db:
        low = await _seed_confirmed(db, code="600107", route_id=ROUTE_C, now=at,
                                   price=10.03, ask=10.04, volume_ratio=.8,
                                   orderbook_imbalance=-.1)
        high = await _seed_confirmed(db, code="600108", route_id=ROUTE_C, now=at,
                                    price=10.20, ask=10.21, volume_ratio=2.5,
                                    orderbook_imbalance=1.)
        keys_before = [low.event_key, high.event_key]
        first = await qualified_challenger_execution(
            db, now=at, account_name="challenger_c")
        second = await qualified_challenger_execution(
            db, now=at, account_name="challenger_c")
        assert first["entries"] == 1 and second["entries"] == 0
        positions = list((await db.scalars(select(PaperPosition))).all())
        assert [p.code for p in positions] == ["600108"]
        assert await db.scalar(select(func.count()).select_from(PaperTradeLog)) == 1
        assert [low.event_key, high.event_key] == keys_before
        logs = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.action=="buy"))).all())
        import json
        assert json.loads(logs[0].candidate_json)["pre_entry_rank_within_route"] == 1


@pytest.mark.asyncio
async def test_post_commit_audit_failure_never_resubmits_committed_order(
    challenger_env, qualified_challenger_execution, monkeypatch,
):
    at = datetime(2026,9,1,10)
    monkeypatch.setattr(paper, "_risk_check_for_buy",
                        _async_value({"final_level":"pass", "block_reasons":[], "warnings":[]}))
    real_log = c._record_terminal_log
    async def failed_audit(db, **kwargs):
        if kwargs["action"] == "buy":
            raise OperationalError("audit insert", {}, sqlite3.OperationalError("database is locked"))
        return await real_log(db, **kwargs)
    async with challenger_env() as db:
        await _seed_confirmed(db, code="600100", route_id=ROUTE_B, now=at)
        monkeypatch.setattr(c, "_record_terminal_log", failed_audit)
        with pytest.raises(OperationalError):
            await qualified_challenger_execution(db, now=at, account_name="challenger_b")
        await db.rollback()
    monkeypatch.setattr(c, "_record_terminal_log", real_log)
    async with challenger_env() as db:
        again = await qualified_challenger_execution(db, now=at, account_name="challenger_b")
        assert again["entries"] == 0
        assert await db.scalar(select(func.count()).select_from(TradeOrder)) == 1
        assert await db.scalar(select(func.count()).select_from(PaperTradeLog)) == 1
        assert await db.scalar(select(func.count()).select_from(PaperPosition)) == 1


@pytest.mark.asyncio
async def test_second_event_writer_busy_preserves_first_fill_and_remaining_rank(
    challenger_env, qualified_challenger_execution, monkeypatch,
):
    at = datetime(2026,9,1,9,40)
    monkeypatch.setattr(paper, "_strategy_buy_limits", lambda name: (2, 2))
    monkeypatch.setattr(paper, "_risk_check_for_buy",
                        _async_value({"final_level":"pass", "block_reasons":[], "warnings":[]}))
    async with challenger_env() as setup:
        await setup.execute(text("PRAGMA journal_mode=WAL"))
        await setup.commit()
        await _seed_confirmed(setup, code="600107", route_id=ROUTE_C, now=at,
                              price=10.05, ask=10.06, volume_ratio=1.2,
                              orderbook_imbalance=.15)
        await _seed_confirmed(setup, code="600108", route_id=ROUTE_C, now=at,
                              price=10.20, ask=10.21, volume_ratio=2.5,
                              orderbook_imbalance=1.)
    real_begin = c._begin_challenger_write
    real_log = c._record_terminal_log
    real_processed = c._already_processed
    first_filled = False
    awaiting_low = False
    injected = False
    seen_buy_ranks = []
    async with challenger_env() as holder:
        async def observe_fill(db, **kwargs):
            nonlocal first_filled
            result = await real_log(db, **kwargs)
            if kwargs["action"] == "buy":
                seen_buy_ranks.append((kwargs["event"].code,
                                      kwargs["candidate"]["pre_entry_rank_within_route"]))
                first_filled = True
            return result
        async def processed(db, **kwargs):
            nonlocal awaiting_low
            if first_filled:
                awaiting_low = True
            return await real_processed(db, **kwargs)
        async def reserve(db):
            nonlocal injected
            if first_filled and awaiting_low and not injected:
                injected = True
                await holder.execute(text("BEGIN IMMEDIATE"))
                await holder.execute(text("UPDATE paper_account SET total_return=total_return"))
            await real_begin(db)
        monkeypatch.setattr(c, "_record_terminal_log", observe_fill)
        monkeypatch.setattr(c, "_already_processed", processed)
        monkeypatch.setattr(c, "_begin_challenger_write", reserve)
        async with challenger_env() as db:
            await db.execute(text("PRAGMA busy_timeout=50"))
            with pytest.raises(OperationalError) as busy:
                await qualified_challenger_execution(db, now=at, account_name="challenger_c")
            assert busy.value.orig.sqlite_errorcode == sqlite3.SQLITE_BUSY
            await db.rollback()  # Only scheduler-like owner retries.
        await holder.rollback()
    assert injected and seen_buy_ranks == [("600108", 1)]
    monkeypatch.setattr(c, "_begin_challenger_write", real_begin)
    async with challenger_env() as db:
        assert await db.scalar(select(func.count()).select_from(PaperTradeLog)) == 1
        replay = await qualified_challenger_execution(db, now=at, account_name="challenger_c")
        assert replay["entries"] == 1
        assert seen_buy_ranks == [("600108", 1), ("600107", 2)]
        assert await db.scalar(select(func.count()).select_from(TradeOrder)) == 2
        assert await db.scalar(select(func.count()).select_from(PaperTradeLog)) == 2
        assert await db.scalar(select(func.count()).select_from(PaperPosition)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired", [False, True], ids=["original-failure-preserved", "clean-boundary"])
async def test_actual_notification_savepoint_then_capacity_log_on_stale_wal(push_setup, repaired):
    maker, at, send = push_setup
    async with maker() as db:
        assert await db.scalar(text("PRAGMA journal_mode=WAL")) == "wal"
        await db.commit()
    account = SimpleNamespace(id=1, account_name="challenger_c", status="active")
    event = SimpleNamespace(trade_date=at.date(), route_id=ROUTE_C, code="600001",
                            name="隔离测试")
    async with maker() as scanner, maker() as writer:
        await scanner.execute(text("BEGIN"))
        await scanner.execute(text("SELECT code FROM stock_tags"))
        await writer.execute(text("UPDATE stock_tags SET name='independent commit' WHERE code='600001'"))
        await writer.commit()
        if repaired:
            await c._begin_challenger_write(scanner)
        queued = await points.record_buy_point(
            scanner, account=account, strategy_version="test-only", label="isolated",
            code=event.code, name=event.name, source=event.route_id, signal_key="wal-event",
            reason="confirmed", price=10.5, observed_at=at, decision_run_id="wal-decision",
            quote_round_id="test-round", as_of_at=at)
        kwargs = dict(paper=paper, account_id=1, event=event, run_id="wal-decision",
                      action="wait_buy", decision="wait",
                      reason="本轮达到单日开仓数或最大持仓数",
                      price=10.5, amount=None, candidate={"decision_round_id":"test-round"})
        if repaired:
            assert queued is not None
            assert points._FAILED_INGRESS not in scanner.info
            await c._record_terminal_log(scanner, **kwargs)
        else:
            assert queued is None and scanner.info[points._FAILED_INGRESS]
            with pytest.raises(OperationalError) as failed:
                await c._record_terminal_log(scanner, **kwargs)
            assert failed.value.orig.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
            await scanner.rollback()
    async with maker() as db:
        rows = list((await db.scalars(select(PaperAutoTradeLog))).all())
        assert len(rows) == (2 if repaired else 0)
        if repaired:
            assert {r.action for r in rows} == {"buy_signal", "wait_buy"}
    send.assert_not_awaited()
