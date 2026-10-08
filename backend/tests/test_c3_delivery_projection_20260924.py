"""Equivalent C3 queue projection; temporary SQLite only, no runtime access."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.paper import PaperAutoTradeLog as Log
from app.push import paper_buy_points as points

NOW = datetime(2026, 9, 24, 22, 30)


@pytest_asyncio.fixture
async def env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'projection.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Log.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine, maker
    finally:
        await engine.dispose()


def row(i, **kw):
    return Log(**{
        "run_id": f"run:{i // 8}", "trade_date": NOW.date(),
        "created_at": NOW - timedelta(seconds=i % 19), "source": points.C3_ROUTE,
        "account_id": None, "stage_code": "c3_research_delivery",
        "action": "research_push", "decision": ("sent", "expired", "rejected", "disabled",
            "attempting", "waiting", "failed", "throttled")[i % 8],
        "code": f"{600000 + i % 10}", "candidate_json": '{"cause":"preserved"}', **kw})


def old_query(now):
    return select(Log).where(
        Log.source == points.C3_ROUTE, Log.account_id.is_(None),
        Log.stage_code.in_(("c3_research_signal", "c3_research_delivery")),
        Log.trade_date >= now.date() - timedelta(days=1), Log.created_at <= now,
    ).order_by(Log.id)


def leaves(rows):
    return [{col.name: getattr(r, col.name) for col in Log.__table__.columns} for r in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1, 399, 400, 401, 811])
async def test_full_rows_order_states_and_page_boundary_equivalent(env, count):
    _, maker = env
    async with maker() as db:
        db.add_all([row(i) for i in range(count)])
        db.add_all([
            row(9001, source="feishu"),
            row(9002, account_id=3),
            row(9003, account_id=0),
            row(9004, account_id=-1),
            row(9005, stage_code="notification_delivery"),
            row(9006, created_at=NOW + timedelta(microseconds=1)),
            row(9007, trade_date=NOW.date() - timedelta(days=2)),
            row(9008, source=None),
            row(9009, stage_code=None),
        ])
        await db.commit()
        old = leaves((await db.scalars(old_query(NOW))).all())
        new = leaves(await points._c3_delivery_rows(db, NOW))
        assert new == old and len(new) == count


@pytest.mark.asyncio
async def test_cutoff_ties_future_trade_day_and_all_versions_keep_old_semantics(env):
    _, maker = env
    async with maker() as db:
        db.add_all([
            row(1, created_at=NOW, trade_date=NOW.date()-timedelta(days=1),
                stage_code="c3_research_signal", action="research_signal"),
            row(2, created_at=NOW, trade_date=NOW.date()+timedelta(days=1), strategy_version="old"),
            row(3, created_at=NOW-timedelta(days=9), decision="sent"),
            row(4, created_at=NOW-timedelta(days=8), decision="failed"),
        ])
        await db.commit()
        got = await points._c3_delivery_rows(db, NOW)
        assert leaves(got) == leaves((await db.scalars(old_query(NOW))).all())
        assert len(got) == 4
        # Keep legacy visibility, even a future trade_date with non-future created_at.
        # Fixing data validity is not a hidden part of this performance change.


@pytest.mark.asyncio
async def test_projection_queries_payloads_only_by_bounded_primary_keys(env):
    engine, maker = env
    async with maker() as db:
        db.add_all([row(i) for i in range(811)])
        await db.commit()
    statements = []
    def observe(conn, cursor, statement, params, ctx, many):
        if statement.lstrip().startswith("SELECT"):
            statements.append((statement, params))
    event.listen(engine.sync_engine, "before_cursor_execute", observe)
    try:
        async with maker() as db:
            got = await points._c3_delivery_rows(db, NOW)
            assert len(got) == 811
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe)
    assert len(statements) == 4
    assert "candidate_json" not in statements[0][0]
    assert "account_id IS NULL" not in statements[0][0]
    assert "stage_code IN" in statements[0][0]
    for sql, params in statements[1:]:
        assert "paper_auto_trade_log.id IN" in sql and len(params) <= 400
        assert "source =" not in sql and "account_id IS NULL" not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_later_page_error_never_returns_partial_queue(env, error):
    _, maker = env
    async with maker() as db:
        db.add_all([row(i) for i in range(401)])
        await db.commit()
        calls = 0
        async def fail_page_two(statement):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise error("isolated second page")
            return await db.scalars(statement)
        proxy = SimpleNamespace(execute=db.execute, scalars=fail_page_two)
        with pytest.raises(error):
            await points._c3_delivery_rows(proxy, NOW)
        assert calls == 2


@pytest.mark.asyncio
async def test_restart_has_no_volatile_projection_watermark(env):
    _, maker = env
    async with maker() as db:
        db.add(row(1, action="research_signal", stage_code="c3_research_signal"))
        await db.commit()
        first = leaves(await points._c3_delivery_rows(db, NOW))
    async with maker() as db:
        assert leaves(await points._c3_delivery_rows(db, NOW)) == first
        db.add(row(2, decision="expired"))
        await db.commit()
    async with maker() as db:
        assert len(await points._c3_delivery_rows(db, NOW)) == 2
