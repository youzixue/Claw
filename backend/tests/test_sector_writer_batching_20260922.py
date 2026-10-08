"""Scheduler-only deferral of ranking autoflush; preserve original row semantics."""
import asyncio
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.data import scheduler as module
from app.models.sector import SectorStrength
from app.risk.rotation import SectorRankItem, SectorRotationEngine


def rank_items(n=40):
    return [SectorRankItem(sector_code=f"s{i:04}", sector_name=f"新名称{i}",
        sector_type="concept" if i % 2 else "industry", rank=n-i, rank_change=i-7,
        strength_score=round(i * 1.7, 1), change_pct=i/10-2, fund_flow=i/5-4,
        limit_up_count=i % 5, consecutive_days=i % 4) for i in range(n)]


async def seed(db, items, mode="existing"):
    for i, item in enumerate(items):
        if mode == "new" or (mode == "mixed" and i % 2):
            continue
        db.add(SectorStrength(trade_date=date.today(), sector_code=item.sector_code,
            sector_name=f"旧名称{i}", sector_type="old", rank=999,
            rank_change=99, strength_score=-1, change_pct=-1, fund_flow=-1,
            limit_up_count=-1, consecutive_days=-1, is_hot=i % 2))
    await db.commit()


async def serialized(db):
    rows = (await db.execute(select(SectorStrength.__table__).order_by(
        SectorStrength.id))).mappings().all()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), default=str).encode()


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path/"sector.db"))
    async with engine.begin() as conn:
        await conn.run_sync(SectorStrength.__table__.create)
        await conn.execute(text("CREATE TABLE latency_probe(id INTEGER PRIMARY KEY, n INTEGER)"))
        await conn.execute(text("INSERT INTO latency_probe VALUES(1,0)"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(module, "async_session", sessions)
    monkeypatch.setattr(module.trade_calendar, "is_trading_hours", AsyncMock(return_value=True))
    scheduler = module.DataScheduler()
    async def update(db, day):
        await db.execute(text("UPDATE latency_probe SET n=n+1 WHERE id=1"))
    scheduler._update_sector_persistence = update
    restored = []
    async def later(db, day):
        restored.append(db.autoflush)
    scheduler._compute_lifecycle = later
    scheduler._prewarm_lifecycle_snapshot = AsyncMock()
    try:
        yield SimpleNamespace(engine=engine,sessions=sessions,scheduler=scheduler,restored=restored)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["existing","new","mixed","empty"])
async def test_row_bytes_match_original_writer_and_update_roundtrips_are_batched(env, monkeypatch, mode):
    items = rank_items(40 if mode != "empty" else 0)
    async with env.sessions() as db:
        await seed(db, items, mode)
        await SectorRotationEngine().save_strength_ranking(db, items, date.today())
        expected = await serialized(db)
        await db.execute(text("DELETE FROM sector_strength"))
        await db.commit()
        await seed(db, items, mode)
    monkeypatch.setattr(SectorRotationEngine, "calc_sector_strength", AsyncMock(return_value=items))
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):
        statements.append((statement,executemany))
    event.listen(env.engine.sync_engine,"before_cursor_execute",capture)
    try:
        await env.scheduler._sector_derive()
    finally:
        event.remove(env.engine.sync_engine,"before_cursor_execute",capture)
    async with env.sessions() as db:
        assert await serialized(db) == expected
    assert env.restored == [True]
    updates = [s for s,many in statements if s.startswith("UPDATE sector_strength")]
    lookup_positions = [i for i, (s, many) in enumerate(statements) if "FROM sector_strength" in s]
    update_positions = [i for i, (s, many) in enumerate(statements) if s.startswith("UPDATE sector_strength")]
    if update_positions:
        assert min(update_positions) > max(lookup_positions), "per-item SELECT flushed a preceding UPDATE"
        # Different dirty column sets may form several executemany groups.
        assert len(updates) < len(items) // 2
    # Existing row names/is_hot are deliberately NOT recomputed by old writer.
    # Comparing every column protects that non-obvious compatibility contract.


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lookup", "commit"])
async def test_error_rolls_back_all_ranking_mutations_and_releases_owned_session(env, monkeypatch, failure):
    items=rank_items()
    async with env.sessions() as db:
        await seed(db, items)
        before=await serialized(db)
    seen=[]
    class BrokenSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if "FROM sector_strength" in str(statement):
                seen.append(1)
                if failure == "lookup" and len(seen) == 1:
                    raise ValueError("injected lookup")
            return await super().execute(statement,*args,**kwargs)
        async def commit(self):
            if failure == "commit":
                raise ValueError("injected commit")
            return await super().commit()
    monkeypatch.setattr(module,"async_session",async_sessionmaker(env.engine,
        class_=BrokenSession,expire_on_commit=False))
    monkeypatch.setattr(SectorRotationEngine,"calc_sector_strength",AsyncMock(return_value=items))
    await env.scheduler._sector_derive()
    assert env.restored == []
    async with env.sessions() as db:
        assert await serialized(db) == before
        assert await db.scalar(text("SELECT n FROM latency_probe")) == 0
        await db.execute(text("UPDATE latency_probe SET n=2 WHERE id=1"))
        await db.commit()


@pytest.mark.asyncio
async def test_cancel_is_propagated_and_no_autoflush_restores_before_close(env, monkeypatch):
    items=rank_items()
    ready=asyncio.Event();on_close=[]
    async with env.sessions() as db:
        await seed(db,items)
        before=await serialized(db)
    class PausedSession(AsyncSession):
        count=0
        async def execute(self,statement,*args,**kwargs):
            if "FROM sector_strength" in str(statement):
                self.count+=1
                if self.count==1:
                    ready.set()
                    await asyncio.Event().wait()
            return await super().execute(statement,*args,**kwargs)
        async def close(self):
            on_close.append(self.autoflush)
            await super().close()
    monkeypatch.setattr(module,"async_session",async_sessionmaker(env.engine,
        class_=PausedSession,expire_on_commit=False))
    monkeypatch.setattr(SectorRotationEngine,"calc_sector_strength",AsyncMock(return_value=items))
    task=asyncio.create_task(env.scheduler._sector_derive())
    await asyncio.wait_for(ready.wait(),3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert on_close == [True]
    async with env.sessions() as db:
        assert await serialized(db)==before
        await db.execute(text("UPDATE latency_probe SET n=2 WHERE id=1"))
        await db.commit()


@pytest.mark.asyncio
async def test_cleanup_failure_propagates_after_autoflush_is_restored(env, monkeypatch):
    items=rank_items()
    cleanup_state=[]
    class CloseFailure(AsyncSession):
        async def close(self):
            cleanup_state.append(self.autoflush)
            await super().close()
            raise RuntimeError("injected cleanup failure")
    monkeypatch.setattr(module,"async_session",async_sessionmaker(env.engine,
        class_=CloseFailure,expire_on_commit=False))
    monkeypatch.setattr(SectorRotationEngine,"calc_sector_strength",AsyncMock(return_value=items))
    with pytest.raises(RuntimeError,match="cleanup failure"):
        await env.scheduler._sector_derive()
    assert cleanup_state == [True]
    # Original writer commits before lifecycle/close: a later close error must
    # not be represented as rollback of an already committed ranking.
    async with env.sessions() as db:
        assert len((await db.scalars(select(SectorStrength))).all()) == len(items)
        await db.execute(text("UPDATE latency_probe SET n=2 WHERE id=1"))
        await db.commit()


@pytest.mark.asyncio
async def test_scheduler_yields_to_peer_between_ranking_queries(env, monkeypatch):
    items=rank_items(100)
    async with env.sessions() as db:
        await seed(db,items)
    monkeypatch.setattr(SectorRotationEngine,"calc_sector_strength",AsyncMock(return_value=items))
    ticks=[];stop=False
    async def peer():
        while not stop:
            ticks.append(1)
            await asyncio.sleep(.001)
    task=asyncio.create_task(peer())
    try:
        await env.scheduler._sector_derive()
    finally:
        stop=True
        await task
    assert len(ticks)>2
