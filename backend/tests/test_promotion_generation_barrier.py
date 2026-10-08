"""Generation barriers survive failed/aborted work; all DBs and sources isolated."""
import asyncio
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.api.v1 import paper, promotion
from app.config.settings import settings
from app.core.data_quality import data_quality_guard
from app.data import scheduler as scheduler_module
from app.data.scheduler import DataScheduler
from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from app.models.signal import PromotionPredictionRecord
from app.models.stock import StockSpot
from app.promotion import ledger, shadow
from app.promotion.ledger import ScheduleBatch, append_blocked_prediction_run, append_prediction_run
from app.promotion.persistence import record_promotion_predictions
from app.strategy.auction import auction_collector
from test_paper_api import paper_client, _governed_promotion_run, _governed_promotion_snapshot

DAY = date(2026, 9, 7)
ACCOUNTS = [
    ("promotion", 2, "second_board_promotion"),
    ("mainline", 1, "mainline_spread_start"),
    ("auction", 1, "auction_surge_start"),
]


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        current = datetime(2026, 9, 7, 9, 35, 0, 100)

        @classmethod
        def now(cls, tz=None):
            value = cls.fromisoformat(cls.current.isoformat())
            cls.current += timedelta(microseconds=100)
            return value

    monkeypatch.setattr(scheduler_module, "datetime", Clock)
    monkeypatch.setattr(ledger, "datetime", Clock)
    return Clock


@pytest.fixture
def env(paper_client, monkeypatch, clock):
    _, maker = paper_client
    scheduler = DataScheduler()
    scheduler.scheduler = SimpleNamespace(running=False)
    monkeypatch.setattr(scheduler_module, "async_session", maker)
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    monkeypatch.setattr(scheduler_module.trade_calendar, "get_trade_session", lambda *args, **kwargs: "morning")
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=date(2026, 9, 4)))
    monkeypatch.setattr(scheduler, "_ensure_fresh_promotion_news",
        AsyncMock(return_value={"status": "fresh"}))
    monkeypatch.setattr(scheduler, "_ensure_close_snapshot_ready",
        AsyncMock(return_value={"ready": True}))
    monkeypatch.setattr(auction_collector, "get_snapshot_health",
        AsyncMock(return_value={"missing": False, "degraded": False}))
    monkeypatch.setattr(auction_collector, "ensure_auction_data_snapshot", AsyncMock())
    monkeypatch.setattr(data_quality_guard, "audit_prediction_data",
        AsyncMock(return_value={"gate_passed": True}))
    monkeypatch.setattr(settings, "PROMOTION_QUALITY_GATE_MODE", "enforce")
    monkeypatch.setattr(settings, "PROMOTION_SHADOW_AUTOMATION_ENABLED", False)
    monkeypatch.setattr(settings, "PROMOTION_SNAPSHOT_TIMEOUT_SEC", 2)
    monkeypatch.setattr(settings, "PROMOTION_SNAPSHOT_RETRY_COOLDOWN_SEC", 0)
    return scheduler, maker, clock


async def seed_old(maker, target=2, route="second_board_promotion", *, context="promotion_0935", at=None):
    async with maker() as db:
        run = _governed_promotion_run(run_key="old-valid", reference_trade_date=DAY,
            snapshot_context=context, as_of_at=at or datetime(2026, 9, 7, 9, 34, 59))
        db.add(run)
        await db.flush()
        db.add_all([
            _governed_promotion_snapshot(run_id=run.id, record_key="old-row", code="600001",
                prediction_trade_date=DAY, target_board=target, route=route, probability=.9),
            StockSpot(code="600001", name="test", price=10.15, prev_close=10,
                change_pct=1.5, volume_ratio=1.8),
        ])
        await db.commit()
        return run.id


async def selected(maker, account="promotion", *, day=DAY):
    async with maker() as db:
        diagnostics = []
        rows, notes = await paper._promotion_route_buy_candidates(
            db, limit=10, trade_date=day, account_name=account,
            now=datetime(2026, 9, 8, 10), diagnostics=diagnostics)
        return rows, notes, diagnostics


def candidate(at, *, target=2, route="second_board_promotion"):
    return {
        "code": "600001", "name": "test", "target_board": target, "candidate_route": route,
        "raw_probability": .8, "probability": .8,
        "probability_factors": {
            "prediction_snapshot_source": "schedule",
            "prediction_snapshot_context": "promotion_0935",
            "prediction_snapshot_recorded_at": at.isoformat(timespec="seconds"),
            "prediction_snapshot_batch_key": f"schedule:promotion_0935:{at.isoformat(timespec='seconds')}",
            "prediction_trade_gate_passed": True, "prediction_actionable": True,
            "prediction_watch_only": False, "prediction_record_scope": "ranked",
            "prediction_ranked_selected": True, "prediction_ranked_position": 1,
        },
    }


async def persist(db, at, *, target=2, route="second_board_promotion", item=None):
    return await record_promotion_predictions(
        db, [candidate(at, target=target, route=route) if item is None else item],
        {1: DAY, 2: DAY}, snapshot_source="schedule",
        model_version=promotion.PROMOTION_MODEL_VERSION,
        model_identity=promotion.PROMOTION_MODEL_IDENTITY,
        is_recordable=lambda item: True, reason_builder=lambda item: {},
        learning_bucket_builder=lambda target, route: "test",
        # A completed fixture must carry its own route proof; a batch-only
        # boolean is now intentionally insufficient for B/C/D consumption.
        quality_gate={"gate_passed": True, "route_gates": {route: {"gate_passed": True}}},
        schedule_batch=ScheduleBatch("promotion_0935", at),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("account,target,route", ACCOUNTS)
@pytest.mark.parametrize("failure", ["quality", "builder_error", "timeout", "cancel"])
async def test_committed_start_marker_blocks_old_run_through_failure(
    env, monkeypatch, account, target, route, failure,
):
    scheduler, maker, clock = env
    old_id = await seed_old(maker, target, route)
    assert len((await selected(maker, account))[0]) == 1
    entered = asyncio.Event()

    async def builder(**kwargs):
        entered.set()
        if failure == "builder_error":
            raise ValueError("bad producer probability")
        await asyncio.Event().wait()

    build = AsyncMock(side_effect=builder)
    monkeypatch.setattr(promotion, "build_promotion_candidates", build)
    if failure == "quality":
        monkeypatch.setattr(data_quality_guard, "audit_prediction_data",
            AsyncMock(return_value={"gate_passed": False, "blocking_count": 1}))
    if failure == "timeout":
        # This case exercises cancellation AFTER a committed barrier, not a
        # 100ms SQLite/GC race before it exists (covered as a separate boundary).
        monkeypatch.setattr(settings, "PROMOTION_SNAPSHOT_TIMEOUT_SEC", 2)
    task = asyncio.create_task(scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935"))
    try:
        if failure == "cancel":
            await asyncio.wait_for(entered.wait(), 2)
            # Barrier must be visible in another transaction BEFORE cancellation.
            assert (await selected(maker, account))[0] == []
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            if failure == "timeout":
                assert entered.is_set(), "timeout must occur inside candidate work"
            assert result["status"] == {
                "quality": "blocked", "builder_error": "failed", "timeout": "timeout",
            }[failure]
        rows, notes, diagnostics = await selected(maker, account)
        assert rows == [] and "禁止回退" in notes[0]
        assert diagnostics[0]["candidate"]["persistence_status"] == "generation_pending"
        assert diagnostics[0]["candidate"]["input_count"] is None
        async with maker() as db:
            runs = list((await db.scalars(select(PromotionPredictionRun).order_by(PromotionPredictionRun.id))).all())
            assert len(runs) == 2
            assert runs[0].id == old_id and runs[0].status == "completed"
            marker = runs[1]
            assert marker.status == "blocked" and marker.gate_passed is False
            assert marker.candidate_count == 0
            assert marker.as_of_at <= marker.created_at <= marker.completed_at
            proof = json.loads(marker.metadata_json)
            assert proof["persistence"]["input_count"] is None
            assert proof["universe_complete"] is None
            assert await db.scalar(select(func.count()).select_from(PromotionPredictionSnapshot)) == 1
        if failure == "quality":
            build.assert_not_awaited()
        assert not scheduler._promotion_prediction_refreshing
        assert scheduler._promotion_prediction_task is None
        assert scheduler._promotion_snapshot_completed_contexts == set()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_quality_failure_is_preceded_by_committed_barrier(env, monkeypatch):
    scheduler, maker, clock = env
    clock.current = datetime(2026, 9, 7, 20, 0, 0, 100)
    await seed_old(maker, context="promotion_1510", at=datetime(2026, 9, 7, 15, 10))
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=DAY))
    monkeypatch.setattr(scheduler, "_ensure_close_snapshot_ready",
        AsyncMock(return_value={"ready": False}))
    builder = AsyncMock()
    monkeypatch.setattr(promotion, "build_promotion_candidates", builder)
    result = await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_2000")
    assert result["status"] == "blocked" and result["generation_attempt"]["run_id"]
    assert (await selected(maker, day=date(2026, 9, 8)))[0] == []
    builder.assert_not_awaited()
    scheduler._ensure_fresh_promotion_news.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("shadow_timeout", [False, True])
@pytest.mark.parametrize("account,target,route", ACCOUNTS)
async def test_same_second_success_supersedes_marker_even_if_later_shadow_times_out(
    env, monkeypatch, shadow_timeout, account, target, route,
):
    scheduler, maker, clock = env
    await seed_old(maker, target, route)

    async def build(*, db, **kwargs):
        at = clock.now()
        result = await persist(db, at, target=target, route=route)
        await db.commit()
        return {"learning": {"recorded_predictions": result.touched, "prediction_run_id": result.ledger.run_id}}

    monkeypatch.setattr(promotion, "build_promotion_candidates", build)
    if shadow_timeout:
        monkeypatch.setattr(settings, "PROMOTION_SHADOW_AUTOMATION_ENABLED", True)
        monkeypatch.setattr(settings, "PROMOTION_SNAPSHOT_TIMEOUT_SEC", .5)

        async def slow_shadow(*args, **kwargs):
            await asyncio.Event().wait()
        monkeypatch.setattr(shadow, "run_eligible_shadows_for_prediction_run", slow_shadow)
    result = await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935")
    if shadow_timeout:
        assert result["status"] == "completed_postprocessing_timeout"
    else:
        assert result["learning"]["recorded_predictions"] == 1
    assert (DAY, "promotion_0935") in scheduler._promotion_snapshot_completed_contexts
    rows, _, _ = await selected(maker, account)
    assert len(rows) == 1 and rows[0]["probability"] == .8
    async with maker() as db:
        runs = list((await db.scalars(select(PromotionPredictionRun).order_by(PromotionPredictionRun.id))).all())
        assert len(runs) == 3 and [run.status for run in runs] == ["completed", "blocked", "completed"]
        marker, success = runs[1:]
        assert marker.as_of_at.replace(microsecond=0) == success.as_of_at.replace(microsecond=0)
        assert marker.as_of_at < success.as_of_at
        assert marker.snapshot_batch_key != success.snapshot_batch_key
    assert (await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935"))["status"] == "already_completed"


@pytest.mark.asyncio
async def test_failed_attempt_then_valid_retry_preserves_marker_and_old_evidence(env, monkeypatch):
    scheduler, maker, clock = env
    await seed_old(maker)
    monkeypatch.setattr(promotion, "build_promotion_candidates", AsyncMock(side_effect=ValueError("fixture")))
    first = await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935")
    assert first["status"] == "failed"

    async def build(*, db, **kwargs):
        result = await persist(db, clock.now())
        await db.commit()
        return {"learning": {"recorded_predictions": result.touched, "prediction_run_id": result.ledger.run_id}}

    monkeypatch.setattr(promotion, "build_promotion_candidates", build)
    second = await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935")
    assert second["generation_attempt"]["run_id"] != first["generation_attempt"]["run_id"]
    assert (await selected(maker))[0][0]["probability"] == .8
    async with maker() as db:
        runs = list((await db.scalars(select(PromotionPredictionRun).order_by(PromotionPredictionRun.id))).all())
        assert [run.status for run in runs] == ["completed", "blocked", "blocked", "completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["holiday", "expired", "calendar_crossed_window", "calendar_crossed_day", "marker_error"])
async def test_no_candidate_work_without_valid_committed_attempt(env, monkeypatch, mode):
    scheduler, maker, clock = env
    build = AsyncMock()
    monkeypatch.setattr(promotion, "build_promotion_candidates", build)
    if mode == "holiday":
        monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", AsyncMock(return_value=False))
    elif mode == "expired":
        clock.current = datetime(2026, 9, 7, 12)
    elif mode in {"calendar_crossed_window", "calendar_crossed_day"}:
        async def calendar(*args):
            clock.current = (
                datetime(2026, 9, 7, 12) if mode == "calendar_crossed_window"
                else datetime(2026, 9, 8, 9, 35)
            )
            return True
        monkeypatch.setattr(scheduler_module.trade_calendar, "is_trade_day", calendar)
    else:
        monkeypatch.setattr(scheduler, "_begin_promotion_generation",
            AsyncMock(side_effect=RuntimeError("storage unavailable")))
    result = await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935")
    assert result["status"] == {
        "holiday": "not_trade_day", "expired": "expired_window",
        "calendar_crossed_window": "expired_window", "calendar_crossed_day": "expired_window",
        "marker_error": "failed",
    }[mode]
    build.assert_not_awaited()
    async with maker() as db:
        assert await db.scalar(select(func.count()).select_from(PromotionPredictionRun)) == 0


@pytest.mark.asyncio
async def test_start_barrier_does_not_commit_caller_owned_changes(env):
    scheduler, maker, _ = env
    async with maker() as caller:
        caller.add(StockSpot(code="600999", name="caller-pending", price=10))
        marker = await scheduler._begin_promotion_generation("promotion_0935")
        assert marker["run_id"]
        async with maker() as reader:
            assert await reader.scalar(select(StockSpot).where(StockSpot.code == "600999")) is None
            assert await reader.get(PromotionPredictionRun, marker["run_id"]) is not None
        await caller.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("prediction_snapshot_context", "promotion_2000"),
    ("prediction_snapshot_source", "page"),
    ("prediction_snapshot_recorded_at", None),
    ("prediction_snapshot_recorded_at", "2026-09-07T09:34:59"),
    ("prediction_snapshot_recorded_at", "2026-09-07T09:35:00+08:00"),
    ("prediction_snapshot_recorded_at", "2026-09-07T09:35:01"),
])
async def test_successful_batch_clock_or_context_mismatch_rejected_before_db(clock, field, value):
    at = clock.now()
    item = candidate(at)
    item["probability_factors"][field] = value
    with pytest.raises(ValueError, match="schedule_batch"):
        await persist(None, at, item=item)
    with pytest.raises(ValueError, match="schedule_batch"):
        await append_prediction_run(
            None, [item], {2: DAY}, identity=promotion.PROMOTION_MODEL_IDENTITY,
            snapshot_source="schedule", snapshot_context="promotion_0935",
            schedule_batch=ScheduleBatch("promotion_0935", at),
        )


@pytest.mark.asyncio
async def test_same_second_identical_candidate_batches_have_distinct_attempt_identity(env):
    _, maker, clock = env
    first_at, later_at = clock.now(), clock.now()
    assert first_at.replace(microsecond=0) == later_at.replace(microsecond=0)
    async with maker() as db:
        first = await persist(db, first_at)
        retry = await persist(db, first_at)
        later = await persist(db, later_at)
        assert first.ledger.run_id == retry.ledger.run_id and not retry.ledger.created
        assert first.ledger.run_id != later.ledger.run_id
        assert await db.scalar(select(func.count()).select_from(PromotionPredictionSnapshot)) == 2


@pytest.mark.asyncio
async def test_real_internal_builder_publishes_success_after_start_barrier(env, monkeypatch):
    scheduler, maker, clock = env
    monkeypatch.setattr(promotion, "datetime", clock)
    item = candidate(clock.now())
    item["trade_ready"] = True
    values = {
        "prepare_daily_hist_context": None,
        "prewarm_anomaly_snapshot": {},
        "_resolve_first_board_trade_date": DAY,
        "_resolve_second_board_source_trade_date": DAY,
        "_load_filtered_limit_ups": [],
        "_enrich_market_ladder_context": {},
        "_refresh_promotion_learning": 0,
        "_load_promotion_learning_stats": {},
        "_resolve_promotion_snapshot_news_end_time": clock.now(),
        "_build_first_board_candidates": ([], clock.now().isoformat(), {}),
        "_build_second_board_candidates": [item],
        "_build_prediction_snapshot_health": {},
        "_build_actual_limit_up_replay": {},
        "_persist_dashboard_snapshot": None,
    }
    for name, value in values.items():
        monkeypatch.setattr(promotion, name, AsyncMock(return_value=value))
    async def overlay(db, items, **kwargs):
        return items, {"applied": False}
    async def research(items, *args, **kwargs):
        return items, {}
    monkeypatch.setattr(promotion, "apply_active_promotion_overlay", overlay)
    monkeypatch.setattr(promotion, "attach_daily_hist_evidence", research)
    monkeypatch.setattr(promotion, "_PROMOTION_LATEST_CANDIDATES_CACHE", {})
    result = await scheduler._prewarm_promotion_candidates(trigger="promotion_prediction_0935")
    assert result["learning"]["recorded_predictions"] == 1
    # Neither the builder nor its annotation/persistence functions are mocked.
    async with maker() as db:
        marker = await db.get(PromotionPredictionRun, result["generation_attempt"]["run_id"])
        success = await db.get(PromotionPredictionRun, result["learning"]["prediction_run_id"])
        assert marker.status == "blocked" and success.status == "completed"
        assert marker.as_of_at < success.as_of_at
        row = await db.scalar(select(PromotionPredictionSnapshot).where(
            PromotionPredictionSnapshot.run_id == success.id))
        assert row.code == "600001"
        factors = json.loads(row.features_json)
        assert factors["prediction_snapshot_recorded_at"] == success.as_of_at.isoformat(timespec="seconds")
        assert success.snapshot_batch_key.endswith(success.as_of_at.isoformat())
        assert await db.scalar(select(func.count()).select_from(PromotionPredictionRecord)) == 1


@pytest.mark.asyncio
async def test_pending_denominator_cannot_be_claimed_as_zero(clock):
    with pytest.raises(ValueError, match="pending_counts"):
        await append_blocked_prediction_run(
            None, {}, identity=promotion.PROMOTION_MODEL_IDENTITY,
            batch=ScheduleBatch("promotion_0935", clock.now()),
            persistence={"status": "generation_pending", "input_count": 0,
                "recordable_count": 0, "prepared_count": 0},
        )
