import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("DEBUG", "false")

from app.dashboard2.cache import DashboardSnapshotCache
from app.dashboard2.schemas import Dashboard2Snapshot
from app.models.governance import DashboardSnapshot


@pytest.mark.asyncio
async def test_overview_snapshot_cache_overwrites_latest_row(tmp_path):
    db_path = tmp_path / "dashboard_cache.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(DashboardSnapshot.__table__.create)

    cache = DashboardSnapshotCache()
    try:
        async with session_factory() as session:
            await cache.save(session, Dashboard2Snapshot(summary_text="first"))
            await session.commit()
            await cache.save(session, Dashboard2Snapshot(summary_text="second"))
            await session.commit()

            count = (
                await session.execute(
                    select(func.count(DashboardSnapshot.id)).where(
                        DashboardSnapshot.snapshot_key == cache.SNAPSHOT_KEY
                    )
                )
            ).scalar_one()
            latest = await cache.latest(session)

        assert count == 1
        assert latest["summary_text"] == "second"
    finally:
        await engine.dispose()
