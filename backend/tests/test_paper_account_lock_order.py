"""Deterministic lock-order regressions against an isolated real SQLite database."""
import asyncio
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import paper
from app.models.paper import PaperAccount, PaperNav
from app.models.governance import TradeCalendarModel


@pytest_asyncio.fixture
async def account_sessions(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'account-locks.db'}",
        connect_args={"timeout": .75},
    )
    async with engine.begin() as connection:
        await connection.execute(text("PRAGMA journal_mode=WAL"))
        await connection.run_sync(PaperAccount.__table__.create)
        await connection.run_sync(PaperNav.__table__.create)
        await connection.run_sync(TradeCalendarModel.__table__.create)
        # Account NAV now observes the trading calendar; keep this lock test
        # deterministic without replacing the real account initialization path.
        await connection.execute(TradeCalendarModel.__table__.insert().values(
            trade_date=datetime(2026, 9, 10).date(), is_trade_day=True))
        # Match the runtime uniqueness guard while testing first-use races.
        await connection.execute(text(
            "CREATE UNIQUE INDEX uq_active_account_test ON paper_account(account_name) WHERE status='active'"
        ))
    monkeypatch.setattr(paper, "_ACCOUNT_INIT_LOCK", asyncio.Lock())
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 10, 10)
    monkeypatch.setattr(paper, "datetime", FixedDateTime)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def seed_accounts(factory):
    async with factory() as session:
        session.add_all([
            PaperAccount(account_name=name, initial_capital=50000, current_capital=50000, status="active")
            for name in ("default", "promotion")
        ])
        await session.commit()


@pytest.mark.asyncio
async def test_existing_account_lookup_does_not_wait_for_initialization_lock(account_sessions):
    await seed_accounts(account_sessions)
    async with account_sessions() as session:
        async with paper._ACCOUNT_INIT_LOCK:
            account = await asyncio.wait_for(paper._get_or_create_account(session, "default"), .3)
            assert account.account_name == "default"


@pytest.mark.asyncio
async def test_sqlite_writer_can_finish_while_other_lookup_autoflush_waits(account_sessions):
    await seed_accounts(account_sessions)
    async with account_sessions() as writer, account_sessions() as waiter:
        first = await writer.scalar(select(PaperAccount).where(PaperAccount.account_name == "default"))
        second = await waiter.scalar(select(PaperAccount).where(PaperAccount.account_name == "promotion"))
        first.current_capital = 49000
        await writer.flush()  # Real SQLite write lock, held until writer's commit.
        second.current_capital = 48000
        waiter_autoflush = asyncio.Event()
        event.listen(waiter.sync_session, "before_flush", lambda *args: waiter_autoflush.set())

        async def waiting_lookup():
            account = await paper._get_or_create_account(waiter, "promotion")
            await waiter.commit()
            return account.account_name

        async def writer_finishes():
            await asyncio.wait_for(waiter_autoflush.wait(), 2)
            # Legacy lookup holds the Python initialization lock while the
            # waiter's autoflush waits for our SQLite transaction: a cycle.
            account = await paper._get_or_create_account(writer, "default")
            await writer.commit()
            return account.account_name

        tasks = [asyncio.create_task(waiting_lookup()), asyncio.create_task(writer_finishes())]
        try:
            results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await waiter.rollback()
            await writer.rollback()
        assert results == ["promotion", "default"], [type(x).__name__ for x in results]

    async with account_sessions() as check:
        rows = (await check.scalars(select(PaperAccount).order_by(PaperAccount.account_name))).all()
        assert [(a.account_name, a.current_capital) for a in rows] == [("default",49000),("promotion",48000)]


@pytest.mark.asyncio
async def test_parallel_first_access_still_creates_one_active_account_and_nav(account_sessions):
    async def obtain():
        async with account_sessions() as session:
            account = await paper._get_or_create_account(session, "reversal")
            return account.id
    ids = await asyncio.gather(*(obtain() for _ in range(8)))
    assert len(set(ids)) == 1
    async with account_sessions() as check:
        assert await check.scalar(select(func.count()).select_from(PaperAccount)) == 1
        assert await check.scalar(select(func.count()).select_from(PaperNav)) == 1
        account = await check.get(PaperAccount, ids[0])
        assert account.strategy == "reversal"
        assert account.initial_capital == paper.settings.PAPER_INITIAL_CAPITAL


@pytest.mark.asyncio
async def test_existing_account_lookup_does_not_commit_callers_changes(account_sessions):
    await seed_accounts(account_sessions)
    async with account_sessions() as session:
        account = await session.scalar(select(PaperAccount).where(PaperAccount.account_name == "default"))
        account.current_capital = 123
        again = await paper._get_or_create_account(session, "default")
        assert again is account and again.current_capital == 123
        await session.rollback()
    async with account_sessions() as check:
        assert await check.scalar(select(PaperAccount.current_capital).where(PaperAccount.account_name == "default")) == 50000


@pytest.mark.asyncio
async def test_archived_account_is_not_reused_as_active(account_sessions):
    async with account_sessions() as session:
        session.add(PaperAccount(account_name="reversal", initial_capital=12345, status="closed"))
        await session.commit()
        active = await paper._get_or_create_account(session, "reversal")
        assert active.status == "active" and active.initial_capital == paper.settings.PAPER_INITIAL_CAPITAL
        assert await session.scalar(select(func.count()).select_from(PaperAccount)) == 2
