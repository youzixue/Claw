"""Real temporary SQLite commits around the slow presentation/learning boundaries."""
import asyncio
import json
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import promotion
from app.config.settings import settings
from app.core.data_quality import data_quality_guard
from app.data import scheduler as scheduler_module
from app.data.scheduler import DataScheduler
from app.db.session import Base
from app.models.governance import TradeCalendarModel
from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from app.promotion import ledger
from app.strategy.auction import auction_collector

DAY = date(2026, 9, 21)
AT = datetime(2026, 9, 21, 9, 35, 5)


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'isolated.db'}",
                                connect_args={"timeout": .1})
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("CREATE TABLE prep_probe (id INTEGER PRIMARY KEY, value INTEGER)"))
    class Clock(datetime):
        current = AT
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(cls.current.isoformat())
    monkeypatch.setattr(scheduler_module, "datetime", Clock)
    monkeypatch.setattr(promotion, "datetime", Clock)
    monkeypatch.setattr(ledger, "datetime", Clock)
    monkeypatch.setattr(scheduler_module, "async_session", maker)
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    monkeypatch.setattr(scheduler_module.trade_calendar, "get_trade_session", lambda: "morning")
    async with maker() as db:
        db.add(TradeCalendarModel(trade_date=DAY, is_trade_day=True))
        await db.commit()
    scheduler = DataScheduler()
    monkeypatch.setattr(scheduler, "_ensure_fresh_promotion_news", AsyncMock(return_value={"status": "fresh"}))
    monkeypatch.setattr(auction_collector, "get_snapshot_health",
                        AsyncMock(return_value={"missing": False, "degraded": False}))
    monkeypatch.setattr(data_quality_guard, "audit_prediction_data",
                        AsyncMock(return_value={"gate_passed": True}))
    monkeypatch.setattr(settings, "PROMOTION_SHADOW_AUTOMATION_ENABLED", False)
    monkeypatch.setattr(settings, "PROMOTION_QUALITY_GATE_MODE", "enforce")
    monkeypatch.setattr(settings, "PROMOTION_SNAPSHOT_RETRY_COOLDOWN_SEC", 0)
    monkeypatch.setattr(settings, "PROMOTION_SNAPSHOT_TIMEOUT_SEC", .4)
    values = {
        "prepare_daily_hist_context": None, "prewarm_anomaly_snapshot": {},
        "_resolve_first_board_trade_date": DAY, "_resolve_second_board_source_trade_date": DAY,
        "_load_filtered_limit_ups": [], "_enrich_market_ladder_context": {},
        "_validate_official_snapshot_clock": None, "_refresh_promotion_learning": 0,
        "_load_promotion_learning_stats": {}, "_resolve_promotion_snapshot_news_end_time": AT,
        "_build_first_board_candidates": ([], AT.isoformat(), {}),
        "_build_second_board_candidates": [], "apply_active_promotion_overlay": ([], {"applied": False}),
        "_build_prediction_snapshot_health": {}, "_build_actual_limit_up_replay": {},
        "attach_daily_hist_evidence": ([], {}), "_persist_dashboard_snapshot": None,
    }
    for name, value in values.items():
        monkeypatch.setattr(promotion, name, AsyncMock(return_value=value))
    monkeypatch.setattr(promotion, "_PROMOTION_LATEST_CANDIDATES_CACHE", {})
    try:
        yield scheduler, maker, Clock
    finally:
        await engine.dispose()


async def publish(maker, *, pending=False, invalid=False):
    identity = promotion.PROMOTION_MODEL_IDENTITY
    async with maker() as db:
        count = await db.scalar(select(func.count()).select_from(PromotionPredictionRun))
        at = AT + timedelta(microseconds=count)
        row = PromotionPredictionRun(
            run_key=f"run-{count}", reference_trade_date=DAY,
            snapshot_batch_key=f"schedule:promotion_0935:{count}",
            snapshot_source="schedule", snapshot_context="promotion_0935",
            as_of_at=at, created_at=at, completed_at=at,
            model_version="invalid" if invalid else identity.active_model_version,
            feature_version=identity.feature_version, data_version=identity.data_version,
            runtime_mode=identity.runtime_mode.value,
            candidate_count=0 if pending else 1, ranked_count=0 if pending else 1,
            actionable_count=0, status="blocked" if pending else "completed",
            gate_passed=False, payload_hash="a" * 64,
            metadata_json=json.dumps({"trade_date_by_target": {"1": str(DAY)},
                "persistence": {"status": "generation_pending"} if pending else {},
                "quality_gate": {"route_gates": {"mainline_spread_start": {"gate_passed": False}}}}))
        db.add(row)
        await db.flush()
        if not pending:
            db.add(PromotionPredictionSnapshot(run_id=row.id, record_key=f"s-{count}",
                code="600001", name="isolated", prediction_trade_date=DAY, target_board=1,
                candidate_route="mainline_spread_start", rank_scope="ranked", actionable=False,
                calibrated_probability=.3, reason_json="{}", features_json="{}"))
        await db.commit()
        return row.id


@pytest.mark.asyncio
@pytest.mark.parametrize("later", [None, "pending", "invalid"])
async def test_intraday_restart_checks_latest_durable_attempt_only(env, later):
    scheduler, maker, clock = env
    await publish(maker)
    if later:
        await publish(maker, pending=True, invalid=later == "invalid")
    clock.current += timedelta(seconds=1)
    probe = await scheduler._read_promotion_recovery_probe("promotion_0935", clock.current)
    expected = {None: "persisted_completed", "pending": "no_completed_batch", "invalid": "probe_attention"}
    assert probe["status"] == expected[later]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "error", "commit_error"])
async def test_real_builder_commit_precedes_slow_presentation(env, monkeypatch, failure):
    scheduler, maker, clock = env
    from test_promotion_generation_barrier import candidate
    from app.promotion.persistence import record_promotion_predictions
    entered = asyncio.Event()

    async def record(db, *args, **kwargs):
        at = clock.now()
        item = candidate(at)
        result = await record_promotion_predictions(
            db, [item], {1: DAY, 2: DAY}, snapshot_source="schedule",
            model_version=promotion.PROMOTION_MODEL_VERSION,
            model_identity=promotion.PROMOTION_MODEL_IDENTITY,
            is_recordable=lambda item: True, reason_builder=lambda item: {},
            learning_bucket_builder=lambda target, route: "test",
            quality_gate={"gate_passed": True,
                          "route_gates": {"second_board_promotion": {"gate_passed": True}}},
            schedule_batch=ledger.ScheduleBatch("promotion_0935", at))
        if failure == "commit_error":
            monkeypatch.setattr(db, "commit", AsyncMock(side_effect=RuntimeError("commit failed")))
        return result
    async def slow_replay(*args, **kwargs):
        entered.set()
        if failure == "error":
            raise ValueError("presentation only")
        await asyncio.Event().wait()
    monkeypatch.setattr(promotion, "_record_promotion_predictions", record)
    monkeypatch.setattr(promotion, "_build_actual_limit_up_replay", slow_replay)
    result = await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935")
    if failure == "commit_error":
        assert not entered.is_set()
        assert result["status"] == "failed"
        assert scheduler._promotion_snapshot_completed_contexts == set()
        async with maker() as db:
            runs = list((await db.scalars(select(PromotionPredictionRun))).all())
            assert len(runs) == 1 and runs[0].status == "blocked"
            assert await db.scalar(select(func.count()).select_from(PromotionPredictionSnapshot)) == 0
        return
    assert entered.is_set()
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(PromotionPredictionRun)
                               .where(PromotionPredictionRun.status == "completed")) == 1
    assert (DAY, "promotion_0935") in scheduler._promotion_snapshot_completed_contexts
    assert result["status"] == f"completed_postprocessing_{failure}"
    assert (await scheduler._prewarm_promotion_candidates(
        trigger="promotion_prediction_0935"))["status"] == "already_completed"
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(PromotionPredictionRun)) == 2


@pytest.mark.asyncio
async def test_learning_preparation_does_not_hold_writer_across_candidate_work(env, monkeypatch):
    _, maker, _ = env
    entered = asyncio.Event()
    async def learning(db):
        await db.execute(text("INSERT INTO prep_probe VALUES (1, 1)"))
        return 1
    async def slow_candidates(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(promotion, "_refresh_promotion_learning", learning)
    monkeypatch.setattr(promotion, "_build_first_board_candidates", slow_candidates)
    async with maker() as db:
        task = asyncio.create_task(promotion.build_promotion_candidates(
            db=db, snapshot_source="schedule", snapshot_context="promotion_0935",
            quality_gate={"gate_passed": True}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            async with maker() as writer:
                await writer.execute(text("INSERT INTO prep_probe VALUES (2, 2)"))
                await writer.commit()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await db.rollback()
    async with maker() as reader:
        assert (await reader.execute(text("SELECT id FROM prep_probe ORDER BY id"))).scalars().all() == [1, 2]
        assert await reader.scalar(select(func.count()).select_from(PromotionPredictionRun)) == 0


@pytest.mark.asyncio
async def test_restart_recovery_does_not_append_pending_over_completed(env, monkeypatch):
    scheduler, maker, clock = env
    await publish(maker)
    clock.current += timedelta(seconds=1)
    builder = AsyncMock()
    monkeypatch.setattr(scheduler, "_prewarm_promotion_candidates", builder)
    result = await scheduler._promotion_recovery_tick()
    assert result["status"] == "persisted_completed"
    assert (DAY, "promotion_0935") in scheduler._promotion_snapshot_completed_contexts
    builder.assert_not_awaited()


@pytest.mark.asyncio
async def test_committed_empty_blocker_never_emits_success_receipt(env):
    from unittest.mock import Mock
    _, maker, _ = env
    receipt = Mock()
    async with maker() as db:
        result = await promotion.build_promotion_candidates(
            db=db, snapshot_source="schedule", snapshot_context="promotion_0935",
            quality_gate={"gate_passed": True}, on_committed=receipt)
        assert result["learning"]["recorded_predictions"] == 0
    receipt.assert_not_called()
    async with maker() as db:
        runs = list((await db.scalars(select(PromotionPredictionRun))).all())
        assert len(runs) == 1 and runs[0].status == "blocked"

