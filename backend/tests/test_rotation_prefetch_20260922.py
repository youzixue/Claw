"""Bound ranking existence reads without changing legacy last-write behavior."""
import asyncio
import json
from dataclasses import replace
from datetime import date
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event,select,text
from sqlalchemy.ext.asyncio import AsyncSession,async_sessionmaker,create_async_engine

from app.models.sector import SectorStrength
from app.risk.rotation import SectorRankItem,SectorRotationEngine


async def legacy_write(db,items,day):
    # Oracle frozen from the original production writer; commit is intentionally
    # inside it, including the empty-list case.
    for item in items:
        row=(await db.execute(select(SectorStrength).where(
            SectorStrength.trade_date==day,SectorStrength.sector_code==item.sector_code
        ))).scalar_one_or_none()
        if row:
            row.rank=item.rank
            row.rank_change=item.rank_change
            row.strength_score=item.strength_score
            row.consecutive_days=item.consecutive_days
            row.fund_flow=item.fund_flow
            row.sector_type=item.sector_type
            row.change_pct=getattr(item,"change_pct",None)
            row.limit_up_count=getattr(item,"limit_up_count",None)
        else:
            db.add(SectorStrength(trade_date=day,sector_code=item.sector_code,
                sector_name=item.sector_name,sector_type=item.sector_type,rank=item.rank,
                rank_change=item.rank_change,strength_score=item.strength_score,
                change_pct=getattr(item,"change_pct",None),fund_flow=item.fund_flow,
                limit_up_count=getattr(item,"limit_up_count",None),
                consecutive_days=item.consecutive_days,
                is_hot=1 if (item.fund_flow or 0)>10 or (item.consecutive_days or 0)>=3 else 0))
    await db.commit()
    return len(items)


def items(n):
    return [SectorRankItem(sector_code=f"sector{i:04}",sector_name=f"新名称{i}",
        sector_type="concept",rank=i+1,rank_change=i-2,strength_score=i%100,
        change_pct=i%4-2,fund_flow=i%14,limit_up_count=i%5,consecutive_days=i%4)
        for i in range(n)]


@pytest_asyncio.fixture
async def env(tmp_path):
    engine=create_async_engine("sqlite+aiosqlite:///"+str(tmp_path/"rotation.db"))
    async with engine.begin() as db:
        await db.run_sync(SectorStrength.__table__.create)
        await db.execute(text("CREATE TABLE marker(id INTEGER PRIMARY KEY)"))
    factory=async_sessionmaker(engine,expire_on_commit=False)
    try:yield engine,factory
    finally:await engine.dispose()


async def reset(db,batch,mode):
    await db.execute(text("DELETE FROM sector_strength"));await db.commit()
    for i,item in enumerate(batch):
        if mode=="new" or (mode=="mixed" and i%2):continue
        db.add(SectorStrength(trade_date=date(2026,9,22),sector_code=item.sector_code,
            sector_name=f"旧名称{i}",sector_type="old",rank=999,rank_change=-99,
            strength_score=-1,change_pct=-1,fund_flow=-1,limit_up_count=-1,
            consecutive_days=-1,is_hot=i%2))
    await db.commit()


async def body(db):
    result=(await db.execute(select(SectorStrength.__table__).order_by(SectorStrength.id))).mappings()
    return json.dumps([dict(r) for r in result],sort_keys=True,ensure_ascii=False,
        separators=(",",":"),default=str).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["existing","new","mixed"])
@pytest.mark.parametrize("duplicate", [False,True])
async def test_full_row_bytes_and_duplicate_first_insert_last_update_contract(env,mode,duplicate):
    engine,factory=env;batch=items(41)
    calls=batch+[replace(batch[i],sector_name="不得覆盖首次名称",rank=100+i,
                       sector_type="industry",fund_flow=88,consecutive_days=8)
                 for i in (0,20,40)] if duplicate else batch
    async with factory() as db:
        await reset(db,batch,mode);assert await legacy_write(db,calls,date(2026,9,22))==len(calls)
        expected=await body(db);await reset(db,batch,mode)
        commit=AsyncMock(wraps=db.commit);db.commit=commit
        count=await SectorRotationEngine().save_strength_ranking(db,calls,date(2026,9,22))
        assert count==len(calls)
        assert await body(db)==expected
        assert commit.await_count==1


@pytest.mark.asyncio
@pytest.mark.parametrize("n",[0,1,500,501,1001])
async def test_existence_queries_are_bounded_by_unique_codes_not_items(env,n):
    engine,factory=env;batch=items(n);queries=[]
    def before(conn,cursor,statement,parameters,context,executemany):
        if "FROM sector_strength" in statement:queries.append(statement)
    event.listen(engine.sync_engine,"before_cursor_execute",before)
    try:
        async with factory() as db:
            await SectorRotationEngine().save_strength_ranking(db,batch,date(2026,9,22))
        assert len(queries)==(n+499)//500
    finally:event.remove(engine.sync_engine,"before_cursor_execute",before)


@pytest.mark.asyncio
async def test_empty_input_still_commits_caller_pending_work_once(env):
    engine,factory=env
    async with factory() as db:
        await db.execute(text("INSERT INTO marker VALUES(1)"))
        commit=AsyncMock(wraps=db.commit);db.commit=commit
        assert await SectorRotationEngine().save_strength_ranking(db,[],date(2026,9,22))==0
        assert commit.await_count==1
    async with factory() as db:assert await db.scalar(text("SELECT id FROM marker"))==1


@pytest.mark.asyncio
async def test_duplicate_new_identity_under_caller_no_autoflush_keeps_first_name_and_hot(env):
    engine,factory=env;first=items(1)[0]
    second=replace(first,sector_name="second",fund_flow=99,consecutive_days=9,rank=2)
    async with factory() as db:
        with db.no_autoflush:
            assert await SectorRotationEngine().save_strength_ranking(db,[first,second],date(2026,9,22))==2
        row=(await db.scalars(select(SectorStrength))).one()
        assert (row.sector_name,row.is_hot,row.fund_flow,row.rank)==(first.sector_name,0,99,2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure",["query","commit","cancel_commit"])
async def test_errors_and_cancel_propagate_without_implicit_rollback_or_commit_retry(env,failure):
    engine,factory=env;batch=items(10)
    async with factory() as db:
        original_commit=db.commit;original_execute=db.execute
        if failure=="query":
            db.execute=AsyncMock(side_effect=ValueError("query"))
        else:
            db.commit=AsyncMock(side_effect=asyncio.CancelledError() if failure=="cancel_commit" else ValueError("commit"))
        with pytest.raises(asyncio.CancelledError if failure=="cancel_commit" else ValueError):
            await SectorRotationEngine().save_strength_ranking(db,batch,date(2026,9,22))
        db.execute=original_execute;db.commit=original_commit
        await db.rollback()
    async with factory() as db:assert not (await db.scalars(select(SectorStrength))).all()
