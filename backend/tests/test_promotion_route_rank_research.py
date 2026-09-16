"""Isolated fixtures for research only: no imports of production API/trading."""
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from app.promotion.route_rank_research import (
    C_ROUTE, RouteQuotaContract, analyze_database, compare_frozen_run,
)

DAY = date(2026, 9, 8)
START = datetime(2026, 9, 8, 13, 2)
DONE = START + timedelta(minutes=1)
CUTOFF = datetime(2026, 9, 8, 13, 5)


def fixture_run():
    run = PromotionPredictionRun(
        id=50, run_key="run50", snapshot_batch_key="batch50",
        reference_trade_date=DAY, as_of_at=START, created_at=START, completed_at=DONE,
        snapshot_source="schedule", snapshot_context="promotion_1305",
        status="completed", model_version="frozen_v1", feature_version="feature_v1",
        data_version="data_v1", runtime_mode="legacy", gate_passed=True,
        candidate_count=8, ranked_count=2, actionable_count=0, payload_hash="fixture_hash",
        metadata_json=json.dumps({"quality_gate": {
            "route_gates": {C_ROUTE: {"gate_passed": True}},
            "watermarks": [{"dataset": "fund_flow", "trade_date": "2026-09-04",
                            "record_count": 100, "status": "ok"}],
        }}),
    )
    rows = []
    for i in range(1, 9):
        formal = i <= 2
        recall = i <= 4
        rows.append(PromotionPredictionSnapshot(
            id=i, run_id=50, record_key=f"row{i}", code=f"{600000+i}",
            name=f"fixture{i}", target_board=1, horizon_days=1,
            prediction_trade_date=DAY, created_at=START,
            candidate_route=C_ROUTE if i >= 6 else "news_catalyst_start",
            rank_scope="ranked" if formal else "recall_ranked" if recall else "pool_unranked",
            rank_position=i if formal else None, recall_rank_position=i if recall else None,
            pool_rank=i, calibrated_probability=0.9-i*0.05,
            raw_probability=0.9-i*0.05, trade_gate_passed=i != 7,
            watch_only=i == 8, actionable=False, reason_json="{}",
            features_json=json.dumps({
                "prediction_rank_contract_version": "promotion_rank_contract_v1",
                "prediction_rank_contract_complete": True, "prediction_rank_eligible": i != 8,
                "prediction_ranked_limit": 2, "prediction_recall_ranked_limit": 4,
            }),
        ))
    return run, rows


def compare(run=None, rows=None, **kwargs):
    if run is None:
        run, rows = fixture_run()
    return compare_frozen_run(run, rows, decision_at=kwargs.pop("decision_at", CUTOFF),
                              contract=kwargs.pop("contract", RouteQuotaContract(1, 2)), **kwargs)


def test_three_paired_arms_and_no_execution_promotion():
    r = compare()
    arms = r["arms"]
    observed, global_, quota = arms.values()
    assert observed["formal_ids"] == global_["formal_ids"] == [1, 2]
    assert observed["recall_including_formal_ids"] == [1, 2, 3, 4]
    assert quota["formal_ids"] == [6, 1]
    assert quota["recall_including_formal_ids"] == [6, 1, 7, 2]
    assert all(a["denominator"] == 7 for a in arms.values())
    assert all(a["formal_count"] == 2 and a["recall_count"] == 4 for a in arms.values())
    assert r["route_funnel"]["pool"] == 3
    assert r["route_funnel"]["rank_eligible"] == 2
    assert r["route_funnel"]["eligible_unranked"] == 2
    assert r["execution"]["status"] == "blocked"
    assert r["execution"]["funding_gate"] == "blocked_missing_or_stale"
    assert r["execution"]["frozen_route_gate_passed"] is True
    assert r["execution"]["buy_allowed"] is False
    assert r["route_order"][1]["trade_gate_passed"] is False
    assert all(a["cost_after_return"] is None and a["precision"] is None for a in arms.values())


def test_deterministic_ties_and_no_mutation_or_outcome_selection():
    run, rows = fixture_run()
    rows[5].calibrated_probability = rows[6].calibrated_probability
    original = [(s.rank_scope, s.actionable, s.features_json) for s in rows]
    r = compare(run, rows)
    reverse = compare(run, list(reversed(rows)))
    assert r == reverse
    assert [x["id"] for x in r["route_order"]] == [6, 7]
    for row in rows:
        f = json.loads(row.features_json)
        f["actual_limit_up"] = row.id % 2 == 0
        f["future_return"] = 999
        row.features_json = json.dumps(f)
    assert compare(run, rows)["arms"] == r["arms"]
    assert [(s.rank_scope, s.actionable) for s in rows] == [(a, b) for a, b, _ in original]


@pytest.mark.parametrize("field,value", [
    ("completed_at", None), ("created_at", None), ("as_of_at", None),
    ("completed_at", CUTOFF + timedelta(seconds=1)),
    ("created_at", DONE + timedelta(seconds=1)),
    ("as_of_at", START.replace(tzinfo=timezone.utc)),
    ("status", "failed"), ("snapshot_source", "page"),
    ("reference_trade_date", date(2026, 9, 7)),
])
def test_invalid_run_rejected(field, value):
    run, rows = fixture_run()
    setattr(run, field, value)
    with pytest.raises(ValueError):
        compare(run, rows)


def test_quote_cutoff_and_equality():
    assert compare(quote_as_of=DONE)["evidence"]["visible_cutoff"] == DONE.isoformat()
    with pytest.raises(ValueError, match="future"):
        compare(quote_as_of=DONE - timedelta(microseconds=1))
    with pytest.raises(ValueError, match="quote"):
        compare(quote_as_of=DONE - timedelta(days=1))
    with pytest.raises(ValueError, match="decision"):
        compare(decision_at=CUTOFF.replace(tzinfo=timezone.utc))


@pytest.mark.parametrize("mutation", [
    "truncated", "snapshot_clock", "duplicate", "duplicate_code", "mixed_run",
    "bad_json", "missing_eligible", "mixed_capacity", "nan", "missing_probability",
    "bad_position", "ineligible_ranked", "mixed_date", "duplicate_id",
])
def test_bad_denominators_and_contracts_fail_closed(mutation):
    run, rows = fixture_run()
    if mutation == "truncated":
        rows.pop()
    elif mutation == "snapshot_clock":
        rows[-1].created_at = None
    elif mutation == "duplicate":
        rows[-1].record_key = rows[0].record_key
    elif mutation == "duplicate_code":
        rows[-1].code = rows[0].code
    elif mutation == "duplicate_id":
        rows[-1].id = rows[0].id
    elif mutation == "mixed_run":
        rows[-1].run_id = 99
    elif mutation == "bad_json":
        rows[-1].features_json = "[]"
    elif mutation == "missing_eligible":
        rows[-1].features_json = "{}"
    elif mutation == "mixed_capacity":
        f = json.loads(rows[-1].features_json)
        f["prediction_ranked_limit"] = 3
        rows[-1].features_json = json.dumps(f)
    elif mutation == "nan":
        rows[5].calibrated_probability = float("nan")
    elif mutation == "missing_probability":
        rows[5].calibrated_probability = None
    elif mutation == "bad_position":
        rows[0].rank_position = 3
    elif mutation == "ineligible_ranked":
        f = json.loads(rows[0].features_json)
        f["prediction_rank_eligible"] = False
        rows[0].features_json = json.dumps(f)
    elif mutation == "mixed_date":
        rows[-1].prediction_trade_date = date(2026, 9, 7)
    with pytest.raises(ValueError):
        compare(run, rows)


def test_empty_zero_quota_and_exhausted_route():
    run, rows = fixture_run()
    run.candidate_count = 0
    r = compare(run, [], contract=RouteQuotaContract(0, 0))
    assert r["evidence"]["eligible_rows"] == 0
    assert all(not arm["formal_ids"] for arm in r["arms"].values())
    run, rows = fixture_run()
    for row in rows:
        row.candidate_route = "news_catalyst_start"
    result = compare(run, rows)
    assert result["arms"]["routequota_probability_hypothesis"] == result["arms"]["global_probability_control"]


@pytest.mark.parametrize("formal,recall", [(-1, 2), (2, 1), (True, 2), (1.5, 2), (3, 4), (1, 5)])
def test_invalid_quota(formal, recall):
    with pytest.raises(ValueError):
        compare(contract=RouteQuotaContract(formal, recall))


def test_quota_changes_reproducible_contract_and_zero_quota_control():
    baseline = compare()
    zero = compare(contract=RouteQuotaContract(0, 0))
    assert zero["contract_hash"] != baseline["contract_hash"]
    assert zero["comparison_hash"] != baseline["comparison_hash"]
    assert zero["arms"]["global_probability_control"] == zero["arms"]["routequota_probability_hypothesis"]


@pytest.mark.asyncio
async def test_readonly_cli_and_complete_batch_no_sql_limit(tmp_path):
    path = tmp_path / "frozen.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    run, rows = fixture_run()
    # Lower-ranked route candidates occur AFTER the formal and recall top slots.
    async with engine.begin() as conn:
        await conn.run_sync(PromotionPredictionRun.__table__.create)
        await conn.run_sync(PromotionPredictionSnapshot.__table__.create)
    async with AsyncSession(engine) as db:
        db.add(run)
        db.add_all(rows)
        await db.commit()
    await engine.dispose()
    before = path.read_bytes()
    from sqlalchemy.engine import Engine
    statements = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(Engine, "before_cursor_execute", observe)
    try:
        r = await analyze_database(path, run_id=50, decision_at=CUTOFF,
                                   contract=RouteQuotaContract(1, 2))
    finally:
        event.remove(Engine, "before_cursor_execute", observe)
    assert r["evidence"]["full_run_rows"] == 8
    snapshot_queries = [s for s in statements if "FROM promotion_prediction_snapshot" in s]
    assert snapshot_queries and all("LIMIT" not in s.upper() for s in snapshot_queries)
    assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for s in statements)
    assert path.read_bytes() == before
    ro_engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_uri()}?mode=ro&uri=true")
    try:
        async with ro_engine.connect() as conn:
            with pytest.raises(OperationalError, match="readonly"):
                await conn.execute(text("DELETE FROM promotion_prediction_snapshot"))
    finally:
        await ro_engine.dispose()
    cli = Path(__file__).resolve().parents[1] / "scripts/analyze_route_rank_research.py"
    args = [sys.executable, str(cli), "--database", str(path), "--run-id", "50",
            "--decision-at", CUTOFF.isoformat(), "--formal-quota", "1", "--recall-quota", "2"]
    p = subprocess.run(args, capture_output=True, text=True, check=False)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["comparison_hash"] == r["comparison_hash"]
    p = subprocess.run(args + ["--quote-as-of", START.isoformat()], capture_output=True, text=True, check=False)
    assert p.returncode == 2 and json.loads(p.stdout)["status"] == "rejected"
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="not found"):
        await analyze_database(path, run_id=99, decision_at=CUTOFF, contract=RouteQuotaContract(1, 2))
