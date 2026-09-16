"""Recovery invokes the original generator only at real, still-legal times."""
import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.data.scheduler import DataScheduler, _promotion_startup_catchup_trigger
import app.data.scheduler as scheduler_module


@pytest.mark.parametrize(("clock", "trigger"), [
    ("15:04:59", ""), ("15:05:00", ""), ("15:09:59", ""),
    ("15:10:00", "1510"), ("15:34:23", "1510"), ("16:30:59", "1510"),
    ("16:31:00", ""), ("19:49:59", ""), ("19:50:00", ""), ("19:59:59", ""),
    ("20:00:00", "2000"), ("21:30:59", "2000"), ("21:31:00", ""),
])
def test_close_recovery_is_not_early_or_expired(clock, trigger):
    result = _promotion_startup_catchup_trigger(datetime.fromisoformat("2026-09-14T" + clock))
    assert result == ("promotion_prediction_" + trigger if trigger else "")


@pytest.fixture
def frozen_clock(monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 9, 14, 15, 34, 23)
        @classmethod
        def now(cls, tz=None):
            return cls.current
    monkeypatch.setattr(scheduler_module, "datetime", Clock)
    return Clock


@pytest.mark.asyncio
async def test_missing_close_recovers_through_original_builder(frozen_clock, monkeypatch):
    scheduler = DataScheduler()
    probe = AsyncMock(return_value={"status": "no_completed_batch", "diagnostic_status": "awaiting_persisted_attempt"})
    builder = AsyncMock(return_value={"learning": {"recorded_predictions": 12}})
    monkeypatch.setattr(scheduler, "_read_promotion_recovery_probe", probe)
    monkeypatch.setattr(scheduler, "_prewarm_promotion_candidates", builder)
    result = await scheduler._promotion_recovery_tick()
    builder.assert_awaited_once_with(trigger="promotion_prediction_1510")
    assert result["checked_at"] == "2026-09-14T15:34:23"
    assert result["recorded_predictions"] == 12
    assert result["attempt_status"] == "completed"
    assert result["historical_backfill"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "completed_route_blocked", "completed_batch_gate_not_passed", "completed_route_gate_unknown"])
async def test_restart_preserves_published_batch_including_route_blocks(frozen_clock, monkeypatch, status):
    scheduler = DataScheduler()
    probe = AsyncMock(return_value={"status": "persisted_completed", "run_id": 19, "diagnostic_status": status})
    build = AsyncMock()
    monkeypatch.setattr(scheduler, "_read_promotion_recovery_probe", probe)
    monkeypatch.setattr(scheduler, "_prewarm_promotion_candidates", build)
    result = await scheduler._promotion_recovery_tick()
    assert result["diagnostic_status"] == status
    assert result["run_id"] == 19
    assert (date(2026, 9, 14), "promotion_1510") in scheduler._promotion_snapshot_completed_contexts
    assert (await scheduler._promotion_recovery_tick())["status"] == "already_completed"
    probe.assert_awaited_once()
    build.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["not_trade_day", "probe_unavailable", "probe_attention"])
async def test_unknown_evidence_or_closed_day_cannot_trigger_generation(frozen_clock, monkeypatch, status):
    scheduler = DataScheduler()
    monkeypatch.setattr(scheduler, "_read_promotion_recovery_probe", AsyncMock(return_value={"status": status}))
    build = AsyncMock()
    monkeypatch.setattr(scheduler, "_prewarm_promotion_candidates", build)
    assert (await scheduler._promotion_recovery_tick())["status"] == status
    build.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("next_clock", [datetime(2026, 9, 14, 16, 31), datetime(2026, 9, 15, 15, 34)])
async def test_probe_crossing_expiry_or_date_cannot_backdate(frozen_clock, monkeypatch, next_clock):
    scheduler = DataScheduler()
    async def probe(*args):
        frozen_clock.current = next_clock
        return {"status": "no_completed_batch"}
    monkeypatch.setattr(scheduler, "_read_promotion_recovery_probe", probe)
    build = AsyncMock()
    monkeypatch.setattr(scheduler, "_prewarm_promotion_candidates", build)
    assert (await scheduler._promotion_recovery_tick())["status"] == "expired_during_probe"
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_timeout_releases_tick_without_generation(frozen_clock, monkeypatch):
    scheduler = DataScheduler()
    async def blocked(*args):
        await asyncio.Event().wait()
    monkeypatch.setattr(scheduler, "_read_promotion_recovery_probe", blocked)
    monkeypatch.setattr(scheduler_module.settings, "PROMOTION_RECOVERY_PROBE_TIMEOUT_SEC", .01)
    build = AsyncMock()
    monkeypatch.setattr(scheduler, "_prewarm_promotion_candidates", build)
    assert (await scheduler._promotion_recovery_tick())["status"] == "probe_timeout"
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_failure_does_not_disclose_exception_text(frozen_clock, monkeypatch):
    scheduler = DataScheduler()
    monkeypatch.setattr(scheduler, "_read_promotion_recovery_probe", AsyncMock(side_effect=RuntimeError("secret fixture")))
    result = await scheduler._promotion_recovery_tick()
    assert result["status"] == "probe_failed" and result["error_type"] == "RuntimeError"
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_expired_day_does_not_query_or_generate(frozen_clock, monkeypatch):
    frozen_clock.current = datetime(2026, 9, 14, 19, 0)
    scheduler = DataScheduler()
    probe, build = AsyncMock(), AsyncMock()
    monkeypatch.setattr(scheduler, "_read_promotion_recovery_probe", probe)
    monkeypatch.setattr(scheduler, "_prewarm_promotion_candidates", build)
    assert (await scheduler._promotion_recovery_tick())["status"] == "outside_recovery_window"
    probe.assert_not_awaited()
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_running_builder_keeps_existing_single_owner_coalescing(frozen_clock, monkeypatch):
    scheduler = DataScheduler()
    scheduler._promotion_prediction_refreshing = True
    scheduler._promotion_prediction_active_context = "promotion_1510"
    probe = AsyncMock()
    monkeypatch.setattr(scheduler, "_read_promotion_recovery_probe", probe)
    result = await scheduler._promotion_recovery_tick()
    assert result["attempt_status"] == "coalesced"
    probe.assert_not_awaited()
    assert scheduler._promotion_prediction_pending is None


@pytest.mark.asyncio
@pytest.mark.parametrize("later_status", [None, "generation_pending", "invalid_model"])
async def test_real_read_probe_does_not_restore_old_success_over_new_attempt(
    tmp_path, frozen_clock, monkeypatch, later_status,
):
    import json
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db.session import Base
    from app.models.governance import TradeCalendarModel
    from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
    from app.api.v1.promotion import PROMOTION_MODEL_IDENTITY
    from app.core.prediction_data_quality import PREDICTION_ROUTE_REQUIRED_DATASETS

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        identity = PROMOTION_MODEL_IDENTITY
        day = frozen_clock.current.date()
        at = frozen_clock.current - timedelta(minutes=10)
        def row(id, **changes):
            values = dict(id=id, run_key=f"run-{id}", reference_trade_date=day,
                snapshot_batch_key=f"schedule:promotion_1510:{id}", as_of_at=at,
                created_at=at, completed_at=at + timedelta(seconds=1),
                snapshot_source="schedule", snapshot_context="promotion_1510",
                model_version=identity.active_model_version, feature_version=identity.feature_version,
                data_version=identity.data_version, runtime_mode=identity.runtime_mode.value,
                candidate_count=1, ranked_count=1, actionable_count=0,
                status="completed", gate_passed=False, payload_hash="a" * 64,
                metadata_json=json.dumps({"trade_date_by_target": {"1": str(day)},
                    "quality_gate": {"route_gates": {r: {"gate_passed": False}
                                     for r in PREDICTION_ROUTE_REQUIRED_DATASETS}}}))
            return PromotionPredictionRun(**{**values, **changes})
        async with sessions() as db:
            db.add(TradeCalendarModel(trade_date=day, is_trade_day=True))
            db.add(row(1))
            db.add(PromotionPredictionSnapshot(run_id=1, record_key="s1",
                code="000001", name="隔离样本", prediction_trade_date=day,
                target_board=1, candidate_route="mainline_spread_start", rank_scope="ranked",
                actionable=False, calibrated_probability=30, reason_json="{}", features_json="{}"))
            if later_status:
                db.add(row(2, as_of_at=at + timedelta(minutes=1),
                    created_at=at + timedelta(minutes=1),
                    completed_at=at + timedelta(minutes=1, seconds=1),
                    status="blocked", candidate_count=0, ranked_count=0,
                    model_version="wrong-model" if later_status == "invalid_model" else identity.active_model_version,
                    metadata_json=json.dumps({"persistence": {"status": "generation_pending"}})))
            await db.commit()
        statements = []
        event.listen(engine.sync_engine, "before_cursor_execute",
                     lambda c, cur, statement, params, ctx, many: statements.append(statement))
        class ReadOnly:
            async def __aenter__(self):
                self.db = sessions()
                await self.db.execute(text("PRAGMA query_only=ON"))
                return self.db
            async def __aexit__(self, *args):
                await self.db.close()
        monkeypatch.setattr(scheduler_module, "async_session", ReadOnly)
        monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
        scheduler = DataScheduler()
        probe = await scheduler._read_promotion_recovery_probe("promotion_1510", frozen_clock.current)
        expected = {None: "persisted_completed", "generation_pending": "no_completed_batch",
                    "invalid_model": "probe_attention"}[later_status]
        assert probe["status"] == expected
        if later_status is None:
            assert probe["diagnostic_status"] == "completed_route_blocked"
        assert all(sql.lstrip().upper().startswith(("SELECT", "PRAGMA")) for sql in statements)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_live_cron_failure_during_probe_cannot_resurrect_old_completion(frozen_clock, monkeypatch):
    scheduler = DataScheduler()
    monkeypatch.setattr(scheduler, "_build_promotion_snapshot_once", AsyncMock(return_value={"status": "failed"}))
    async def probe(*args):
        result = await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_1510")
        assert result["status"] == "failed"
        assert scheduler._promotion_prediction_refreshing is False
        return {"status": "persisted_completed", "run_id": 1, "diagnostic_status": "completed"}
    monkeypatch.setattr(scheduler, "_read_promotion_recovery_probe", probe)
    result = await scheduler._promotion_recovery_tick()
    assert result["status"] == "probe_obsoleted_by_live_attempt"
    assert scheduler._promotion_snapshot_completed_contexts == set()
    assert scheduler._promotion_prediction_epoch == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("calendar", [None, 0, 1, "true"])
async def test_calendar_unknown_is_not_mislabeled_closed_or_authorized(frozen_clock, monkeypatch, calendar):
    scheduler = DataScheduler()
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", AsyncMock(return_value=calendar))
    result = await scheduler._read_promotion_recovery_probe("promotion_1510", frozen_clock.current)
    assert result["status"] == "calendar_unknown"


@pytest.mark.asyncio
async def test_stop_cancels_recovery_probe_even_if_apscheduler_already_stopped(frozen_clock, monkeypatch):
    scheduler = DataScheduler()
    started = asyncio.Event()
    async def probe(*args):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(scheduler, "_read_promotion_recovery_probe", probe)
    build = AsyncMock()
    monkeypatch.setattr(scheduler, "_prewarm_promotion_candidates", build)
    task = asyncio.create_task(scheduler._promotion_recovery_tick())
    scheduler._promotion_startup_catchup_task = task
    await asyncio.wait_for(started.wait(), 1)
    scheduler.stop()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert scheduler._promotion_startup_catchup_task is None
    build.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_reuses_real_start_barrier_before_close_gate(tmp_path, frozen_clock, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.db.session import Base
    from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'barrier.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(scheduler_module, "async_session", sessions)
        monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
        scheduler = DataScheduler()
        monkeypatch.setattr(scheduler, "_read_promotion_recovery_probe", AsyncMock(return_value={"status": "no_completed_batch"}))
        monkeypatch.setattr(scheduler, "_ensure_close_snapshot_ready", AsyncMock(return_value={"ready": False}))
        result = await scheduler._promotion_recovery_tick()
        assert result["attempt_status"] == "blocked"
        assert result["recorded_predictions"] == 0
        async with sessions() as db:
            runs = (await db.scalars(select(PromotionPredictionRun))).all()
            assert len(runs) == 1
            assert runs[0].snapshot_context == "promotion_1510"
            assert runs[0].as_of_at == frozen_clock.current
            assert runs[0].status == "blocked" and not runs[0].gate_passed
            assert "generation_pending" in runs[0].metadata_json
            assert not (await db.scalars(select(PromotionPredictionSnapshot))).all()
        assert scheduler._promotion_snapshot_completed_contexts == set()
    finally:
        await engine.dispose()
