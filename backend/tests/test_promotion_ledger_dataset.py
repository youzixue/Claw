import json
from datetime import date, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config.settings import settings
from app.db.session import Base
from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from app.models.signal import PromotionPredictionRecord
from app.models.stock import StockKline, LimitUpPool
from app.promotion.modeling.ledger_dataset import build_ledger_training_dataset
from app.promotion.modeling.training import train_promotion_challenger

DAY = date(2026, 9, 7)
OUTCOME = date(2026, 9, 8)
CUTOFF = datetime(2026, 9, 8, 20)


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PROMOTION_SHADOW_MIN_KLINE_ROWS", 1)
    # Explicit synthetic prepared identity view, not a production parser override.
    from test_promotion_identity_evidence import fixture_index
    async def prepared_fixture(*, known_cutoff, **kwargs):
        return fixture_index(known_cutoff=known_cutoff, codes=("600001", "600002"))
    monkeypatch.setattr("app.promotion.identity_evidence.prepare_identity_index", prepared_fixture)
    path = tmp_path / "ledger.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        # Unit mechanics dependency only; immutable source tests use real archives.
        from test_promotion_paired_material import load_material_fixture, outcome_ready_fixture
        async def prepared_outcome_fixture(*, known_cutoff, **kwargs):
            return await load_material_fixture(db)
        monkeypatch.setattr("app.promotion.outcome_materials.prepare_outcome_index", prepared_outcome_fixture)
        monkeypatch.setattr("app.promotion.outcome_materials.outcome_pair_gate", outcome_ready_fixture)
        yield db, path
    await engine.dispose()


async def seal_fixture(db):
    """Canonical fixture-only sealing; never repair persisted production snapshots."""
    from app.promotion.ledger import _candidate_identity, _hash
    runs = list((await db.scalars(select(PromotionPredictionRun))).all())
    for run in runs:
        snapshots = list((await db.scalars(select(PromotionPredictionSnapshot).where(
            PromotionPredictionSnapshot.run_id == run.id))).all())
        identities = []
        components = {}
        for s in snapshots:
            factors = json.loads(s.features_json)
            identity = _candidate_identity(dict(code=s.code, target_board=s.target_board,
                candidate_route=s.candidate_route, raw_probability=s.raw_probability,
                probability=s.calibrated_probability, probability_factors=factors), s.prediction_trade_date)
            identities.append(identity)
            await db.execute(text("UPDATE promotion_prediction_snapshot SET record_key=:key WHERE id=:id"),
                             {"key": _hash(identity), "id": s.id})
            lane = components.setdefault(str(s.target_board), {})
            for n in ("model", "feature", "data"):
                lane[n + "_versions"] = [getattr(run, n + "_version")]
        identities.sort(key=lambda i: (i["target_board"], i["prediction_trade_date"], i["code"], i["candidate_route"]))
        payload = _hash(identities)
        keys = run.snapshot_batch_key.split("|")
        metadata = {"batch_keys": keys, "model_components_by_target": components}
        key = _hash(dict(model_version=run.model_version, feature_version=run.feature_version,
            data_version=run.data_version, source=run.snapshot_source,
            context=run.snapshot_context, batch_keys=keys, payload_hash=payload))
        await db.execute(text("UPDATE promotion_prediction_run SET run_key=:key,payload_hash=:payload,metadata_json=:meta WHERE id=:id"),
                         {"key": key, "payload": payload, "meta": json.dumps(metadata), "id": run.id})
    await db.commit()
    db.expire_all()


async def calendar_quality_fixture(db, days):
    from app.models.governance import TradeCalendarModel, DataQualityRun
    from sqlalchemy import func
    cursor = min(days)
    while cursor <= max(days):
        db.add(TradeCalendarModel(trade_date=cursor, is_trade_day=cursor in days, session_type="full"))
        cursor += timedelta(days=1)
    for d in days:
        marks = []
        for name, model in (("stock_kline", StockKline), ("limit_up_pool", LimitUpPool)):
            count = await db.scalar(select(func.count()).select_from(model).where(model.trade_date == d))
            marks.append(dict(dataset=name, trade_date=d.isoformat(), status="ok",
                record_count=count, expected_count=count, completeness=1.0,
                details={"coverage_scope": "all_rows"}))
        at = datetime.combine(d, datetime.min.time()).replace(hour=16)
        db.add(DataQualityRun(trade_date=d, snapshot_context="promotion_2000", status="ok",
            gate_passed=True, started_at=at, completed_at=at,
            summary_json=json.dumps({"watermarks": marks})))
    await db.commit()


async def seed(db, *, run_id=1, at=None, codes=("600001", "600002"), status="completed", complete=True):
    at = at or datetime(2026, 9, 7, 20)
    run = PromotionPredictionRun(
        id=run_id, run_key=f"run{run_id}", snapshot_batch_key=f"batch{run_id}",
        reference_trade_date=DAY, as_of_at=at, created_at=at,
        completed_at=at + timedelta(seconds=1) if complete else None,
        snapshot_source="schedule", snapshot_context="promotion_2000",
        model_version="fixture_champion", feature_version="fixture_features",
        data_version="fixture_data", runtime_mode="legacy", status=status, gate_passed=True,
        candidate_count=len(codes), ranked_count=0, actionable_count=0,
        payload_hash=f"payload{run_id}", metadata_json="{}",
    )
    db.add(run)
    for i, code in enumerate(codes):
        db.add(PromotionPredictionSnapshot(
            run_id=run_id, record_key=f"{run_id}:{code}", code=code, name="fixture",
            target_board=1, prediction_trade_date=DAY, horizon_days=1,
            candidate_route="fixture", rank_scope="pool_unranked", calibrated_probability=0.1,
            raw_probability=0.1, trade_gate_passed=True, actionable=False, watch_only=False,
            features_json=json.dumps({"route_score": i, "market_regime": "recovery"}),
            reason_json="{}", created_at=at,
        ))
    await db.commit()
    await seal_fixture(db)
    return run


async def truth(db):
    for d in (DAY, OUTCOME):
        for code in ("600001", "600002"):
            db.add(StockKline(code=code, trade_date=d, close=10.0, prev_close=10.0, volume=10000, source="ths"))
    db.add(LimitUpPool(code="600003", trade_date=DAY, consecutive_days=1, quarantined=False, source="fixture"))
    db.add(LimitUpPool(code="600001", trade_date=OUTCOME, consecutive_days=1, quarantined=False, source="fixture"))
    await db.commit()
    await calendar_quality_fixture(db, [DAY, OUTCOME])


async def build(db, **kwargs):
    return await build_ledger_training_dataset(db, target_board=1,
                                              as_of_at=kwargs.pop("as_of_at", CUTOFF), **kwargs)


@pytest.mark.asyncio
async def test_compatibility_history_does_not_become_immutable_days(env):
    db, _ = env
    for i in range(26):
        d = DAY - timedelta(days=i)
        db.add(PromotionPredictionRecord(code="600001", target_board=1, prediction_trade_date=d,
               calibrated_probability=0.9, snapshot_source="schedule", snapshot_context="promotion_2000",
               factors_json="{}", candidate_route="legacy"))
    await db.commit()
    empty = await build(db)
    assert empty.rows == [] and empty.diagnostics["loaded_runs"] == 0
    await seed(db)
    await truth(db)
    result = await build(db)
    assert result.diagnostics["trade_day_count"] == 1
    assert len(result.rows) == 2
    assert sum(r.label for r in result.rows) == 1
    assert result.diagnostics["storage_source"] == "immutable_prediction_ledger"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["failed", "missing_clock", "future", "incomplete", "row_clock", "duplicate_code", "bad_factors", "hist_without_proof"])
async def test_bad_latest_batch_never_falls_back_or_stockwise_stitches(env, bad):
    db, _ = env
    await seed(db)
    await truth(db)
    at = datetime(2026, 9, 7, 20, 1)
    await seed(db, run_id=2, at=at, codes=("600001",))
    if bad == "failed":
        await db.execute(text("UPDATE promotion_prediction_run SET status='failed' WHERE id=2"))
    elif bad == "missing_clock":
        await db.execute(text("UPDATE promotion_prediction_run SET completed_at=NULL WHERE id=2"))
    elif bad == "future":
        await db.execute(text("UPDATE promotion_prediction_run SET completed_at='2026-09-09 20:00:00' WHERE id=2"))
    elif bad == "incomplete":
        await db.execute(text("UPDATE promotion_prediction_run SET candidate_count=2 WHERE id=2"))
    elif bad == "row_clock":
        await db.execute(text("UPDATE promotion_prediction_snapshot SET created_at='2026-09-09 00:00:00' WHERE run_id=2"))
    elif bad == "duplicate_code":
        row = (await db.scalars(select(PromotionPredictionSnapshot).where(PromotionPredictionSnapshot.run_id == 2))).one()
        db.add(PromotionPredictionSnapshot(run_id=2, record_key="dup", code="600001",
            target_board=1, prediction_trade_date=DAY, horizon_days=1,
            candidate_route="fixture", features_json="{}", created_at=at))
        await db.execute(text("UPDATE promotion_prediction_run SET candidate_count=2 WHERE id=2"))
    elif bad == "bad_factors":
        await db.execute(text("UPDATE promotion_prediction_snapshot SET features_json='[]' WHERE run_id=2"))
    elif bad == "hist_without_proof":
        await db.execute(text("UPDATE promotion_prediction_snapshot SET features_json=:payload WHERE run_id=2"),
                         {"payload": json.dumps({"hist_return_1d": 0})})
    await db.commit()
    db.expire_all()
    result = await build(db)
    assert not result.rows
    assert result.diagnostics["selected_run_ids"] == [2]
    assert len(result.diagnostics["excluded_runs"]) == 1


@pytest.mark.asyncio
async def test_newest_whole_batch_is_not_old_stock_union(env):
    db, _ = env
    await seed(db)
    await seed(db, run_id=2, at=datetime(2026, 9, 7, 20, 1), codes=("600001",))
    await truth(db)
    r = await build(db)
    assert [x.code for x in r.rows] == ["600001"]
    assert r.diagnostics["eligible_run_ids"] == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["intraday", "partial_market", "candidate_missing", "suspended", "quarantined", "missing_prediction_truth"])
async def test_unknown_outcomes_are_not_negative_samples(env, mutation):
    db, _ = env
    await seed(db)
    await truth(db)
    cutoff = CUTOFF
    if mutation == "intraday":
        cutoff = datetime(2026, 9, 8, 14)
    elif mutation in {"partial_market", "candidate_missing"}:
        if mutation == "candidate_missing":
            db.add(StockKline(code="600003", trade_date=OUTCOME, close=10, volume=1))
        await db.execute(text("DELETE FROM stock_kline WHERE trade_date='2026-09-08' AND code='600002'"))
    elif mutation == "suspended":
        await db.execute(text("UPDATE stock_kline SET volume=0 WHERE trade_date='2026-09-08' AND code='600002'"))
    elif mutation == "quarantined":
        db.add(LimitUpPool(code="600002", trade_date=OUTCOME, quarantined=True, source="fixture"))
    elif mutation == "missing_prediction_truth":
        await db.execute(text("DELETE FROM limit_up_pool WHERE trade_date=:day"), {"day": DAY.isoformat()})
    await db.commit()
    result = await build(db, as_of_at=cutoff)
    assert not result.rows
    assert result.diagnostics["excluded_runs"]


@pytest.mark.asyncio
async def test_readonly_full_batch_query_and_content_hash(env):
    db, path = env
    await seed(db)
    await truth(db)
    before = path.read_bytes()
    statements = []
    from sqlalchemy.engine import Engine
    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_uri()}?mode=ro&uri=true")
    event.listen(Engine, "before_cursor_execute", observe)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("PRAGMA query_only=ON"))
            await conn.execute(text("BEGIN"))
            async with AsyncSession(bind=conn, autoflush=False) as ro:
                first = await build(ro)
                second = await build(ro)
    finally:
        event.remove(Engine, "before_cursor_execute", observe)
        await engine.dispose()
    assert first.data_version == second.data_version
    assert path.read_bytes() == before
    assert any("FROM promotion_prediction_snapshot" in s for s in statements)
    assert all("LIMIT" not in s.upper() for s in statements if "FROM promotion_prediction_snapshot" in s)
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for s in statements)
    # Fixture-only corruption/revision demonstrates the content fingerprint is
    # not just IDs; production ledger never permits these mutations.
    await db.execute(text("UPDATE promotion_prediction_snapshot SET features_json=:payload WHERE code=:code"),
                     {"payload": json.dumps({"route_score": 999}), "code": "600001"})
    await db.commit()
    db.expire_all()
    changed = await build(db)
    assert changed.data_version != first.data_version


@pytest.mark.asyncio
async def test_default_training_rejects_legacy_only_data_before_model_fit(env):
    db, _ = env
    db.add(PromotionPredictionRecord(code="600001", target_board=1, prediction_trade_date=DAY,
           snapshot_source="schedule", snapshot_context="promotion_2000", factors_json="{}",
           candidate_route="legacy"))
    await db.commit()
    with pytest.raises(ValueError, match="training data guard"):
        await train_promotion_challenger(db, target_board=1, persist=False, as_of_at=CUTOFF)


@pytest.mark.asyncio
async def test_no_mixed_model_versions(env):
    db, _ = env
    await seed(db)
    await seed(db, run_id=2, at=datetime(2026, 9, 4, 20))
    await db.execute(text("UPDATE promotion_prediction_run SET reference_trade_date='2026-09-04',model_version='other' WHERE id=2"))
    await db.execute(text("UPDATE promotion_prediction_snapshot SET prediction_trade_date='2026-09-04' WHERE run_id=2"))
    await db.commit()
    db.expire_all()
    await seal_fixture(db)
    with pytest.raises(ValueError, match="mixed model"):
        await build(db)


@pytest.mark.asyncio
async def test_read_builder_never_autoflushes_pending_writes(env):
    db, _ = env
    db.add(PromotionPredictionRecord(code="600001", target_board=1, prediction_trade_date=DAY,
                                   candidate_route="pending", factors_json="{}"))
    with pytest.raises(ValueError, match="autoflush"):
        await build(db)
    assert db.new
    await db.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "missing_calendar", "missing_t1_all_bars", "spot_fallback", "unknown_source",
    "price_chain", "missing_audit", "presence_only_limit", "partial_limit",
    "future_prediction_audit", "current_tags_scope", "feature_hash",
    "payload_hash", "run_key", "producer_manifest", "rank_identity",
])
async def test_strict_calendar_truth_and_frozen_identity(env, mutation):
    db, _ = env
    await seed(db)
    await truth(db)
    if mutation == "missing_calendar":
        sql = "DELETE FROM trade_calendar WHERE trade_date='2026-09-08'"
    elif mutation == "missing_t1_all_bars":
        sql = "DELETE FROM stock_kline WHERE trade_date='2026-09-08'"
        # T+2 has available bars: must never be mislabeled as T+1.
        for code in ("600001", "600002"):
            db.add(StockKline(code=code, trade_date=date(2026, 9, 9), close=10,
                             prev_close=10, volume=100, source="ths"))
    elif mutation in {"spot_fallback", "unknown_source"}:
        sql = "UPDATE stock_kline SET source='" + mutation + "' WHERE trade_date='2026-09-08'"
    elif mutation == "price_chain":
        sql = "UPDATE stock_kline SET prev_close=99 WHERE trade_date='2026-09-08'"
    elif mutation == "missing_audit":
        sql = "DELETE FROM data_quality_run"
    elif mutation == "future_prediction_audit":
        sql = "UPDATE data_quality_run SET started_at='2026-09-07 21:00:00',completed_at='2026-09-07 21:00:00' WHERE trade_date='2026-09-07'"
    elif mutation in {"presence_only_limit", "partial_limit", "current_tags_scope"}:
        from app.models.governance import DataQualityRun
        audit = (await db.scalars(select(DataQualityRun).where(DataQualityRun.trade_date == OUTCOME))).one()
        payload = json.loads(audit.summary_json)
        if mutation == "presence_only_limit":
            payload["watermarks"][1]["expected_count"] = None
        elif mutation == "partial_limit":
            payload["watermarks"][1]["expected_count"] = 2
        else:
            payload["watermarks"][0]["details"]["coverage_scope"] = "tradeable_stock_tags"
        await db.execute(text("UPDATE data_quality_run SET summary_json=:p WHERE id=:id"),
                         {"p": json.dumps(payload), "id": audit.id})
        sql = "SELECT 1"
    elif mutation == "feature_hash":
        sql = "UPDATE promotion_prediction_snapshot SET features_json='{}'"
    elif mutation == "payload_hash":
        sql = "UPDATE promotion_prediction_run SET payload_hash='broken'"
    elif mutation == "run_key":
        sql = "UPDATE promotion_prediction_run SET run_key='broken'"
    elif mutation == "producer_manifest":
        sql = "UPDATE promotion_prediction_run SET feature_version='different'"
    else:
        sql = "UPDATE promotion_prediction_snapshot SET rank_position=1"
    await db.execute(text(sql))
    await db.commit()
    db.expire_all()
    result = await build(db, as_of_at=datetime(2026, 9, 9, 20))
    assert not result.rows
    assert result.diagnostics["excluded_runs"]


@pytest.mark.asyncio
async def test_policy_and_rejected_evidence_change_hash(env, monkeypatch):
    db, _ = env
    await seed(db)
    await truth(db)
    a = await build(db)
    # Producer feature version is deliberately different from modeling extractor.
    assert a.rows and a.diagnostics["extractor_feature_version"] != "fixture_features"
    monkeypatch.setattr(settings, "PROMOTION_SHADOW_MIN_KLINE_COMPLETENESS", 0.9)
    b = await build(db)
    assert b.rows and a.data_version != b.data_version
    await db.execute(text("UPDATE data_quality_run SET summary_json='{}'"))
    await db.commit()
    db.expire_all()
    c = await build(db)
    assert not c.rows
    await db.execute(text("UPDATE data_quality_run SET summary_json='{\"reason\": \"changed\"}'"))
    await db.commit()
    db.expire_all()
    d = await build(db)
    assert not d.rows and c.data_version != d.data_version


@pytest.mark.asyncio
async def test_other_lane_corruption_invalidates_whole_batch(env):
    db, _ = env
    await seed(db)
    await truth(db)
    await db.execute(text("UPDATE promotion_prediction_snapshot SET target_board=2 WHERE code='600002'"))
    await db.commit()
    db.expire_all()
    await seal_fixture(db)
    good = await build(db)
    assert len(good.rows) == 1
    await db.execute(text("UPDATE promotion_prediction_snapshot SET features_json='{}' WHERE target_board=2"))
    await db.commit()
    db.expire_all()
    bad = await build(db)
    assert not bad.rows
    assert bad.diagnostics["excluded_runs"][0]["reason"] == "snapshot_feature_hash_or_identity_mismatch"


@pytest.mark.asyncio
async def test_identity_missing_one_stock_blocks_whole_ledger_day(env):
    from test_promotion_identity_evidence import fixture_index
    db, _ = env
    await seed(db)
    await truth(db)
    missing = fixture_index(known_cutoff=CUTOFF, codes=("600001", "600002"), unknown_code="600002")
    bad = await build(db, identity_index=missing)
    assert not bad.rows
    assert bad.diagnostics["identity_gates"][0]["passed"] is False
    assert len(bad.diagnostics["identity_gates"][0]["per_code"]) == 2
    good = await build(db, identity_index=fixture_index(known_cutoff=CUTOFF, codes=("600001", "600002")))
    assert len(good.rows) == 2
    assert good.data_version != bad.data_version


@pytest.mark.asyncio
async def test_default_unknown_identity_never_uses_current_stock_tags(env, tmp_path):
    from app.promotion.identity_evidence import _prepare
    from app.models.stock import StockTag
    db, _ = env
    await seed(db)
    await truth(db)
    for code in ("600001", "600002"):
        db.add(StockTag(code=code, board_type="main_sh", board_tag="tradeable",
                        is_st=False, is_suspended=False, is_delisting=False))
    await db.commit()
    empty = _prepare(known_cutoff=CUTOFF, archive_root=tmp_path / "no_identity", policy=None)
    result = await build(db, identity_index=empty)
    assert not result.rows
    assert result.diagnostics["excluded_runs"][-1]["reason"] == "candidate_identity_unknown_or_outside_profile"
