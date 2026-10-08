"""Official attempt identity and direct-ledger contracts; isolated DBs, no orders."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.api.v1 import paper, promotion
from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from app.models.signal import PromotionPredictionRecord
from app.models.stock import StockSpot
from app.promotion.ledger import ScheduleBatch, append_prediction_run, append_blocked_prediction_run
from app.promotion.persistence import record_promotion_predictions
from app.promotion.versioning import (
    ProbabilityContractError, get_promotion_model_identity, project_promotion_probability,
)
from test_promotion_ledger import ledger_session
from test_promotion_api import promotion_api_env
from test_promotion_probability_contract import candidate, INVALID
from test_paper_api import paper_client, _governed_promotion_run, _governed_promotion_snapshot

DAY = date(2026, 9, 7)
AT = datetime(2026, 9, 7, 9, 35)


async def persist(db, items, *, batch=None, policy=lambda item: True, source="schedule", dates=None):
    identity = get_promotion_model_identity()
    return await record_promotion_predictions(
        db, items, {1: DAY, 2: DAY} if dates is None else dates,
        snapshot_source=source, model_version=identity.active_model_version,
        model_identity=identity, is_recordable=policy,
        reason_builder=lambda item: {}, learning_bucket_builder=lambda target, route: "test",
        quality_gate={"gate_passed": True}, schedule_batch=batch,
    )


async def append(db, items):
    return await append_prediction_run(
        db, items, {1: DAY}, identity=get_promotion_model_identity(),
        snapshot_source="schedule", snapshot_context="promotion_0935",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["p_raw", "p_calibrated", "production_probability"])
@pytest.mark.parametrize("value", INVALID)
async def test_direct_ledger_validates_original_batch_before_storage_or_identity(field, value):
    good, bad = candidate(), candidate()
    bad[field] = value
    bad["code"] = ""  # Previously skipped and therefore hidden by the direct writer.
    with pytest.raises(ProbabilityContractError):
        await append(None, [good, bad])


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["probability", "raw_probability"])
@pytest.mark.parametrize("value", INVALID)
async def test_direct_ledger_never_defaults_bad_legacy_probability(field, value):
    item = {"code": "600001", "target_board": 1, "probability": .5,
            "raw_probability": 0, field: value}
    with pytest.raises(ProbabilityContractError):
        await append(None, [item])


@pytest.mark.asyncio
async def test_direct_ledger_freezes_exact_three_values_without_mutating_input(ledger_session):
    item = candidate()
    before = deepcopy(item)
    result = await append(ledger_session, [item])
    row = await ledger_session.scalar(select(PromotionPredictionSnapshot))
    assert item == before
    assert row.raw_probability == .4 and row.calibrated_probability == 0
    assert json.loads(row.features_json)["probability_contract"] == {
        "probability_contract_version": item["probability_contract_version"],
        "p_raw": .4, "p_calibrated": .2, "production_probability": 0,
    }
    retry = await append(ledger_session, [project_promotion_probability(item)])
    assert retry.run_id == result.run_id and not retry.created


@pytest.mark.asyncio
@pytest.mark.parametrize("frozen", [None, [], {"probability": .98}, {
    "probability_contract_version": "promotion_probability_v1",
    "p_raw": .4, "p_calibrated": .3, "production_probability": 0,
}])
async def test_conflicting_or_malformed_frozen_probability_cannot_be_overwritten(frozen):
    item = candidate()
    item["probability_factors"]["probability_contract"] = frozen
    with pytest.raises(ProbabilityContractError):
        await append(None, [item])
    with pytest.raises(ProbabilityContractError):
        await persist(None, [item])


@pytest.mark.asyncio
async def test_direct_ledger_rejects_partial_identity_before_storage():
    bad = {**candidate(), "code": ""}
    with pytest.raises(ValueError, match="missing_code_or_trade_date"):
        await append(None, [candidate(), bad])


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [
    ScheduleBatch("page", AT), ScheduleBatch("promotion_9999", AT),
    ScheduleBatch(None, AT), ScheduleBatch([], AT),
    ScheduleBatch("promotion_0935", None), ScheduleBatch("promotion_0935", True),
    ScheduleBatch("promotion_0935", AT.replace(tzinfo=timezone.utc)),
    ScheduleBatch("promotion_0935", datetime.now() + timedelta(days=1)),
])
async def test_invalid_independent_envelope_rejected_before_storage(batch):
    with pytest.raises(ValueError, match="schedule_batch"):
        await persist(None, [], batch=batch)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,counts", [
    ("completed", [0, 0, 0]), ("empty_unproven", [True, 0, 0]),
    ("empty_unproven", [None, 0, 0]), ("empty_unproven", [-1, 0, 0]),
    ("empty_unproven", [0, 1, 0]), ("empty_unproven", [1, 0, 0]),
    ("fully_filtered", [0, 0, 0]), ("fully_filtered", [1, 1, 0]),
    ("invalid_candidate_identity", [0, 0, 0]), ("invalid_candidate_identity", [1, 1, 1]),
])
async def test_direct_blocking_writer_cannot_mislabel_counts(status, counts):
    with pytest.raises(ValueError, match="schedule_batch"):
        await append_blocked_prediction_run(
            None, {1: DAY}, identity=get_promotion_model_identity(),
            batch=ScheduleBatch("promotion_0935", AT),
            persistence=dict(zip(
                ["status", "input_count", "recordable_count", "prepared_count"],
                [status, *counts],
            )),
        )


@pytest.mark.asyncio
async def test_page_cannot_append_an_official_empty_run():
    with pytest.raises(ValueError, match="official_source_required"):
        await persist(None, [], source="page", batch=ScheduleBatch("promotion_0935", AT))
    assert (await persist(None, [], source="page")).ledger is None


@pytest.mark.asyncio
@pytest.mark.parametrize("dates", [{True: DAY}, {3: DAY}, {1: None}, {1: AT}, {1: DAY + timedelta(days=1)}])
async def test_invalid_target_dates_rejected_before_empty_run_storage(dates):
    with pytest.raises(ValueError, match="invalid_target_trade_date"):
        await persist(None, [], batch=ScheduleBatch("promotion_0935", AT), dates=dates)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["empty_unproven", "fully_filtered", "invalid_candidate_identity"])
async def test_blocking_attempt_is_idempotent_append_only_and_preserves_legacy(ledger_session, mode):
    valid = candidate()
    original = await persist(ledger_session, [valid])
    original_row = await ledger_session.scalar(select(PromotionPredictionRecord))
    original_evidence = original_row.factors_json
    if mode == "empty_unproven":
        items, policy = [], lambda item: True
    elif mode == "fully_filtered":
        items, policy = [valid, valid], lambda item: False
    else:
        items, policy = [valid, {**valid, "code": ""}], lambda item: True
    batch = ScheduleBatch("promotion_0935", AT)
    first = await persist(ledger_session, items, batch=batch, policy=policy)
    retry = await persist(ledger_session, items, batch=batch, policy=policy)
    later = await persist(ledger_session, items,
        batch=ScheduleBatch("promotion_0935", AT + timedelta(seconds=1)), policy=policy)
    assert first.status == mode and first.touched == 0
    assert first.ledger.snapshot_count == 0 and first.ledger.created
    assert retry.ledger.run_id == first.ledger.run_id and not retry.ledger.created
    assert later.ledger.run_id != first.ledger.run_id
    assert first.as_payload()["ledger_recorded"] is True
    if mode == "invalid_candidate_identity":
        assert first.prepared_count == 1
        assert first.as_payload()["invalid_identity_count"] == 1
    run = await ledger_session.get(PromotionPredictionRun, first.ledger.run_id)
    assert run.status == "blocked" and run.gate_passed is False
    assert run.candidate_count == run.actionable_count == run.ranked_count == 0
    assert run.as_of_at == AT and run.as_of_at <= run.created_at <= run.completed_at
    metadata = json.loads(run.metadata_json)
    assert metadata["universe_complete"] is None
    assert metadata["persistence"]["status"] == mode
    assert metadata["quality_gate"]["gate_passed"] is True  # Not an override of blocked state.
    assert run.reference_trade_date == DAY
    assert await ledger_session.scalar(select(func.count()).select_from(PromotionPredictionRun)) == 3
    assert await ledger_session.scalar(select(func.count()).select_from(PromotionPredictionSnapshot)) == 1
    assert await ledger_session.get(PromotionPredictionRun, original.ledger.run_id) is not None
    assert (await ledger_session.scalar(select(PromotionPredictionRecord))).factors_json == original_evidence
    await ledger_session.commit()
    run.status = "completed"
    with pytest.raises(RuntimeError, match="append-only"):
        await ledger_session.flush()
    await ledger_session.rollback()


@pytest.mark.asyncio
async def test_empty_attempt_uses_actual_signal_session_not_stale_evidence_day(ledger_session):
    result = await persist(ledger_session, [], batch=ScheduleBatch("promotion_1510", AT),
        dates={1: date(2026, 9, 3), 2: date(2026, 9, 4)})
    run = await ledger_session.get(PromotionPredictionRun, result.ledger.run_id)
    assert run.reference_trade_date == DAY
    assert json.loads(run.metadata_json)["trade_date_by_target"] == {"1": "2026-09-03", "2": "2026-09-04"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["empty_unproven", "fully_filtered", "invalid_candidate_identity"])
@pytest.mark.parametrize("account,target,route", [
    ("promotion", 2, "second_board_promotion"),
    ("mainline", 1, "mainline_spread_start"),
    ("auction", 1, "auction_surge_start"),
])
async def test_actual_empty_writer_blocks_old_candidates_then_valid_retry_recovers(
    paper_client, monkeypatch, mode, account, target, route,
):
    _, maker = paper_client
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=date(2026, 9, 4)))
    async with maker() as db:
        old = _governed_promotion_run(run_key="old-valid", reference_trade_date=DAY,
            snapshot_context="promotion_0935", as_of_at=AT - timedelta(seconds=1))
        db.add(old)
        await db.flush()
        db.add_all([
            _governed_promotion_snapshot(run_id=old.id, record_key="old", code="600001",
                prediction_trade_date=DAY, target_board=target, route=route, probability=.9),
            StockSpot(code="600001", name="test", price=10.15, prev_close=10,
                change_pct=1.5, volume_ratio=1.8),
        ])
        await db.flush()
        options = dict(limit=10, trade_date=DAY, account_name=account, now=AT + timedelta(minutes=10))
        before, _ = await paper._promotion_route_buy_candidates(db, **options)
        assert [row["code"] for row in before] == ["600001"]
        if mode == "empty_unproven":
            items, policy = [], lambda item: True
        elif mode == "fully_filtered":
            items, policy = [candidate()], lambda item: False
        else:
            items, policy = [{**candidate(), "code": ""}], lambda item: True
        blocked = await persist(db, items, batch=ScheduleBatch("promotion_0935", AT), policy=policy)
        diagnostics = []
        selected, notes = await paper._promotion_route_buy_candidates(db, diagnostics=diagnostics, **options)
        assert selected == [] and "禁止回退" in notes[0]
        assert diagnostics[0]["reason_code"] == "prediction_batch_blocked"
        assert diagnostics[0]["candidate"]["persistence_status"] == mode
        # A genuinely later completed fixture is allowed; neither blocker nor old row is rewritten.
        later = _governed_promotion_run(run_key="new-valid", reference_trade_date=DAY,
            snapshot_context="promotion_0935", as_of_at=AT + timedelta(seconds=1))
        db.add(later)
        await db.flush()
        db.add(_governed_promotion_snapshot(run_id=later.id, record_key="new", code="600001",
            prediction_trade_date=DAY, target_board=target, route=route, probability=.8))
        await db.flush()
        selected, _ = await paper._promotion_route_buy_candidates(db, **options)
        assert len(selected) == 1 and selected[0]["probability"] == .8
        assert (await db.get(PromotionPredictionRun, blocked.ledger.run_id)).status == "blocked"
        assert (await db.get(PromotionPredictionRun, old.id)).status == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("context,attempt_at,blocked_accounts", [
    ("promotion_2000", datetime(2026, 9, 4, 20), {"promotion", "mainline", "auction"}),
    ("promotion_1000", datetime(2026, 9, 7, 10), {"mainline"}),
    ("promotion_0935", datetime(2026, 9, 3, 9, 35), set()),
])
@pytest.mark.parametrize("account,target,route", [
    ("promotion", 2, "second_board_promotion"),
    ("mainline", 1, "mainline_spread_start"),
    ("auction", 1, "auction_surge_start"),
])
@pytest.mark.parametrize("intraday_refresh", [False, True])
async def test_empty_blocker_respects_account_context_and_signal_date(
    paper_client, monkeypatch, context, attempt_at, blocked_accounts, account, target, route,
    intraday_refresh,
):
    _, maker = paper_client
    # B can now opt into later intraday contexts. Freeze both configurations
    # instead of inheriting the deployment setting in this ledger isolation test.
    monkeypatch.setattr(paper.settings, "PAPER_PROMOTION_INTRADAY_REFRESH_ENABLED", intraday_refresh)
    previous = date(2026, 9, 4)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=previous))
    async with maker() as db:
        old = _governed_promotion_run(run_key="allowed-previous-close", reference_trade_date=previous,
            snapshot_context="promotion_1510", as_of_at=datetime(2026, 9, 4, 15, 10))
        db.add(old)
        await db.flush()
        db.add_all([
            _governed_promotion_snapshot(run_id=old.id, record_key="old-close", code="600001",
                prediction_trade_date=previous, target_board=target, route=route, probability=.9),
            StockSpot(code="600001", name="test", price=10.15, prev_close=10,
                change_pct=1.5, volume_ratio=1.8),
        ])
        await db.flush()
        await persist(db, [], batch=ScheduleBatch(context, attempt_at), dates={})
        selected, notes = await paper._promotion_route_buy_candidates(
            db, limit=10, trade_date=DAY, account_name=account, now=datetime(2026, 9, 7, 10, 5))
        blocked = account in blocked_accounts or (
            context == "promotion_1000" and account == "promotion" and intraday_refresh
        )
        if blocked:
            assert not selected and "禁止回退" in notes[0]
        else:
            assert [row["code"] for row in selected] == ["600001"]


@pytest.mark.asyncio
async def test_wrapper_passes_independent_empty_identity(ledger_session):
    result = await promotion._record_promotion_predictions(
        ledger_session, [], {1: DAY, 2: DAY}, snapshot_source="schedule",
        quality_gate={"gate_passed": True}, return_details=True,
        schedule_batch=ScheduleBatch("promotion_0935", AT),
    )
    assert result.status == "empty_unproven" and result.ledger is not None


@pytest.mark.asyncio
async def test_scheduler_does_not_complete_or_start_shadow_for_blocking_run(monkeypatch):
    from app.data.scheduler import DataScheduler, trade_calendar
    from app.strategy.auction import auction_collector
    from app.core.data_quality import data_quality_guard
    from app.promotion import shadow
    from app.config.settings import settings

    scheduler = DataScheduler()
    monkeypatch.setattr(scheduler, "_begin_promotion_generation", AsyncMock(return_value={"run_id": 122}))
    monkeypatch.setattr(trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    monkeypatch.setattr(trade_calendar, "get_trade_session", lambda: "afternoon")
    monkeypatch.setattr(scheduler, "_ensure_fresh_promotion_news",
        AsyncMock(return_value={"status": "fresh"}))
    monkeypatch.setattr(auction_collector, "get_snapshot_health",
        AsyncMock(return_value={"missing": False, "degraded": False}))
    monkeypatch.setattr(data_quality_guard, "audit_prediction_data",
        AsyncMock(return_value={"gate_passed": True}))
    monkeypatch.setattr(settings, "PROMOTION_QUALITY_GATE_MODE", "enforce")
    monkeypatch.setattr(settings, "PROMOTION_SHADOW_AUTOMATION_ENABLED", True)
    builder = AsyncMock(return_value={
        "learning": {"recorded_predictions": 0, "prediction_run_id": 123},
        "prediction_health": {"persistence": {"status": "empty_unproven", "ledger_recorded": True}},
    })
    monkeypatch.setattr(promotion, "build_promotion_candidates", builder)
    shadow_runner = AsyncMock()
    monkeypatch.setattr(shadow, "run_eligible_shadows_for_prediction_run", shadow_runner)
    result = await scheduler._build_promotion_snapshot_once(
        session=object(), trigger="promotion_prediction_0935")
    assert result["learning"]["prediction_run_id"] == 123
    builder.assert_awaited_once()
    shadow_runner.assert_not_awaited()
    assert scheduler._promotion_snapshot_completed_contexts == set()


@pytest.mark.asyncio
async def test_internal_builder_commits_empty_blocker_without_any_other_writes(promotion_api_env, monkeypatch):
    maker, _ = promotion_api_env
    # Isolate all market/source inputs; keep the real builder, adapter, ledger
    # and transaction boundary. No calendar/network/research-artifact operation.
    values = {
        "prepare_daily_hist_context": None,
        "prewarm_anomaly_snapshot": {},
        "_resolve_first_board_trade_date": DAY,
        "_resolve_second_board_source_trade_date": DAY,
        "_load_filtered_limit_ups": [],
        "_enrich_market_ladder_context": {},
        "_validate_official_snapshot_clock": None,
        "_refresh_promotion_learning": 0,
        "_load_promotion_learning_stats": {},
        "_resolve_promotion_snapshot_news_end_time": AT,
        "_build_first_board_candidates": ([], AT.isoformat(), {}),
        "_build_second_board_candidates": [],
        "apply_active_promotion_overlay": ([], {"applied": False}),
        "_build_prediction_snapshot_health": {},
        "_build_actual_limit_up_replay": {},
        "attach_daily_hist_evidence": ([], {}),
    }
    for name, value in values.items():
        monkeypatch.setattr(promotion, name, AsyncMock(return_value=value))
    monkeypatch.setattr(promotion, "_PROMOTION_LATEST_CANDIDATES_CACHE", {})
    async with maker() as db:
        response = await promotion.build_promotion_candidates(
            db=db, snapshot_source="schedule", snapshot_context="promotion_0935",
            quality_gate={"gate_passed": True}, compact=True,
        )
        run_id = response["learning"]["prediction_run_id"]
        assert response["learning"]["recorded_predictions"] == 0
        assert run_id is not None
        assert response["prediction_health"]["persistence"]["status"] == "empty_unproven"
        # Deliberately do NOT commit here; the internal builder must do it.
    async with maker() as db:
        run = await db.get(PromotionPredictionRun, run_id)
        assert run is not None and run.status == "blocked"
        assert run.reference_trade_date == run.as_of_at.date()
        assert await db.scalar(select(func.count()).select_from(PromotionPredictionRecord)) == 0
        assert await db.scalar(select(func.count()).select_from(PromotionPredictionSnapshot)) == 0
