"""Real isolated SQL capture/replay; no production DB, scheduler, training or orders."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import json

import pytest
from sqlalchemy import select, func, text
from sqlalchemy.exc import SQLAlchemyError, IntegrityError

from app.factors import FactorEngine
from app.factors.computation_evidence import (
    append_computation, read_computation, replay_computation, engine_descriptor,
    validate_payload, digest, PROTOCOL,
)
from app.models.factor import FactorComputationRun, FactorValue
from app.models.stock import StockKline, FundFlow
from app.risk.factor_scheduler import FactorEvaluationScheduler
from test_factor_market_inputs_20260914 import db, seed, row, CODE, DAY, NOW


async def capture(db, codes=None):
    report = await FactorEvaluationScheduler().compute_and_store_factors(db, DAY, codes or [CODE])
    evidence = report["computation_capture"]
    assert evidence["status"] == "committed"
    payload = await read_computation(db, capture_id=evidence["capture_id"])
    return report, payload


def arguments(payload):
    return {
        **{key: deepcopy(payload[key]) for key in (
            "capture_id", "descriptor", "stocks", "requested_codes", "attempted_codes", "deferred_codes", "failures")},
        "trade_date": date.fromisoformat(payload["trade_date"]),
        "read_started_at": datetime.fromisoformat(payload["read_started_at"]),
        "captured_at": datetime.fromisoformat(payload["captured_at"]),
    }


@pytest.mark.asyncio
async def test_capture_replays_only_frozen_inputs_after_mutable_sources_change(db):
    await seed(db)
    report, payload = await capture(db)
    assert report["saved_values"] == 48
    assert payload["protocol"] == PROTOCOL
    assert payload["read_started_at"] == payload["captured_at"] == NOW.isoformat()
    assert payload["stocks"][CODE]["context"] == {}
    assert len(payload["stocks"][CODE]["results"]) == 48
    assert payload["stocks"][CODE]["results"]["big_order_pct"]["value"] == 0
    assert payload["stocks"][CODE]["results"]["sector_fund_flow"]["value"] is None
    assert payload["stocks"][CODE]["market_evidence"] == report["input_evidence"][CODE]
    before = json.dumps(payload, sort_keys=True, allow_nan=False)
    (await row(db, FundFlow)).main_net_inflow = 9000
    (await row(db, StockKline)).close = 999
    await db.commit()
    replay = await replay_computation(payload, FactorEngine())
    assert replay["matched"] is True and replay["evaluated_stock_count"] == 1
    assert replay["point_in_time_verified"] is False
    assert json.dumps(await read_computation(db, capture_id=payload["capture_id"]),
                      sort_keys=True, allow_nan=False) == before
    assert await read_computation(db, capture_id="missing") is None


@pytest.mark.asyncio
async def test_a_b_a_is_three_captures_and_legacy_value_is_never_rewritten(db):
    await seed(db)
    captures = []
    for amount in (1000, 2000, 1000):
        (await row(db, FundFlow)).main_net_inflow = amount
        await db.commit()
        report, payload = await capture(db)
        captures.append(payload)
    assert len({p["capture_id"] for p in captures}) == 3
    assert [p["stocks"][CODE]["results"]["main_inflow_strength"]["value"] for p in captures] == [1, 2, 1]
    assert captures[0]["stocks"] == captures[2]["stocks"]
    assert await db.scalar(select(func.count()).select_from(FactorComputationRun)) == 3
    value = await db.scalar(select(FactorValue.factor_value).where(FactorValue.factor_name == "main_inflow_strength"))
    assert value == 1 and report["saved_values"] == 0


@pytest.mark.asyncio
async def test_exact_retry_is_idempotent_but_same_capture_collision_fails(db):
    await seed(db)
    _, payload = await capture(db)
    result = await append_computation(db, **arguments(payload))
    assert result["inserted"] is False and result["status"] == "flushed_not_committed"
    args = arguments(payload)
    args["captured_at"] += timedelta(microseconds=1)
    with pytest.raises(ValueError, match="collision"):
        await append_computation(db, **args)
    assert await db.scalar(select(func.count()).select_from(FactorComputationRun)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["orm_update", "orm_delete", "sql_update", "sql_delete", "replace_id", "replace_capture"])
async def test_orm_bulk_and_replace_cannot_change_evidence(db, action):
    await seed(db)
    _, payload = await capture(db)
    record = await db.scalar(select(FactorComputationRun))
    expected = ValueError if action.startswith("orm") else IntegrityError
    with pytest.raises(expected, match="append-only"):
        if action == "orm_update":
            record.payload_json = "{}"
            await db.flush()
        elif action == "orm_delete":
            await db.delete(record)
            await db.flush()
        elif action == "sql_update":
            await db.execute(text("UPDATE factor_computation_run SET payload_json='{}'"))
        elif action == "sql_delete":
            await db.execute(text("DELETE FROM factor_computation_run"))
        else:
            column = "id" if action == "replace_id" else "capture_id"
            replacement = "id, 'different'" if column == "id" else "id+100, capture_id"
            await db.execute(text(
                "INSERT OR REPLACE INTO factor_computation_run SELECT " + replacement +
                ",trade_date,read_started_at,captured_at,protocol_version,payload_hash,payload_json FROM factor_computation_run"))
    await db.rollback()
    assert await read_computation(db, capture_id=payload["capture_id"]) == payload


@pytest.mark.asyncio
async def test_missing_migration_cannot_write_untraced_legacy_values(db):
    await seed(db)
    await db.execute(text("DROP TABLE factor_computation_run"))
    await db.commit()
    with pytest.raises(SQLAlchemyError):
        await FactorEvaluationScheduler().compute_and_store_factors(db, DAY, [CODE])
    assert await db.scalar(select(func.count()).select_from(FactorValue)) == 0


@pytest.mark.asyncio
async def test_legacy_insert_error_rolls_back_capture_and_legacy_atomically(db):
    await seed(db)
    await db.execute(text("CREATE TRIGGER block_legacy BEFORE INSERT ON factor_values "
                          "BEGIN SELECT RAISE(ABORT, 'isolated fault'); END"))
    await db.commit()
    with pytest.raises(IntegrityError, match="isolated fault"):
        await FactorEvaluationScheduler().compute_and_store_factors(db, DAY, [CODE])
    assert await db.scalar(select(func.count()).select_from(FactorComputationRun)) == 0
    assert await db.scalar(select(func.count()).select_from(FactorValue)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("after_commit", [False, True])
async def test_commit_failure_never_returns_durable_success_even_when_ack_unknown(db, monkeypatch, after_commit):
    await seed(db)
    original = db.commit
    async def fail():
        if after_commit:
            await original()
        raise RuntimeError("isolated commit acknowledgement failure")
    monkeypatch.setattr(db, "commit", fail)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        await FactorEvaluationScheduler().compute_and_store_factors(db, DAY, [CODE])
    # A failed acknowledgement AFTER a real commit cannot be undone or called a rollback.
    assert await db.scalar(select(func.count()).select_from(FactorComputationRun)) == int(after_commit)
    assert await db.scalar(select(func.count()).select_from(FactorValue)) == 48 * int(after_commit)


@pytest.mark.asyncio
async def test_stock_objects_are_frozen_before_append_database_await(db, monkeypatch):
    await seed(db)
    _, payload = await capture(db)
    args = arguments(payload)
    args["capture_id"] = "second"
    original = db.execute
    async def mutate(*a, **kw):
        args["stocks"][CODE]["results"]["main_inflow_strength"]["value"] = 999
        return await original(*a, **kw)
    monkeypatch.setattr(db, "execute", mutate)
    await append_computation(db, **args)
    await db.commit()
    after = await read_computation(db, capture_id="second")
    assert after["stocks"][CODE]["results"]["main_inflow_strength"]["value"] == 1


@pytest.mark.asyncio
async def test_helper_flush_has_no_implicit_commit(db):
    await seed(db)
    _, payload = await capture(db)
    args = arguments(payload)
    args["capture_id"] = "not-committed"
    await append_computation(db, **args)
    await db.rollback()
    assert await read_computation(db, capture_id="not-committed") is None


def corrupt(payload, case):
    p = deepcopy(payload)
    stock = p["stocks"][CODE]
    if case == "pit":
        p["point_in_time_verified"] = True
    elif case == "trade":
        p["trading_authority"] = True
    elif case == "physical":
        p["physical_commit_at"] = NOW.isoformat()
    elif case == "clock":
        p["captured_at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif case == "before_close":
        p["read_started_at"] = NOW.replace(hour=14).isoformat()
    elif case == "denominator":
        p["requested_codes"] += ["600002"]
    elif case == "overlap":
        p["failures"][CODE] = "ValueError"
    elif case == "missing_result":
        stock["results"].pop("ma5_bias")
    elif case == "frame_hash":
        stock["frame"][-1]["close"] = 999
    elif case == "clock_identity":
        stock["market_evidence"]["as_of_at"] = "2000-01-01T00:00:00"
    elif case == "code":
        stock["market_evidence"]["code"] = "600002"
    elif case == "context":
        stock["context"]["sector_strength_score"] = 90
    elif case == "bool_value":
        stock["results"]["ma5_bias"]["value"] = True
    elif case == "infinite":
        stock["results"]["ma5_bias"]["value"] = float("inf")
    elif case == "rank":
        stock["results"]["ma5_bias"]["rank"] = 1
    elif case == "null_confidence":
        stock["results"]["ma5_bias"]["value"] = None
    elif case == "descriptor":
        p["implementation_hash"] = "0" * 64
    elif case == "relabel_date":
        p["trade_date"] = "2026-09-11"
    return p


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "pit", "trade", "physical", "clock", "before_close", "denominator", "overlap",
    "missing_result", "frame_hash", "clock_identity", "code", "context", "bool_value",
    "infinite", "rank", "null_confidence", "descriptor", "relabel_date",
])
async def test_bad_authority_identity_denominator_and_input_cannot_replay(db, case):
    await seed(db)
    _, payload = await capture(db)
    bad = corrupt(payload, case)
    with pytest.raises(ValueError):
        validate_payload(bad)
    with pytest.raises(ValueError):
        await replay_computation(bad, FactorEngine())


@pytest.mark.asyncio
async def test_new_implementation_never_silently_replays_old_factors(db, monkeypatch):
    await seed(db)
    _, payload = await capture(db)
    original = FactorEngine.compute_single
    async def changed(self, *args, **kwargs):
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(FactorEngine, "compute_single", changed)
    with pytest.raises(ValueError, match="implementation/protocol"):
        await replay_computation(payload, FactorEngine())


@pytest.mark.asyncio
async def test_loading_bypassed_guard_corruption_still_checks_payload_hash(db):
    await seed(db)
    report, _ = await capture(db)
    await db.execute(text("DROP TRIGGER factor_computation_run_no_update"))
    await db.execute(text("UPDATE factor_computation_run SET payload_json='{}'"))
    await db.commit()
    with pytest.raises(ValueError, match="payload corrupt"):
        await read_computation(db, capture_id=report["computation_capture"]["capture_id"])


@pytest.mark.asyncio
async def test_empty_diagnostic_capture_never_claims_successful_replay_sample(db):
    await seed(db)
    report = await FactorEvaluationScheduler().compute_and_store_factors(db, DAY, [])
    payload = await read_computation(db, capture_id=report["computation_capture"]["capture_id"])
    assert payload["requested_codes"] == payload["attempted_codes"] == payload["deferred_codes"] == []
    result = await replay_computation(payload, FactorEngine())
    assert result["matched"] is None and result["evaluated_stock_count"] == 0


@pytest.mark.asyncio
async def test_input_mutation_during_calculation_is_failed_attempt_not_a_valid_capture(db, monkeypatch):
    await seed(db)
    original = FactorEngine.compute_single
    async def mutate(self, code, day, frame):
        result = await original(self, code, day, frame)
        frame.loc[frame.index[-1], "close"] = 99
        return result
    monkeypatch.setattr(FactorEngine, "compute_single", mutate)
    report = await FactorEvaluationScheduler().compute_and_store_factors(db, DAY, [CODE])
    payload = await read_computation(db, capture_id=report["computation_capture"]["capture_id"])
    assert report["computed_stocks"] == report["saved_values"] == 0
    assert payload["stocks"] == {} and payload["failures"] == {CODE: "ValueError"}
    assert payload["attempted_codes"] == [CODE]


@pytest.mark.asyncio
async def test_replay_owns_payload_before_first_calculator_await(db, monkeypatch):
    await seed(db)
    original = FactorEngine.compute_single
    target = {}
    async def mutate_caller_payload(self, *args, **kwargs):
        if target:
            target["payload"]["stocks"][CODE]["results"]["main_inflow_strength"]["value"] = 999
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(FactorEngine, "compute_single", mutate_caller_payload)
    _, payload = await capture(db)
    target["payload"] = payload
    result = await replay_computation(payload, FactorEngine())
    assert result["matched"] is True
    assert payload["stocks"][CODE]["results"]["main_inflow_strength"]["value"] == 999


@pytest.mark.asyncio
async def test_loading_coordinated_json_hash_change_still_checks_scalar_columns(db):
    from app.factors.computation_evidence import freeze
    await seed(db)
    report, payload = await capture(db)
    payload["capture_id"] = "different"
    await db.execute(text("DROP TRIGGER factor_computation_run_no_update"))
    await db.execute(text("UPDATE factor_computation_run SET payload_json=:body,payload_hash=:sha"),
                     {"body": freeze(payload), "sha": digest(payload)})
    await db.commit()
    with pytest.raises(ValueError, match="record contract mismatch"):
        await read_computation(db, capture_id=report["computation_capture"]["capture_id"])


@pytest.mark.asyncio
async def test_changed_frozen_result_is_not_a_matching_recomputation(db):
    await seed(db)
    _, payload = await capture(db)
    payload["stocks"][CODE]["results"]["main_inflow_strength"]["value"] = 999
    replay = await replay_computation(payload, FactorEngine())
    assert replay["matched"] is False and replay["stocks"] == {CODE: False}
