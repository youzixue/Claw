from datetime import datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import stocks
from app.db.session import Base, get_db
from app.models import stock as stock_models  # noqa: F401
from app.models.stock import StockSpot, StockTag


@pytest_asyncio.fixture
async def stock_search_api_env(tmp_path: Path):
    db_path = tmp_path / "stock_search_api.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    session_local = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_local() as session:
        session.add_all(
            [
                StockTag(
                    code="600519",
                    name="贵州茅台",
                    board_type="main_sh",
                    board_tag="tradeable",
                ),
                StockTag(
                    code="000001",
                    name="平安银行",
                    board_type="main_sz",
                    board_tag="tradeable",
                ),
                StockTag(
                    code="300750",
                    name="宁德时代",
                    board_type="gem",
                    board_tag="observe_only",
                ),
            ]
        )
        session.add(
            StockSpot(
                code="600519",
                name="贵州茅台",
                price=1450.5,
                change_pct=1.25,
                updated_at=datetime(2026, 9, 1, 14, 30),
            )
        )
        await session.commit()

    app = FastAPI()
    app.include_router(stocks.router, prefix="/api/v1/stocks")

    async def override_get_db():
        async with session_local() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_search_stock_by_market_suffixed_code_returns_exact_match(stock_search_api_env):
    response = await stock_search_api_env.get(
        "/api/v1/stocks/search",
        params={"keyword": "600519.SH"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["keyword"] == "600519"
    assert payload["count"] == 1
    assert payload["stocks"][0] == {
        "code": "600519",
        "name": "贵州茅台",
        "board_type": "main_sh",
        "board_tag": "tradeable",
        "is_st": False,
        "is_suspended": False,
        "is_delisting": False,
        "price": 1450.5,
        "change_pct": 1.25,
        "updated_at": "2026-09-01 14:30:00",
    }


@pytest.mark.asyncio
async def test_search_stock_by_partial_name_includes_risk_classification(stock_search_api_env):
    response = await stock_search_api_env.get(
        "/api/v1/stocks/search",
        params={"keyword": "宁德"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["stocks"][0]["code"] == "300750"
    assert payload["stocks"][0]["board_type"] == "gem"
    assert payload["stocks"][0]["board_tag"] == "observe_only"
    assert payload["stocks"][0]["price"] is None


@pytest.mark.asyncio
async def test_search_stock_escapes_like_wildcards(stock_search_api_env):
    response = await stock_search_api_env.get(
        "/api/v1/stocks/search",
        params={"keyword": "%"},
    )

    assert response.status_code == 200
    assert response.json()["stocks"] == []
