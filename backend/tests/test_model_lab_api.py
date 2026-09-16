from datetime import date
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.v1 import model_lab
from app.db.session import Base, get_db
from app.models import promotion as promotion_models  # noqa: F401
from app.models import signal as signal_models  # noqa: F401
from app.promotion.ledger import append_prediction_run
from app.promotion.versioning import PromotionModelIdentity, PromotionRuntimeMode


@pytest_asyncio.fixture
async def model_lab_env(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'model_lab.db'}", future=True
    )
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(model_lab.router, prefix="/api/v1/model-lab")

    async def override_get_db():
        async with SessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SessionLocal, client
    app.dependency_overrides.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_model_lab_lists_and_replays_immutable_run(model_lab_env):
    SessionLocal, client = model_lab_env
    identity = PromotionModelIdentity(
        runtime_mode=PromotionRuntimeMode.LEGACY,
        champion_model_version="api_test_v1",
        challenger_model_version=None,
        feature_version="features_v1",
        data_version="data_v1",
    )
    candidate = {
        "code": "000001",
        "name": "测试股",
        "target_board": 1,
        "candidate_route": "fresh_mainline_start",
        "raw_probability": 0.2,
        "probability": 0.1,
        "probability_factors": {
            "prediction_snapshot_source": "schedule",
            "prediction_snapshot_context": "promotion_2000",
            "prediction_snapshot_recorded_at": "2026-08-28T20:00:00",
            "prediction_snapshot_batch_key": "schedule:promotion_2000:2026-08-28T20:00:00",
            "prediction_record_scope": "ranked",
            "prediction_ranked_selected": True,
            "prediction_ranked_position": 1,
            "prediction_pool_rank": 1,
            "memory_score": 60,
        },
    }
    async with SessionLocal() as session:
        result = await append_prediction_run(
            session,
            [candidate],
            {1: date(2026, 8, 28)},
            identity=identity,
            snapshot_source="schedule",
            snapshot_context="promotion_2000",
        )
        await session.commit()

    response = await client.get(
        "/api/v1/model-lab/runs",
        params={"trade_date": "2026-08-28", "snapshot_context": "promotion_2000"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["runs"][0]["model_version"] == "api_test_v1"

    detail = await client.get(
        f"/api/v1/model-lab/runs/{result.run_id}",
        params={"include_features": True},
    )
    assert detail.status_code == 200
    replay = detail.json()
    assert replay["run"]["payload_hash"]
    assert replay["snapshots"][0]["code"] == "000001"
    assert replay["snapshots"][0]["features"]["memory_score"] == 60

    artifacts = await client.get("/api/v1/model-lab/artifacts")
    assert artifacts.status_code == 200
    assert artifacts.json()["artifacts"][0]["model_version"] == "api_test_v1"


@pytest.mark.asyncio
async def test_model_lab_missing_run_returns_404(model_lab_env):
    _SessionLocal, client = model_lab_env
    response = await client.get("/api/v1/model-lab/runs/999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_model_lab_training_fails_closed_when_history_is_insufficient(model_lab_env):
    _SessionLocal, client = model_lab_env

    response = await client.post(
        "/api/v1/model-lab/train",
        json={"target_board": 1, "persist": False},
    )

    assert response.status_code == 409
    assert "insufficient" in response.json()["detail"]
    runs = await client.get("/api/v1/model-lab/training-runs")
    assert runs.status_code == 200
    assert runs.json() == {"count": 0, "training_runs": []}
