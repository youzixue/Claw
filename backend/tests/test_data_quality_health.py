from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1.governance import data_source_health
from app.core.data_quality import DataQualityGuard
from app.db.session import Base


@pytest_asyncio.fixture
async def quality_db(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'quality.db'}",
        future=True,
    )
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_zero_completeness_is_never_rewritten_as_one(quality_db):
    guard = DataQualityGuard()
    async with quality_db() as session:
        await guard.record_success(
            session,
            "empty_source",
            "snapshot",
            latency_ms=12,
            record_count=0,
            expected_count=10,
        )
        health = await guard.check_source_health(session, "empty_source")
        all_health = await guard.get_all_health(session)

    assert health is not None
    assert health.completeness == 0.0
    assert next(
        item for item in all_health if item.source == "empty_source"
    ).completeness == 0.0


@pytest.mark.asyncio
async def test_failure_health_preserves_zero_completeness_and_error_message(quality_db):
    guard = DataQualityGuard()
    async with quality_db() as session:
        await guard.record_failure(
            session,
            "close_snapshot",
            "finalize",
            "canonical=1/5000，终场覆盖不足",
        )
        health = await guard.check_source_health(session, "close_snapshot")

    assert health is not None
    assert health.status == "degraded"
    assert health.completeness == 0.0
    assert health.error_msg == "canonical=1/5000，终场覆盖不足"


@pytest.mark.asyncio
async def test_governance_health_api_exposes_stored_failure_message(quality_db):
    guard = DataQualityGuard()
    async with quality_db() as session:
        await guard.record_failure(
            session,
            "market_sentiment",
            "snapshot",
            "指数有效样本仅1个",
            completeness=0.5,
        )
        payload = await data_source_health(db=session)

    row = next(
        item
        for item in payload["sources"]
        if item["source"] == "market_sentiment"
        and item["api_name"] == "snapshot"
    )
    assert row["status"] == "degraded"
    assert row["completeness"] == pytest.approx(0.5)
    assert row["message"] == "指数有效样本仅1个"
