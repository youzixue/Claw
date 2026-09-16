from datetime import date
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models.promotion import (
    PromotionModelArtifact,
    PromotionPredictionRun,
    PromotionPredictionSnapshot,
)
from app.models.signal import PromotionPredictionRecord  # noqa: F401
from app.promotion.ledger import append_prediction_run
from app.promotion.versioning import PromotionModelIdentity, PromotionRuntimeMode


@pytest_asyncio.fixture
async def ledger_session(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'promotion_ledger.db'}", future=True
    )
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        yield session
    await engine.dispose()


def test_compatibility_model_version_column_accepts_governed_versions():
    assert PromotionPredictionRecord.__table__.c.model_version.type.length == 80


def _identity() -> PromotionModelIdentity:
    return PromotionModelIdentity(
        runtime_mode=PromotionRuntimeMode.LEGACY,
        champion_model_version="test_champion_v1",
        challenger_model_version=None,
        feature_version="test_features_v1",
        data_version="test_data_v1",
    )


def _candidate(code: str, *, batch_key: str = "schedule:promotion_2000:2026-08-28T20:00:00") -> dict:
    return {
        "code": code,
        "name": f"测试{code}",
        "target_board": 1,
        "candidate_route": "fresh_mainline_start",
        "learning_bucket": "T1:fresh_mainline_start",
        "raw_probability": 0.2,
        "probability": 0.12,
        "confidence_level": "medium",
        "signal_status": "watch",
        "probability_factors": {
            "prediction_snapshot_source": "schedule",
            "prediction_snapshot_context": "promotion_2000",
            "prediction_snapshot_recorded_at": batch_key.rsplit(":", 1)[-1]
            if batch_key.count(":") < 4
            else "2026-08-28T20:00:00",
            "prediction_snapshot_batch_key": batch_key,
            "prediction_record_scope": "ranked",
            "prediction_pool_rank": 1,
            "prediction_ranked_selected": True,
            "prediction_ranked_position": 1,
            "prediction_recall_ranked_position": 1,
            "prediction_trade_gate_passed": True,
            "prediction_actionable": True,
            "memory_score": 68.5,
        },
    }


@pytest.mark.asyncio
async def test_append_prediction_run_is_idempotent_for_same_batch_and_payload(ledger_session):
    candidates = [_candidate("000001"), _candidate("600000")]
    trade_dates = {1: date(2026, 8, 28)}

    first = await append_prediction_run(
        ledger_session,
        candidates,
        trade_dates,
        identity=_identity(),
        snapshot_source="schedule",
        snapshot_context="promotion_2000",
    )
    second = await append_prediction_run(
        ledger_session,
        candidates,
        trade_dates,
        identity=_identity(),
        snapshot_source="schedule",
        snapshot_context="promotion_2000",
    )
    await ledger_session.commit()

    assert first is not None and first.created is True
    assert second is not None and second.created is False
    assert second.run_id == first.run_id
    assert await ledger_session.scalar(select(func.count()).select_from(PromotionPredictionRun)) == 1
    assert await ledger_session.scalar(select(func.count()).select_from(PromotionPredictionSnapshot)) == 2
    assert await ledger_session.scalar(select(func.count()).select_from(PromotionModelArtifact)) == 1

    run = await ledger_session.get(PromotionPredictionRun, first.run_id)
    assert run.model_version == "test_champion_v1"
    assert run.feature_version == "test_features_v1"
    assert run.data_version == "test_data_v1"
    assert run.ranked_count == 2
    assert run.actionable_count == 2


@pytest.mark.asyncio
async def test_new_batch_key_appends_new_run_without_replacing_prior_snapshot(ledger_session):
    trade_dates = {1: date(2026, 8, 28)}
    first = await append_prediction_run(
        ledger_session,
        [_candidate("000001")],
        trade_dates,
        identity=_identity(),
        snapshot_source="schedule",
        snapshot_context="promotion_2000",
    )
    second = await append_prediction_run(
        ledger_session,
        [_candidate("000001", batch_key="schedule:promotion_2000:2026-08-28T20:05:00")],
        trade_dates,
        identity=_identity(),
        snapshot_source="schedule",
        snapshot_context="promotion_2000",
    )
    await ledger_session.commit()

    assert first is not None and second is not None
    assert first.run_id != second.run_id
    assert await ledger_session.scalar(select(func.count()).select_from(PromotionPredictionRun)) == 2
    snapshots = list((await ledger_session.execute(select(PromotionPredictionSnapshot))).scalars())
    assert len(snapshots) == 2
    assert {snapshot.code for snapshot in snapshots} == {"000001"}


@pytest.mark.asyncio
async def test_prediction_run_rejects_orm_mutation(ledger_session):
    result = await append_prediction_run(
        ledger_session,
        [_candidate("000001")],
        {1: date(2026, 8, 28)},
        identity=_identity(),
        snapshot_source="schedule",
        snapshot_context="promotion_2000",
    )
    await ledger_session.commit()
    run = await ledger_session.get(PromotionPredictionRun, result.run_id)
    run.status = "rewritten"

    with pytest.raises(RuntimeError, match="append-only"):
        await ledger_session.flush()
    await ledger_session.rollback()
