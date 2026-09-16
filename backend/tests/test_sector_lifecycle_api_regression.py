from datetime import date
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import sectors
from app.db.session import Base, get_db
from app.models import sector as sector_models  # noqa: F401
from app.models import stock as stock_models  # noqa: F401
from app.models.sector import SectorKline, SectorLifecycle
from app.models.stock import LimitUpPool, SectorInfo, SectorPersistence, StockSectorMapping
from app.sector.lifecycle import SectorLifecycleEngine


@pytest_asyncio.fixture
async def lifecycle_api_env(tmp_path: Path):
    db_path = tmp_path / "lifecycle_api_regression.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(sectors.router, prefix="/api/v1/sectors")

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SessionLocal, client

    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed_sector_basics(session: AsyncSession):
    session.add_all(
        [
            SectorInfo(
                sector_code="pw_concept_hot",
                sector_name="热点主线",
                sector_type="concept",
                source="pywencai",
                is_excluded=0,
            ),
            SectorInfo(
                sector_code="pw_concept_flash",
                sector_name="脉冲题材",
                sector_type="concept",
                source="pywencai",
                is_excluded=0,
            ),
        ]
    )
    await session.commit()


async def _seed_day(
    session: AsyncSession,
    *,
    trade_date: date,
    sector_code: str,
    sector_name: str,
    strength_score: float,
    change_pct: float,
    fund_flow: float,
    consecutive_days: int,
    trend_state: str,
    close: float,
    ma5: float,
    ma20: float,
    limit_ups: list[dict],
):
    session.add(
        SectorPersistence(
            sector_code=sector_code,
            sector_name=sector_name,
            trade_date=trade_date,
            consecutive_days=consecutive_days,
            limit_up_count=len(limit_ups),
            fund_flow=fund_flow,
            change_pct=change_pct,
            strength_score=strength_score,
        )
    )
    session.add(
        SectorKline(
            sector_code=sector_code,
            sector_name=sector_name,
            sector_type="concept",
            trade_date=trade_date,
            close=close,
            ma5=ma5,
            ma20=ma20,
            trend_state=trend_state,
            vol_ratio=1.2,
        )
    )

    for item in limit_ups:
        code = item["code"]
        session.add(
            StockSectorMapping(
                code=code,
                sector_code=sector_code,
                sector_name=sector_name,
                sector_type="concept",
                source="test",
            )
        )
        session.add(
            LimitUpPool(
                code=code,
                name=item.get("name", code),
                trade_date=trade_date,
                seal_amount=item.get("seal_amount", 10_000_000),
                consecutive_days=item.get("consecutive_days", 1),
                limit_up_reason=sector_name,
                source="test",
            )
        )

    await session.commit()


def _make_limit_ups(prefix: str, heights: list[int]) -> list[dict]:
    return [
        {
            "code": f"{prefix}{idx:03d}",
            "name": f"{prefix}{idx:03d}",
            "consecutive_days": height,
            "seal_amount": 10_000_000 + idx * 1000,
        }
        for idx, height in enumerate(heights, start=1)
    ]


async def _build_real_lifecycle_history(SessionLocal):
    engine = SectorLifecycleEngine()
    hot = ("pw_concept_hot", "热点主线")
    flash = ("pw_concept_flash", "脉冲题材")

    d1 = date(2026, 4, 12)
    d2 = date(2026, 4, 13)
    d3 = date(2026, 4, 14)

    async with SessionLocal() as session:
        await _seed_sector_basics(session)

        # 热点主线: 高潮 -> 分化 -> 退潮
        await _seed_day(
            session,
            trade_date=d1,
            sector_code=hot[0],
            sector_name=hot[1],
            strength_score=96,
            change_pct=6.8,
            fund_flow=12.0,
            consecutive_days=2,
            trend_state="up",
            close=118,
            ma5=110,
            ma20=102,
            limit_ups=_make_limit_ups("610", [5, 4, 3, 2, 2, 3, 1, 1, 1, 1, 1, 1]),
        )
        await _seed_day(
            session,
            trade_date=d2,
            sector_code=hot[0],
            sector_name=hot[1],
            strength_score=70,
            change_pct=1.5,
            fund_flow=-1.5,
            consecutive_days=3,
            trend_state="sideways",
            close=116,
            ma5=114,
            ma20=104,
            limit_ups=_make_limit_ups("611", [3, 2, 1, 1]),
        )
        await _seed_day(
            session,
            trade_date=d3,
            sector_code=hot[0],
            sector_name=hot[1],
            strength_score=32,
            change_pct=-4.2,
            fund_flow=-12.0,
            consecutive_days=0,
            trend_state="breakdown",
            close=96,
            ma5=108,
            ma20=102,
            limit_ups=[],
        )

        # 脉冲题材: 刚启动 -> 一日游 -> 休眠
        await _seed_day(
            session,
            trade_date=d1,
            sector_code=flash[0],
            sector_name=flash[1],
            strength_score=61,
            change_pct=2.4,
            fund_flow=2.5,
            consecutive_days=1,
            trend_state="up",
            close=105,
            ma5=102,
            ma20=98,
            limit_ups=_make_limit_ups("620", [1, 1]),
        )
        await _seed_day(
            session,
            trade_date=d2,
            sector_code=flash[0],
            sector_name=flash[1],
            strength_score=35,
            change_pct=-0.8,
            fund_flow=-1.0,
            consecutive_days=0,
            trend_state="sideways",
            close=101,
            ma5=102,
            ma20=99,
            limit_ups=[],
        )
        await _seed_day(
            session,
            trade_date=d3,
            sector_code=flash[0],
            sector_name=flash[1],
            strength_score=18,
            change_pct=-1.2,
            fund_flow=0.0,
            consecutive_days=0,
            trend_state="down",
            close=97,
            ma5=100,
            ma20=100,
            limit_ups=[],
        )

    # 按真实历史顺序跑引擎并落生命周期表
    for current_date in (d1, d2, d3):
        async with SessionLocal() as session:
            for code, name in (hot, flash):
                data = await engine.analyze_sector(session, code, name, "concept", current_date)
                await engine.save_lifecycle(session, data)


@pytest.mark.asyncio
async def test_lifecycle_api_real_chain_latest_state(lifecycle_api_env):
    SessionLocal, client = lifecycle_api_env
    await _build_real_lifecycle_history(SessionLocal)

    resp = await client.get("/api/v1/sectors/lifecycle", params={"trade_date": "2026-04-14"})
    assert resp.status_code == 200
    payload = resp.json()
    items = {item["sector_code"]: item for item in payload["items"]}

    assert items["pw_concept_hot"]["lifecycle_state"] == "declining"
    assert items["pw_concept_flash"]["lifecycle_state"] == "dormant"


@pytest.mark.asyncio
async def test_calendar_api_real_chain_transitions(lifecycle_api_env):
    SessionLocal, client = lifecycle_api_env
    await _build_real_lifecycle_history(SessionLocal)

    resp = await client.get("/api/v1/sectors/lifecycle/calendar", params={"days": 5})
    assert resp.status_code == 200
    payload = resp.json()

    assert payload["dates"][-3:] == ["2026-04-12", "2026-04-13", "2026-04-14"]
    matrix = payload["matrix"]

    assert matrix["pw_concept_hot"]["2026-04-12"] == "climax"
    assert matrix["pw_concept_hot"]["2026-04-13"] == "diverging"
    assert matrix["pw_concept_hot"]["2026-04-14"] == "declining"

    assert matrix["pw_concept_flash"]["2026-04-12"] == "emerging"
    assert matrix["pw_concept_flash"]["2026-04-13"] == "one_day"
    assert matrix["pw_concept_flash"]["2026-04-14"] == "dormant"


@pytest.mark.asyncio
async def test_lifecycle_api_intraday_prefers_today_realtime_when_today_lifecycle_missing(
    lifecycle_api_env, monkeypatch
):
    SessionLocal, client = lifecycle_api_env
    await _build_real_lifecycle_history(SessionLocal)

    # 模拟盘中: 持续性表已有 4/14，但生命周期表尚未生成 4/14。
    async with SessionLocal() as session:
        await session.execute(
            SectorLifecycle.__table__.delete().where(SectorLifecycle.trade_date == date(2026, 4, 14))
        )
        await session.commit()

    monkeypatch.setattr(sectors, "_is_intraday_session_now", lambda: True)

    resp = await client.get("/api/v1/sectors/lifecycle")
    assert resp.status_code == 200
    payload = resp.json()

    # 盘中应优先展示今天 4/14，并走实时引擎回补状态，不该退回 4/13。
    assert payload["trade_date"] == "2026-04-14"
    items = {item["sector_code"]: item for item in payload["items"]}
    assert items["pw_concept_hot"]["lifecycle_state"] == "declining"
    assert items["pw_concept_flash"]["lifecycle_state"] == "dormant"


@pytest.mark.asyncio
async def test_lifecycle_api_non_intraday_falls_back_to_latest_precomputed_trade_date(
    lifecycle_api_env, monkeypatch
):
    SessionLocal, client = lifecycle_api_env
    await _build_real_lifecycle_history(SessionLocal)

    async with SessionLocal() as session:
        await session.execute(
            SectorLifecycle.__table__.delete().where(SectorLifecycle.trade_date == date(2026, 4, 14))
        )
        await session.commit()

    monkeypatch.setattr(sectors, "_is_intraday_session_now", lambda: False)

    resp = await client.get("/api/v1/sectors/lifecycle")
    assert resp.status_code == 200
    payload = resp.json()

    # 非盘中优先展示最近一次已经预计算完成的交易日。
    assert payload["trade_date"] == "2026-04-13"


@pytest.mark.asyncio
async def test_calendar_api_fallback_uses_same_engine_when_lifecycle_table_missing(lifecycle_api_env):
    SessionLocal, client = lifecycle_api_env
    await _build_real_lifecycle_history(SessionLocal)

    async with SessionLocal() as session:
        await session.execute(SectorLifecycle.__table__.delete())
        await session.commit()

    resp = await client.get("/api/v1/sectors/lifecycle/calendar", params={"days": 5})
    assert resp.status_code == 200
    payload = resp.json()
    matrix = payload["matrix"]

    assert matrix["pw_concept_hot"]["2026-04-12"] == "climax"
    assert matrix["pw_concept_hot"]["2026-04-13"] == "diverging"
    assert matrix["pw_concept_hot"]["2026-04-14"] == "declining"

    assert matrix["pw_concept_flash"]["2026-04-12"] == "emerging"
    assert matrix["pw_concept_flash"]["2026-04-13"] == "one_day"
    assert matrix["pw_concept_flash"]["2026-04-14"] == "dormant"


@pytest.mark.asyncio
async def test_intraday_realtime_and_after_close_precomputed_results_are_consistent(
    lifecycle_api_env, monkeypatch
):
    SessionLocal, client = lifecycle_api_env
    await _build_real_lifecycle_history(SessionLocal)

    # 模拟盘中：今天生命周期表缺失，只能走实时引擎。
    async with SessionLocal() as session:
        await session.execute(
            SectorLifecycle.__table__.delete().where(SectorLifecycle.trade_date == date(2026, 4, 14))
        )
        await session.commit()

    monkeypatch.setattr(sectors, "_is_intraday_session_now", lambda: True)
    intraday_resp = await client.get("/api/v1/sectors/lifecycle")
    assert intraday_resp.status_code == 200
    intraday_payload = intraday_resp.json()
    intraday_items = {item["sector_code"]: item for item in intraday_payload["items"]}

    # 模拟盘后收盘重算：把 4/14 的生命周期结果正式落表。
    engine = SectorLifecycleEngine()
    async with SessionLocal() as session:
        for code, name in (("pw_concept_hot", "热点主线"), ("pw_concept_flash", "脉冲题材")):
            data = await engine.analyze_sector(session, code, name, "concept", date(2026, 4, 14))
            await engine.save_lifecycle(session, data)

    monkeypatch.setattr(sectors, "_is_intraday_session_now", lambda: False)
    after_close_resp = await client.get("/api/v1/sectors/lifecycle")
    assert after_close_resp.status_code == 200
    after_close_payload = after_close_resp.json()
    after_close_items = {item["sector_code"]: item for item in after_close_payload["items"]}

    assert intraday_payload["trade_date"] == "2026-04-14"
    assert after_close_payload["trade_date"] == "2026-04-14"
    assert intraday_items["pw_concept_hot"]["lifecycle_state"] == after_close_items["pw_concept_hot"]["lifecycle_state"] == "declining"
    assert intraday_items["pw_concept_flash"]["lifecycle_state"] == after_close_items["pw_concept_flash"]["lifecycle_state"] == "dormant"
