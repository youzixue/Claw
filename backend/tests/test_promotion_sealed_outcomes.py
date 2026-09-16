"""Sealed material consumer tests. Fake gates are explicit test dependencies only."""
import asyncio
from copy import deepcopy
from datetime import datetime, date
import threading
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import func, select

from app.promotion import shadow, outcome_materials
from app.models.promotion import PromotionShadowEvaluation
from test_promotion_shadow import shadow_env
from test_promotion_shadow_identity import seed, evaluate, add_second_day
from test_promotion_paired_material import outcome_ready_fixture
from test_promotion_ledger_dataset import env as ledger_env, seed as ledger_seed, truth as ledger_truth, build as ledger_build

REAL_PREPARE = outcome_materials.prepare_outcome_index
REAL_GATE = outcome_materials.outcome_pair_gate


@pytest.mark.asyncio
async def test_default_source_policy_cannot_turn_operational_tables_into_labels(shadow_env, monkeypatch):
    maker, root = shadow_env
    async def prepare(*, known_cutoff):
        return await REAL_PREPARE(known_cutoff=known_cutoff,archive_root=root/"materials")
    monkeypatch.setattr(shadow,"prepare_outcome_index",prepare)
    monkeypatch.setattr(shadow,"outcome_pair_gate",REAL_GATE)
    async with maker() as db:
        _, artifact_id = await seed(db,root)
        ensure = AsyncMock(side_effect=AssertionError("readonly must not migrate"))
        monkeypatch.setattr(shadow,"ensure_prediction_ledger_storage",ensure)
        result = await evaluate(db,artifact_id)
        assert await db.scalar(select(func.count()).select_from(PromotionShadowEvaluation)) == 0
    assert result["decision"] == "collecting" and "sample_count" not in result
    assert result["outcome_material_coverage"] == 0
    failure = result["excluded_runs"][0]
    assert "sealed_outcome_material_unverified" in failure["details"]
    assert not failure["outcome_material_evidence"]["consumer_passed"]
    assert not ensure.called and not (root/"materials").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["unknown","missing_code","wrong_code","event_conflict","price_conflict","bad_hash","bool_price","error"])
async def test_whole_shadow_batch_blocked_without_mutable_fallback(shadow_env,monkeypatch,fault):
    maker, root = shadow_env
    def gate(index,**kwargs):
        if fault == "error": raise OSError("fixture worker failure")
        result = outcome_ready_fixture(index,**kwargs)
        if fault == "unknown": result["passed"] = False
        elif fault == "missing_code": result["per_code"].pop()
        elif fault == "wrong_code": result["per_code"][0]["code"] = "600999"
        elif fault == "event_conflict": result["per_code"][0]["outcome_limit_up"] = not result["per_code"][0]["outcome_limit_up"]
        elif fault == "price_conflict": result["per_code"][0]["after_close"] += .01
        elif fault == "bad_hash": result["evidence_hash"] = ""
        elif fault == "bool_price": result["per_code"][0]["before_close"] = True
        return result
    monkeypatch.setattr(shadow,"outcome_pair_gate",gate)
    async with maker() as db:
        _, artifact_id = await seed(db,root)
        result = await evaluate(db,artifact_id)
    assert result["decision"] == "collecting" and "sample_count" not in result
    assert result["outcome_material_coverage"] == 0
    assert "sealed_outcome_material_unverified" in result["excluded_runs"][0]["details"]


@pytest.mark.asyncio
async def test_failed_material_prepare_never_reaches_permissive_gate(shadow_env,monkeypatch):
    maker,root = shadow_env
    monkeypatch.setattr(shadow,"prepare_outcome_index",AsyncMock(side_effect=OSError("fixture lock error")))
    gate = AsyncMock()
    monkeypatch.setattr(shadow,"outcome_pair_gate",gate)
    async with maker() as db:
        _, artifact_id = await seed(db,root)
        result = await evaluate(db,artifact_id)
    assert result["decision"] == "collecting" and not gate.called


@pytest.mark.asyncio
async def test_worker_gets_only_owned_values_and_does_not_block_event_loop(shadow_env,monkeypatch):
    maker,root = shadow_env
    release = threading.Event()
    calls = []
    main = threading.get_ident()
    def gate(index,**kwargs):
        assert threading.get_ident() != main
        assert type(index) is dict and set(index) == {"bars","pools"}  # explicit test index
        assert all(type(v) is dict for v in index["bars"].values())
        assert type(kwargs["codes"]) is tuple
        calls.append(kwargs)
        assert release.wait(2), "event loop failed to signal a running worker"
        return outcome_ready_fixture(index,**kwargs)
    monkeypatch.setattr(shadow,"outcome_pair_gate",gate)
    async def tick():
        await asyncio.sleep(.03)
        release.set()
    async with maker() as db:
        _, artifact_id = await seed(db,root)
        result,_ = await asyncio.gather(evaluate(db,artifact_id),tick())
    assert result["sample_count"] == 4
    assert calls[0]["prediction_at"] == datetime(2026,8,27,20)
    assert result["metadata"]["outcome_material_coverage"] == 1


@pytest.mark.asyncio
async def test_rejected_day_stays_in_material_denominator_and_revision_key(shadow_env,monkeypatch):
    maker,root = shadow_env
    revision = ["c"*64]
    def gate(index,**kwargs):
        result = outcome_ready_fixture(index,**kwargs)
        result["query_clocks"] = {"evaluation":str(kwargs["evaluation_as_of"])}
        if kwargs["prediction_day"] == date(2026,8,31):
            result.update(passed=False,reasons=["missing_finality"],evidence_hash=revision[0])
        return result
    monkeypatch.setattr(shadow,"outcome_pair_gate",gate)
    async with maker() as db:
        run_id,artifact_id = await seed(db,root)
        other = await add_second_day(db,run_id,artifact_id,monkeypatch)
        first = await evaluate(db,artifact_id)
        again = await shadow.evaluate_shadow_artifact(db,artifact_id=artifact_id,target_board=1,
            snapshot_context="promotion_2000",persist=False,minimum_kline_rows=1,now=datetime(2026,9,8,18))
        assert first["evaluation_key"] == again["evaluation_key"]
        revision[0] = "d"*64
        revised = await evaluate(db,artifact_id)
    assert first["sample_count"] == 4 and first["trade_day_count"] == 1
    assert first["metadata"]["outcome_material_coverage"] == .5
    assert first["metadata"]["outcome_material_unverified_run_ids"] == [other["id"]]
    check = next(c for c in first["acceptance"]["checks"] if c["name"]=="outcome_material_coverage")
    assert check["actual"] == .5 and not check["passed"]
    assert first["decision"] == "collecting"
    assert first["evaluation_key"] != revised["evaluation_key"]
    assert first["metrics"] == revised["metrics"]


@pytest.mark.asyncio
async def test_ledger_default_archive_missing_excludes_whole_day(ledger_env,monkeypatch):
    db,path = ledger_env
    await ledger_seed(db)
    await ledger_truth(db)
    async def prepare(*,known_cutoff):
        return await REAL_PREPARE(known_cutoff=known_cutoff,archive_root=path.parent/"materials")
    monkeypatch.setattr(outcome_materials,"prepare_outcome_index",prepare)
    monkeypatch.setattr(outcome_materials,"outcome_pair_gate",REAL_GATE)
    result = await ledger_build(db)
    assert not result.rows
    assert result.diagnostics["excluded_runs"][-1]["reason"] == "sealed_outcome_material_unverified"
    assert not (path.parent/"materials").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["event","price","missing","unknown","prepare_error"])
async def test_ledger_missing_or_conflicting_sealed_evidence_never_uses_legacy_fallback(ledger_env,monkeypatch,change):
    db,path = ledger_env
    await ledger_seed(db)
    await ledger_truth(db)
    good = await ledger_build(db)
    def gate(index,**kwargs):
        result = outcome_ready_fixture(index,**kwargs)
        if change == "event": result["per_code"][0]["outcome_limit_up"] = False
        elif change == "price": result["per_code"][0]["after_prev_close"] = 9.99
        elif change == "missing": result["per_code"].pop()
        elif change == "unknown": result["passed"] = False
        return result
    monkeypatch.setattr(outcome_materials,"outcome_pair_gate",gate)
    if change == "prepare_error":
        monkeypatch.setattr(outcome_materials,"prepare_outcome_index",AsyncMock(side_effect=OSError("fixture")))
    result = await ledger_build(db)
    assert good.rows and not result.rows
    assert good.data_version != result.data_version
    assert result.diagnostics["excluded_runs"][-1]["reason"] == "sealed_outcome_material_unverified"
