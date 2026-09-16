"""Isolated consumer proofs, not vendor recovery or a real ready trading day."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import threading
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.promotion.modeling import daily_consumer as consumer
from app.promotion.modeling import daily_materialization as materialization
from app.promotion.modeling.daily_materialization import LockedReady, bind_candidates, bind_candidate
from app.promotion.modeling.daily_materials import ReviewedSourcePolicy, encode, decode
from app.promotion.modeling.feature_coverage import hist_feature_coverage
from app.promotion.modeling.features import FEATURE_VERSION, extract_point_in_time_features
from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from test_promotion_ledger import ledger_session

AT = datetime(2026, 9, 7, 20, 0)
DAY = AT.date()
POLICY = ReviewedSourcePolicy("isolated_consumer_fixture_only", {"fixture:v1": decode})


def locked_fixture(at=AT, codes=("600001", "600002")):
    # Pure binding fixture ONLY. Physical archive validation has its separate suite.
    values = {name: 0.0 for name in consumer.HIST_NAMES}
    values["hist_fund_main_net_inflow"] = -3.0
    payload = {"trade_date": at.date().isoformat(), "policy_id": POLICY.policy_id,
        "profile": materialization.PROFILE,
        "materialized_at": (at-timedelta(minutes=5)).isoformat(),
        "field_evidence_clock": {"source_at": (at-timedelta(hours=4)).isoformat(),
            "received_at": (at-timedelta(minutes=10)).isoformat(),
            "observed_at": (at-timedelta(minutes=9)).isoformat()},
        "values_by_code": {code: dict(values) for code in codes}}
    receipt = {"ready_ref": {"path": "objects/"+"a"*64+".blob", "sha256": "a"*64, "size": 123}}
    return LockedReady(encode(payload), encode(receipt), at)


def records(at=AT):
    from app.api.v1 import promotion
    output = []
    for target, code, probability in ((1, "600001", .2), (2, "600002", .3)):
        item = {"code": code, "target_board": target, "name": "隔离测试", "candidate_route": "pre_board_probe_start" if target == 1 else "second_board_promotion",
            "probability": probability, "raw_probability": probability-.01,
            "trade_ready": False, "prediction_rank_eligible": True,
            "prediction_actionable": False, "signal_status": "watch",
            "probability_factors": {"route_score": 42.0}}
        output.extend(promotion._annotate_prediction_record_metadata([item], [item], ranked_limit=12,
            recall_ranked_candidates=[item], recall_ranked_limit=30, rank_eligible_candidates=[item],
            snapshot_source="schedule", snapshot_context="promotion_2000", recorded_at=at,
            candidate_anchor_trade_date=at.date()))
    return output


def context(locked=None):
    locked = locked or locked_fixture()
    return consumer.DailyHistContext(AT, "ready", "", locked)


def prepare(tmp_path, **kwargs):
    return consumer.prepare_daily_hist_context(snapshot_source="schedule", snapshot_context="promotion_2000",
        request_started_at=AT+timedelta(microseconds=999999), archive_root=tmp_path, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("source,ctx", [("page", "promotion_2000"), ("schedule", "promotion_1510"),
    ("schedule", "promotion_0925"), ("schedule", "promotion_0935"), ("schedule", "promotion_1305")])
async def test_non_2000_and_page_do_no_archive_io(tmp_path, monkeypatch, source, ctx):
    def forbidden(*args, **kwargs):
        pytest.fail("non-20:00 attempted archive read")
    monkeypatch.setattr(consumer, "MaterialArchive", forbidden)
    result = await consumer.prepare_daily_hist_context(snapshot_source=source, snapshot_context=ctx,
        request_started_at=AT, archive_root=tmp_path, policy=POLICY)
    assert result is None
    items = records()
    unchanged, summary = await consumer.attach_daily_hist_evidence(items, result, prediction_trade_dates={1: DAY, 2: DAY})
    assert unchanged is items and summary["status"] == "not_applicable"


@pytest.mark.asyncio
async def test_default_empty_policy_does_not_scan_or_create_even_a_path(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("default unreviewed policy must not read archive")
    monkeypatch.setattr(consumer, "MaterialArchive", forbidden)
    result = await prepare(tmp_path / "missing")
    assert result.cutoff == AT
    assert result.reason == "source_protocol_unreviewed"
    assert not (tmp_path / "missing").exists()
    items = records()
    before = deepcopy(items)
    attached, summary = await consumer.attach_daily_hist_evidence(items, result, prediction_trade_dates={1: DAY, 2: DAY})
    assert summary["blocked_count"] == 2 and summary["ready_count"] == 0
    assert items == before
    for old, new in zip(items, attached):
        assert {k:v for k,v in new.items() if k != "probability_factors"} == {k:v for k,v in old.items() if k != "probability_factors"}
        assert new["probability_factors"] == {**old["probability_factors"], consumer.STATUS_KEY: new["probability_factors"][consumer.STATUS_KEY]}
        assert "hist_return_1d" not in new["probability_factors"]


@pytest.mark.asyncio
async def test_lock_in_worker_seconds_cutoff_one_revision_no_orm(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []
    loop_thread = threading.get_ident()
    frozen = locked_fixture()
    def lock(archive, *, trade_date, cutoff):
        calls.append((threading.get_ident(), trade_date, cutoff))
        assert not hasattr(archive, "db")
        started.set()
        assert release.wait(2)
        return frozen
    monkeypatch.setattr(consumer, "lock_ready", lock)
    task = asyncio.create_task(prepare(tmp_path, policy=POLICY))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(.001)
        assert started.is_set() and not task.done()  # loop is responsive while worker waits
        release.set()
        result = await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    assert calls == [(calls[0][0], DAY, AT)] and calls[0][0] != loop_thread
    def forbidden(*args, **kwargs):
        pytest.fail("binding must not reselect newer ready revision")
    monkeypatch.setattr(consumer, "lock_ready", forbidden)
    attached, summary = await consumer.attach_daily_hist_evidence(records(), result, prediction_trade_dates={1: DAY, 2: DAY})
    assert summary["ready_count"] == 2
    for item in attached:
        factors = item["probability_factors"]
        assert factors["hist_materialization"]["as_of_at"] == AT.isoformat()
        assert factors["hist_fund_main_net_inflow"] == -3
        assert factors["hist_return_1d"] == 0


@pytest.mark.asyncio
async def test_timeout_does_not_launch_overlapping_worker(tmp_path, monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def lock(*args, **kwargs):
        started.set()
        try:
            assert release.wait(2)
            return locked_fixture()
        finally:
            finished.set()
    monkeypatch.setattr(consumer, "lock_ready", lock)
    try:
        result = await prepare(tmp_path, policy=POLICY, timeout_sec=.03)
        assert started.is_set() and result.reason == "material_read_timeout"
        again = await prepare(tmp_path, policy=POLICY, timeout_sec=1)
        assert again.reason == "material_reader_busy"
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    # wait for the wrapper finally to release its lock, without spawning a scan
    await asyncio.to_thread(lambda: consumer._READ_LOCK.acquire(timeout=2))
    consumer._READ_LOCK.release()
    assert (await prepare(tmp_path, policy=POLICY)).status == "ready"


@pytest.mark.asyncio
async def test_cancellation_propagates_not_fake_blocked_result(tmp_path, monkeypatch):
    release = threading.Event()
    started = threading.Event()
    def lock(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return None
    monkeypatch.setattr(consumer, "lock_ready", lock)
    task = asyncio.create_task(prepare(tmp_path, policy=POLICY))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        acquired = await asyncio.to_thread(lambda: consumer._READ_LOCK.acquire(timeout=2))
        assert acquired
        consumer._READ_LOCK.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ValueError("bad sha"), OSError("missing file"), TypeError("bad structure")])
async def test_bad_archive_blocks_only_research(tmp_path, monkeypatch, error):
    def lock(*args, **kwargs):
        raise error
    monkeypatch.setattr(consumer, "lock_ready", lock)
    result = await prepare(tmp_path, policy=POLICY)
    assert result.reason == "ready_verification_failed"
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "5"])
async def test_invalid_budget_rejected(tmp_path, timeout):
    with pytest.raises(ValueError):
        await prepare(tmp_path, timeout_sec=timeout)


@pytest.mark.asyncio
async def test_invalid_aware_clock_rejected(tmp_path):
    with pytest.raises(ValueError):
        await consumer.prepare_daily_hist_context(snapshot_source="schedule", snapshot_context="promotion_2000",
            request_started_at=AT.replace(tzinfo=timezone.utc), archive_root=tmp_path)


def test_batch_binding_decodes_only_twice_and_keeps_old_single_api(monkeypatch):
    calls = []
    real = materialization.decode
    monkeypatch.setattr(materialization, "decode", lambda raw: (calls.append(len(raw)), real(raw))[1])
    locked = locked_fixture()
    batch = bind_candidates(locked, codes=("600001", "600002", "600003"))
    assert len(calls) == 2
    assert batch["600003"]["status"] == "blocked"
    assert bind_candidate(locked, code="600001") == batch["600001"]
    assert bind_candidates(None, codes=("600001",))["600001"]["reason"] == "no_ready_before_cutoff"


@pytest.mark.parametrize("code", [None, "６００００１", "60001", "6000001", 600001])
def test_batch_rejects_non_ascii_code(code):
    with pytest.raises(ValueError):
        bind_candidates(locked_fixture(), codes=(code,))


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("prediction_snapshot_source", "page"),
    ("prediction_snapshot_context", "promotion_1510"),
    ("prediction_snapshot_recorded_at", (AT+timedelta(microseconds=1)).isoformat()),
    ("prediction_candidate_anchor_trade_date", "2026-09-04")])
async def test_wrong_candidate_context_preserves_candidate_but_adds_no_hist(field, value):
    items = records()
    items[0]["probability_factors"][field] = value
    attached, summary = await consumer.attach_daily_hist_evidence(items, context(), prediction_trade_dates={1: DAY, 2: DAY})
    assert len(attached) == 2 and summary["ready_count"] == 1 and summary["blocked_count"] == 1
    assert "hist_return_1d" not in attached[0]["probability_factors"]
    assert attached[0]["probability"] == items[0]["probability"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing_field", "extra_output", "invalid_value", "future_materialized", "wrong_cutoff", "old_material_day", "missing_stock", "wrong_target_day", "duplicate_lane", "existing_hist"])
async def test_incomplete_or_conflicting_binding_never_partially_injects(bad):
    items = records()
    locked = locked_fixture()
    payload = decode(locked.payload)
    trade_dates = {1: DAY, 2: DAY}
    if bad == "missing_field":
        payload["values_by_code"]["600001"].pop("hist_return_1d")
    elif bad == "extra_output":
        payload["values_by_code"]["600001"]["probability"] = 1.0
    elif bad == "invalid_value":
        payload["values_by_code"]["600001"]["hist_return_1d"] = True
    elif bad == "future_materialized":
        payload["materialized_at"] = (AT+timedelta(microseconds=1)).isoformat()
    elif bad == "wrong_cutoff":
        locked = replace(locked, cutoff=AT+timedelta(seconds=1))
    elif bad == "old_material_day":
        payload["trade_date"] = "2026-09-04"
    elif bad == "missing_stock":
        payload["values_by_code"].pop("600001")
    elif bad == "wrong_target_day":
        trade_dates[1] = DAY-timedelta(days=3)
    elif bad == "duplicate_lane":
        items.append(deepcopy(items[0]))
    elif bad == "existing_hist":
        items[0]["probability_factors"]["hist_return_1d"] = 99
    locked = replace(locked, payload=encode(payload))
    before = deepcopy(items)
    attached, summary = await consumer.attach_daily_hist_evidence(items, context(locked), prediction_trade_dates=trade_dates)
    assert len(attached) == len(items) and items == before
    assert summary["blocked_count"] >= 1
    factors = attached[0]["probability_factors"]
    assert factors.get("hist_return_1d") == (99 if bad == "existing_hist" else None)
    assert "hist_materialization" not in factors
    assert factors[consumer.STATUS_KEY]["status"] == "blocked"


@pytest.mark.asyncio
async def test_binding_failure_and_empty_candidate_universe(monkeypatch):
    def failed(*args, **kwargs):
        raise ValueError("test fixture binding failure")
    monkeypatch.setattr(consumer, "bind_candidates", failed)
    out, result = await consumer.attach_daily_hist_evidence(records(), context(), prediction_trade_dates={1: DAY, 2: DAY})
    assert result["blocked_reasons"] == {"candidate_binding_failed": 2}
    assert all("hist_materialization" not in i["probability_factors"] for i in out)
    out, result = await consumer.attach_daily_hist_evidence([], context(), prediction_trade_dates={1: DAY})
    assert out == [] and result["status"] == "blocked" and result["candidate_count"] == 0


@pytest.mark.asyncio
async def test_real_annotation_and_append_only_ledger_preserve_cutoff_and_production_fields(ledger_session):
    from app.api.v1 import promotion
    from app.promotion.ledger import append_prediction_run
    from app.promotion.versioning import get_promotion_model_identity
    identity = get_promotion_model_identity()
    original = records(AT+timedelta(microseconds=987654))
    before = deepcopy(original)
    old = await append_prediction_run(ledger_session, original, {1: DAY, 2: DAY}, identity=identity,
        snapshot_source="schedule", snapshot_context="promotion_2000", quality_gate={"gate_passed": True})
    await ledger_session.commit()
    old_snapshots = list((await ledger_session.scalars(select(PromotionPredictionSnapshot).where(PromotionPredictionSnapshot.run_id == old.run_id))).all())
    old_bytes = [(r.id, r.features_json, r.record_key) for r in old_snapshots]
    attached, summary = await consumer.attach_daily_hist_evidence(original, context(), prediction_trade_dates={1: DAY, 2: DAY})
    assert original == before and summary["ready_count"] == 2
    for old_item, new in zip(original, attached):
        cleaned = {**new, "probability_factors": {k:v for k,v in new["probability_factors"].items()
            if k not in consumer.HIST_NAMES and k not in (consumer.STATUS_KEY, "hist_materialization")}}
        assert cleaned == old_item  # all scores/ranks/eligibility/formal counts unchanged
    new = await append_prediction_run(ledger_session, attached, {1: DAY, 2: DAY}, identity=identity,
        snapshot_source="schedule", snapshot_context="promotion_2000", quality_gate={"gate_passed": True})
    await ledger_session.commit()
    assert new.run_id != old.run_id  # fixture only, not a production rerun/backfill
    run = await ledger_session.get(PromotionPredictionRun, new.run_id)
    assert run.as_of_at == AT and run.feature_version == identity.feature_version
    assert run.model_version == identity.active_model_version
    snapshots = list((await ledger_session.scalars(select(PromotionPredictionSnapshot).where(PromotionPredictionSnapshot.run_id == new.run_id))).all())
    for snap in snapshots:
        factors = decode(snap.features_json)
        values = extract_point_in_time_features(factors)
        assert hist_feature_coverage(values, consumer.HIST_NAMES, materialization=factors["hist_materialization"],
            code=snap.code, trade_date=DAY.isoformat(), as_of_at=run.as_of_at, feature_version=FEATURE_VERSION)["passed"]
        assert factors["prediction_ranked_limit"] == 12 and factors["prediction_recall_ranked_limit"] == 30
        assert not snap.actionable and not snap.trade_gate_passed
    for snap in old_snapshots:
        await ledger_session.refresh(snap)
    assert [(r.id, r.features_json, r.record_key) for r in old_snapshots] == old_bytes


@pytest.mark.asyncio
async def test_full_internal_builder_binds_after_ranking_before_real_persistence(ledger_session, monkeypatch):
    from app.api.v1 import promotion
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 7, 20, 0, 0, 765432)
    monkeypatch.setattr(promotion, "datetime", Clock)
    monkeypatch.setattr(promotion, "_PROMOTION_LATEST_CANDIDATES_CACHE", {})
    monkeypatch.setattr(promotion, "_PROMOTION_PAGE_CANDIDATES_CACHE", {})
    events = []
    real_attach = promotion.attach_daily_hist_evidence
    real_persist = promotion._record_promotion_predictions
    candidates = records()
    candidates[0]["prediction_shape_seed"] = True  # actual recordability gate stays in force
    async def first(*args, **kwargs):
        events.append("build_first")
        return [deepcopy(candidates[0])], AT.isoformat(), {}
    async def second(*args, **kwargs):
        events.append("build_second")
        return [deepcopy(candidates[1])]
    async def overlay(db, items, **kwargs):
        assert all(not any(k in item.get("probability_factors", {}) for k in consumer.HIST_NAMES) for item in items)
        events.append("production_overlay:" + str(kwargs["target_board"]))
        return items, {"applied": False, "reason": "isolated_fixture_no_deployment"}
    async def attach(items, frozen, **kwargs):
        events.append("bind")
        assert "prediction_ranked_position" in items[0]["probability_factors"]
        return await real_attach(items, frozen, **kwargs)
    async def persist(db, items, *args, **kwargs):
        events.append("persist")
        assert all(consumer.STATUS_KEY in item["probability_factors"] for item in items)
        return await real_persist(db, items, *args, **kwargs)
    for name, result in {
        "prewarm_anomaly_snapshot": {}, "_resolve_first_board_trade_date": DAY,
        "_resolve_second_board_source_trade_date": DAY, "_load_filtered_limit_ups": [],
        "_enrich_market_ladder_context": {}, "build_market_regime_snapshot": {},
        "_refresh_promotion_learning": 0, "_load_promotion_learning_stats": {},
        "_resolve_promotion_snapshot_news_end_time": AT, "_build_prediction_snapshot_health": {},
        "_build_actual_limit_up_replay": {}, "_persist_dashboard_snapshot": None,
    }.items():
        monkeypatch.setattr(promotion, name, AsyncMock(return_value=result))
    monkeypatch.setattr(promotion, "_build_first_board_candidates", first)
    monkeypatch.setattr(promotion, "_build_second_board_candidates", second)
    monkeypatch.setattr(promotion, "apply_active_promotion_overlay", overlay)
    monkeypatch.setattr(promotion, "attach_daily_hist_evidence", attach)
    monkeypatch.setattr(promotion, "_record_promotion_predictions", persist)
    outputs, snapshots_by_arm = [], []
    for ready in (False, True):
        async def prepare_hook(**kwargs):
            events.append("lock")
            assert kwargs["request_started_at"].microsecond == 765432
            return context() if ready else consumer.DailyHistContext(AT, "blocked", "source_protocol_unreviewed")
        monkeypatch.setattr(promotion, "prepare_daily_hist_context", prepare_hook)
        events.clear()
        payload = await promotion.build_promotion_candidates(db=ledger_session, snapshot_source="schedule",
            snapshot_context="promotion_2000", quality_gate={"gate_passed": True, "status": "passed"})
        assert events == ["lock", "build_first", "production_overlay:1", "build_second", "production_overlay:2", "bind", "persist"]
        run_id = payload["learning"]["prediction_run_id"]
        assert run_id is not None
        run = await ledger_session.get(PromotionPredictionRun, run_id)
        # Preserve the independent producer clock for start-barrier ordering;
        # candidate display metadata remains at its original second precision.
        assert run.as_of_at == AT.replace(microsecond=765432)
        assert run.snapshot_batch_key.endswith("20:00:00.765432")
        snapshots = list((await ledger_session.scalars(select(PromotionPredictionSnapshot).where(PromotionPredictionSnapshot.run_id == run_id))).all())
        assert len(snapshots) == 2
        assert all(("hist_return_1d" in decode(s.features_json)) is ready for s in snapshots)
        outputs.append(payload)
        snapshots_by_arm.append(snapshots)
    for key in ("first_board_candidates", "second_board_candidates", "ranked_first_board_candidates", "ranked_first_board_recall_candidates", "ranked_second_board_candidates", "model_runtime", "quality_gate", "formal_first_board_limit", "recall_first_board_limit"):
        assert outputs[0][key] == outputs[1][key]
    assert outputs[0]["prediction_health"]["daily_hist_research"]["status"] == "blocked"
    assert outputs[1]["prediction_health"]["daily_hist_research"]["status"] == "ready"
    for old in snapshots_by_arm[0]:
        await ledger_session.refresh(old)
        assert "hist_return_1d" not in decode(old.features_json)


@pytest.mark.asyncio
async def test_cached_page_never_reads_daily_archive(monkeypatch):
    from app.api.v1 import promotion
    import time
    class NoDatabaseAccess:
        pass
    db = NoDatabaseAccess()
    monkeypatch.setattr(promotion, "_promotion_page_cache_key", lambda *args: "isolated_page")
    monkeypatch.setattr(promotion, "_promotion_cache_scope", lambda *args: "isolated_scope")
    monkeypatch.setattr(promotion, "_PROMOTION_PAGE_CANDIDATES_CACHE",
        {"isolated_page": (time.monotonic(), {"prediction_health": {}, "fixture": True})})
    def forbidden(*args, **kwargs):
        pytest.fail("cached page must not access research archive")
    monkeypatch.setattr(consumer, "MaterialArchive", forbidden)
    result = await promotion.build_promotion_candidates(db=db, snapshot_source="page", snapshot_context="page")
    assert result["fixture"] and result["prediction_health"]["page_cache_hit"]
