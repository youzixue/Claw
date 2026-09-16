from datetime import date
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import market_regime
from app.db.session import Base, get_db
from app.models.regime import MarketRegimeSnapshot
from app.models.sector import SectorLifecycle, SectorRotation
from app.models.stock import LimitUpPool, MarketSentiment, SectorPersistence, StockKline
from app.promotion.regime import RegimeInputs, build_market_regime_snapshot, classify_market_regime


@pytest_asyncio.fixture
async def regime_env(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'regime.db'}", future=True
    )
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(market_regime.router, prefix="/api/v1/market-regime")

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SessionLocal, client
    app.dependency_overrides.clear()
    await engine.dispose()


def test_regime_classifier_distinguishes_risk_rotation_and_maintrend():
    risk = classify_market_regime(
        RegimeInputs(
            advance_ratio=0.16,
            previous_advance_ratio=0.40,
            limit_up_count=12,
            previous_limit_up_count=40,
            limit_down_count=35,
            broken_limit_count=30,
            seal_rate=28,
            main_net_inflow=-120,
            sentiment_cycle="freezing",
        )
    )
    assert risk["primary_regime"] == "risk_off"

    rotation = classify_market_regime(
        RegimeInputs(
            advance_ratio=0.55,
            previous_advance_ratio=0.52,
            limit_up_count=55,
            previous_limit_up_count=50,
            seal_rate=70,
            board_height=2,
            first_board_count=45,
            active_sector_count=16,
            strong_sector_count=10,
            persistent_sector_count=5,
            rotation_signal_count=10,
            top_sector_limit_share=0.14,
        )
    )
    assert rotation["primary_regime"] == "sector_rotation"

    maintrend = classify_market_regime(
        RegimeInputs(
            advance_ratio=0.57,
            previous_advance_ratio=0.53,
            limit_up_count=60,
            previous_limit_up_count=52,
            seal_rate=82,
            board_height=5,
            first_board_count=35,
            consecutive_board_count=18,
            active_sector_count=6,
            strong_sector_count=4,
            mainline_sector_count=3,
            persistent_sector_count=6,
            rotation_signal_count=1,
            top_sector_limit_share=0.68,
            top_sector_strength=92,
        )
    )
    assert maintrend["primary_regime"] == "sector_maintrend"


async def _seed_regime_day(session: AsyncSession) -> None:
    previous_day = date(2026, 8, 27)
    trade_day = date(2026, 8, 28)
    for index in range(12):
        code = f"000{index:03d}"
        session.add(
            StockKline(
                code=code,
                trade_date=previous_day,
                open=10,
                high=10.2,
                low=9.8,
                close=10,
                prev_close=10,
                change_pct=-1 if index < 7 else 1,
                volume=1000,
            )
        )
        session.add(
            StockKline(
                code=code,
                trade_date=trade_day,
                open=10,
                high=11,
                low=9.9,
                close=10.5,
                prev_close=10,
                change_pct=3 if index < 9 else -1,
                volume=1200,
            )
        )
    session.add_all(
        [
            MarketSentiment(
                trade_date=previous_day,
                sentiment_cycle="freezing",
                limit_up_count=15,
                limit_down_count=20,
                broken_limit_count=18,
                seal_rate=45,
                board_height=2,
                main_net_inflow=-40,
            ),
            MarketSentiment(
                trade_date=trade_day,
                sentiment_cycle="recovery",
                limit_up_count=42,
                limit_down_count=5,
                broken_limit_count=8,
                seal_rate=78,
                board_height=3,
                main_net_inflow=35,
            ),
        ]
    )
    for index in range(5):
        session.add(
            LimitUpPool(
                code=f"000{index:03d}",
                trade_date=trade_day,
                consecutive_days=2 if index == 0 else 1,
                source="test",
            )
        )
    session.add_all(
        [
            SectorPersistence(
                sector_code="S1",
                sector_name="主线",
                trade_date=trade_day,
                consecutive_days=4,
                limit_up_count=4,
                change_pct=3.0,
                strength_score=88,
            ),
            SectorPersistence(
                sector_code="S2",
                sector_name="轮动",
                trade_date=trade_day,
                consecutive_days=2,
                limit_up_count=1,
                change_pct=1.2,
                strength_score=66,
            ),
            SectorLifecycle(
                trade_date=trade_day,
                sector_code="S1",
                sector_name="主线",
                sector_type="concept",
                lifecycle_state="accelerating",
                state_score=90,
                is_main_line=1,
            ),
            SectorRotation(
                trade_date=trade_day,
                from_sector="S0",
                to_sector="S2",
                flow_amount=10,
                rotation_type="gradual",
            ),
        ]
    )
    await session.commit()


@pytest.mark.asyncio
async def test_regime_snapshot_is_versioned_idempotent_and_exposed_by_api(regime_env):
    SessionLocal, client = regime_env
    async with SessionLocal() as session:
        await _seed_regime_day(session)
        first = await build_market_regime_snapshot(
            session,
            trade_date=date(2026, 8, 28),
            minimum_universe_count=1,
            persist=True,
        )
        await session.commit()
        second = await build_market_regime_snapshot(
            session,
            trade_date=date(2026, 8, 28),
            minimum_universe_count=1,
            persist=True,
        )
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(MarketRegimeSnapshot))

    assert first["snapshot_key"] == second["snapshot_key"]
    assert count == 1
    assert first["quality_status"] == "good"
    assert first["persisted"] is True

    current = await client.get("/api/v1/market-regime/current")
    assert current.status_code == 200
    payload = current.json()
    assert payload["trade_date"] == "2026-08-28"
    assert payload["regime_version"].startswith("ashare_regime_rules")
    assert payload["features"]["universe_count"] == 12

    history = await client.get("/api/v1/market-regime/history")
    assert history.status_code == 200
    assert history.json()["count"] == 1
