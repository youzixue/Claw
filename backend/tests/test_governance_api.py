from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import governance
from app.db.session import Base, get_db
from app.models import stock as stock_models  # noqa: F401
from app.models.stock import StockKline, StockSpot


@pytest_asyncio.fixture
async def governance_api_env(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "governance_api.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(governance.router, prefix="/api/v1/governance")

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(governance.data_quality_guard, "get_all_health", lambda db: _empty_health())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SessionLocal, client

    app.dependency_overrides.clear()
    await engine.dispose()


async def _empty_health():
    return []


@pytest.mark.asyncio
async def test_governance_health_marks_spot_kline_alignment_down_on_date_mismatch(governance_api_env):
    SessionLocal, client = governance_api_env

    async with SessionLocal() as session:
        session.add(
            StockSpot(
                code="000001",
                name="测试股",
                price=10.0,
                updated_at=datetime(2026, 4, 23, 23, 59, 55),
            )
        )
        session.add(
            StockKline(
                code="000001",
                trade_date=date(2026, 4, 24),
                open=10.0,
                close=10.1,
                high=10.2,
                low=9.9,
                volume=100000,
                source="spot_fallback",
            )
        )
        await session.commit()

    response = await client.get("/api/v1/governance/health")
    assert response.status_code == 200
    payload = response.json()
    alignment = payload["sources"][0]

    assert alignment["source"] == "spot_kline_alignment"
    assert alignment["status"] == "down"
    assert alignment["latest_spot_trade_date"] == "2026-04-23"
    assert alignment["latest_kline_trade_date"] == "2026-04-24"
    assert alignment["date_gap_days"] == -1
    assert "早于 kline" in alignment["message"]
    # Additive deployment evidence must serialize without changing source health.
    pipeline = payload["pipeline"]
    assert pipeline["contract_version"] == "intraday_pipeline_v2_nonblocking_news"
    assert pipeline["confirmation_windows_changed"] is False
    assert pipeline["active_phase"] is None
    assert isinstance(pipeline["news_source_checks"], dict)
    assert isinstance(pipeline["recent_events"], list)


@pytest.mark.asyncio
async def test_governance_health_marks_spot_kline_alignment_up_when_dates_match(governance_api_env):
    SessionLocal, client = governance_api_env

    async with SessionLocal() as session:
        session.add(
            StockSpot(
                code="000001",
                name="测试股",
                price=10.0,
                updated_at=datetime(2026, 4, 23, 14, 35, 0),
            )
        )
        session.add(
            StockKline(
                code="000001",
                trade_date=date(2026, 4, 23),
                open=10.0,
                close=10.1,
                high=10.2,
                low=9.9,
                volume=100000,
                source="spot_fallback",
            )
        )
        await session.commit()

    response = await client.get("/api/v1/governance/health")
    assert response.status_code == 200
    payload = response.json()
    alignment = payload["sources"][0]

    assert alignment["source"] == "spot_kline_alignment"
    assert alignment["status"] == "up"
    assert alignment["date_gap_days"] == 0
    assert alignment["message"] == "spot 与 kline 已对齐: 2026-04-23"


@pytest.mark.asyncio
async def test_prediction_quality_endpoint_returns_fail_closed_audit(governance_api_env):
    _SessionLocal, client = governance_api_env

    response = await client.get("/api/v1/governance/prediction-quality")

    assert response.status_code == 200
    payload = response.json()
    assert payload["gate_passed"] is False
    assert payload["status"] == "blocked"
    assert payload["blocking_count"] >= 1
    assert {item["dataset"] for item in payload["watermarks"]} == {
        "stock_kline",
        "fund_flow",
        "limit_up_pool",
        "auction_data",
    }


@pytest.mark.asyncio
async def test_prediction_quality_run_endpoint_persists_audit(governance_api_env):
    _SessionLocal, client = governance_api_env

    response = await client.post(
        "/api/v1/governance/prediction-quality/run",
        params={"snapshot_context": "manual"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] > 0
    latest = await client.get("/api/v1/governance/prediction-quality")
    assert latest.status_code == 200
    assert latest.json()["run_id"] == payload["run_id"]
