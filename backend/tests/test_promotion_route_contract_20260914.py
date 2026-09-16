"""Isolated route registry / frozen quality proof boundaries, never orders."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1 import promotion, paper
from app.core.prediction_data_quality import PredictionDataQualityAuditor, QualityFinding
from app.data.scheduler import PROMOTION_EXECUTION_ROUTES, _promotion_quality_gate_blocks_all_routes
from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from app.models.signal import PromotionPredictionRecord
from app.models.stock import StockSpot
from app.promotion.ledger import ScheduleBatch, append_prediction_run
from app.promotion.route_contract import (
    KNOWN_CANDIDATE_ROUTES, GENERATED_CANDIDATE_ROUTES, LEGACY_ROUTE_NAMES,
    REQUIRED_DATASETS_BY_ROUTE, EXECUTION_ROUTES, ROUTE_CONTRACT_VERSION,
    route_contract_payload, route_identity, persisted_route_gate,
)
from app.promotion.versioning import get_promotion_model_identity
from test_promotion_batch_diagnostics import attempt, aggregate, report, window, DAY
from test_promotion_ledger import ledger_session
from test_promotion_batch_attempts import persist, candidate, AT
from test_paper_api import paper_client, _governed_promotion_run, _governed_promotion_snapshot


def watermarks():
    return [dict(dataset=name, status="ok", completeness=1)
            for name in ("stock_kline", "fund_flow", "limit_up_pool", "auction_data")]


def quality():
    return dict(gate_passed=True, route_contract=route_contract_payload(),
                route_gates=PredictionDataQualityAuditor._build_route_gates([], watermarks()))


def test_registry_covers_labels_but_does_not_expand_execution_or_reclassify_legacy():
    assert KNOWN_CANDIDATE_ROUTES == set(promotion.FIRST_BOARD_ROUTE_LABELS)
    assert len(GENERATED_CANDIDATE_ROUTES) == 13 and len(LEGACY_ROUTE_NAMES) == 2
    assert EXECUTION_ROUTES == PROMOTION_EXECUTION_ROUTES == (
        "second_board_promotion", "mainline_spread_start", "auction_surge_start")
    assert set(REQUIRED_DATASETS_BY_ROUTE) == KNOWN_CANDIDATE_ROUTES
    assert all("promotion_prediction_record" not in x for x in REQUIRED_DATASETS_BY_ROUTE.values())
    for name in LEGACY_ROUTE_NAMES:
        assert route_identity(name) == "known_legacy"
    payload = route_contract_payload()
    payload["execution_routes"].append("news_catalyst_start")
    assert "news_catalyst_start" not in route_contract_payload()["execution_routes"]


@pytest.mark.parametrize("route", sorted(KNOWN_CANDIDATE_ROUTES))
@pytest.mark.parametrize("dataset", ["stock_kline", "fund_flow", "limit_up_pool", "auction_data"])
@pytest.mark.parametrize("status", ["missing", "degraded", "blocked", None])
def test_every_declared_route_checks_real_dependency_status(route, dataset, status):
    rows = watermarks()
    next(row for row in rows if row["dataset"] == dataset)["status"] = status
    gates = PredictionDataQualityAuditor._build_route_gates([], rows)
    assert gates[route]["gate_passed"] is (dataset not in REQUIRED_DATASETS_BY_ROUTE[route])
    assert gates[route]["required_datasets"] == sorted(REQUIRED_DATASETS_BY_ROUTE[route])


@pytest.mark.parametrize("value", [None, True, False, "", "bad", float("nan"),
                                       float("inf"), -float("inf"), -1, 1.01, {}, [], 10**1000])
def test_nonfinite_or_invalid_watermark_cannot_pass_with_ok_label(value):
    rows = watermarks()
    rows[0]["completeness"] = value
    assert not any(g["gate_passed"] for g in PredictionDataQualityAuditor._build_route_gates([], rows).values())


def test_auction_failure_is_route_local_and_foreign_blocker_stays_global():
    finding = QualityFinding("blocking", "auction_data", "bad_auction", "bad")
    gates = PredictionDataQualityAuditor._build_route_gates([finding], watermarks())
    assert all(g["gate_passed"] for r, g in gates.items() if r != "auction_surge_start")
    global_finding = QualityFinding("blocking", "promotion_prediction_record", "wrong_outcome", "bad")
    assert not any(g["gate_passed"] for g in
                   PredictionDataQualityAuditor._build_route_gates([global_finding], watermarks()).values())


@pytest.mark.parametrize("bad", [None, "", "   ", 1, [], {}, "mainline_spread", "strict_news_catalyst",
                                      "news_catalyst_start ", "support_squeeze_watch", "invented"])
def test_route_identity_does_not_accept_other_namespaces_or_foreign_gate(bad):
    gate = persisted_route_gate(quality(), bad)
    assert gate["gate_status"] == "unknown" and gate["gate_passed"] is None


@pytest.mark.parametrize("route", sorted(KNOWN_CANDIDATE_ROUTES))
def test_current_contract_is_frozen_proof_not_candidate_authorization(route):
    q = quality()
    before = deepcopy(q)
    got = persisted_route_gate(q, route)
    assert got["gate_passed"] is True and got["contract_status"] == "supported"
    assert q == before
    assert q["route_contract"]["candidate_or_execution_authorization"] is False


@pytest.mark.parametrize("mutation,issue", [
    (lambda q: q.update(route_contract=None), "invalid_contract"),
    (lambda q: q["route_contract"].update(version="future_v99"), "unsupported_contract_version"),
    (lambda q: q["route_contract"]["required_datasets_by_route"]["news_catalyst_start"].clear(),
     "contract_declaration_mismatch"),
    (lambda q: q["route_gates"]["news_catalyst_start"].update(required_datasets=[]),
     "route_required_datasets_mismatch"),
    (lambda q: q["route_gates"]["news_catalyst_start"].update(gate_passed=1),
     "invalid_route_gate_boolean"),
    (lambda q: q["route_gates"]["news_catalyst_start"].update(blocking_datasets=["fund_flow"]),
     "contradictory_route_gate"),
    (lambda q: q["route_gates"].pop("news_catalyst_start"), "missing_route_gate"),
])
def test_bad_contract_cannot_fall_back_to_batch_true(mutation, issue):
    q = quality()
    mutation(q)
    got = persisted_route_gate(q, "news_catalyst_start")
    assert got["gate_passed"] is None and got["gate_issue"] == issue


def test_run87_shape_legal_missing_gates_stay_unknown_not_illegal_or_repaired():
    q = {"gate_passed": True, "route_gates": {
        "second_board_promotion": {"gate_passed": True},
        "mainline_spread_start": {"gate_passed": True},
        "auction_surge_start": {"gate_passed": False, "blocking_datasets": ["auction_data"]},
    }}
    row = attempt(candidate_count=1486, ranked_count=17, actionable_count=5,
        metadata_json=json.dumps({"trade_date_by_target": {"1": str(DAY), "2": str(DAY)}, "quality_gate": q}))
    groups = []
    for route, count, ranked, action, target in [
        ("mainline_spread_start", 1, 0, 0, 1), ("second_board_promotion", 44, 5, 5, 2),
        ("news_catalyst_start", 10, 1, 0, 1), ("oversold_reversal_start", 108, 0, 0, 1),
        ("pre_board_probe_start", 1323, 11, 0, 1),
    ]:
        groups.append({**aggregate(), "candidate_route": route, "snapshot_count": count,
                       "ranked_count": ranked, "actionable_count": action, "target_board": target})
    before = deepcopy((row, groups))
    got = window(report([row], groups))["latest_attempt"]
    assert got["diagnostic_status"] == "completed_route_gate_unknown"
    assert got["issues"] == [] and got["persisted_snapshot_count"] == 1486
    assert got["route_contract_status"] == "legacy_unversioned"
    assert got["recorded_route_contract_version"] is None
    for route in ("news_catalyst_start", "oversold_reversal_start", "pre_board_probe_start"):
        item = next(r for r in got["routes"] if r["route"] == route)
        assert item["route_identity"] == "known"
        assert item["gate_status"] == "unknown" and not item["declared_in_quality_contract"]
    assert (row, groups) == before


def test_version_is_frozen_in_diagnostics_and_bad_version_blocks_scheduler():
    q = quality()
    row = attempt(metadata_json=json.dumps({"trade_date_by_target": {"1": str(DAY)}, "quality_gate": q}))
    got = window(report([row], [aggregate()]))["latest_attempt"]
    assert got["recorded_route_contract_version"] == ROUTE_CONTRACT_VERSION
    assert got["diagnostic_status"] == "completed"
    q["route_contract"]["version"] = "unknown"
    assert _promotion_quality_gate_blocks_all_routes(q) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "", "strict_news_catalyst", "invented", ["bad"]])
async def test_invalid_route_rejected_before_filter_or_db_and_direct_writer(bad):
    item = {**candidate(), "candidate_route": bad}
    result = await persist(None, [item], policy=lambda item: False)
    assert result.status == "invalid_candidate_route" and result.touched == 0
    assert result.as_payload()["filtered_count"] is None
    with pytest.raises(ValueError, match="invalid_candidate_route"):
        await append_prediction_run(None, [item], {1: date(2026, 9, 7)},
            identity=get_promotion_model_identity(), snapshot_source="schedule",
            snapshot_context="promotion_0935")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["page", "cron", "scheduler", " schedule "])
async def test_source_alias_or_factor_override_cannot_bypass_official_route_validation(source):
    item = {**candidate(), "candidate_route": "invented"}
    assert (await persist(None, [item], source=source)).status == "invalid_candidate_route"
    with pytest.raises(ValueError, match="invalid_candidate_route"):
        await append_prediction_run(None, [item], {1: date(2026, 9, 7)},
            identity=get_promotion_model_identity(), snapshot_source=source,
            snapshot_context="promotion_0935")


@pytest.mark.asyncio
async def test_invalid_route_appends_blocker_without_touching_prior_evidence(ledger_session):
    valid = candidate()
    original = await persist(ledger_session, [valid])
    legacy = await ledger_session.scalar(select(PromotionPredictionRecord))
    old = legacy.factors_json
    item = {**candidate(), "candidate_route": "invented"}
    batch = ScheduleBatch("promotion_0935", AT)
    blocked = await persist(ledger_session, [valid, item], batch=batch)
    run = await ledger_session.get(PromotionPredictionRun, blocked.ledger.run_id)
    assert run.status == "blocked" and run.candidate_count == 0
    assert json.loads(run.metadata_json)["persistence"]["candidate_validation_stage"] == "route_identity_before_recordability"
    assert (await persist(ledger_session, [valid, item], batch=batch)).ledger.run_id == run.id
    assert legacy.factors_json == old
    assert await ledger_session.get(PromotionPredictionRun, original.ledger.run_id) is not None


@pytest.mark.asyncio
async def test_new_contract_is_persisted_only_on_new_run_without_retroactive_enrichment(ledger_session):
    from test_promotion_ledger import _candidate, _identity
    async def append_item(item, q):
        return await append_prediction_run(ledger_session, [item], {1: date(2026, 8, 28)},
            identity=_identity(), snapshot_source="schedule", snapshot_context="promotion_2000",
            quality_gate=q)
    old_item = _candidate("600001")
    old_quality = {"gate_passed": True, "route_gates": {"fresh_mainline_start": {"gate_passed": True}}}
    original = await append_item(old_item, old_quality)
    old = await ledger_session.get(PromotionPredictionRun, original.run_id)
    old_bytes = old.metadata_json
    q = quality()
    # Same immutable identity is idempotent: cannot enrich its missing contract.
    same = await append_item(old_item, q)
    assert same.run_id == original.run_id and not same.created
    assert old.metadata_json == old_bytes and "route_contract" not in json.loads(old_bytes)["quality_gate"]
    new_item = _candidate("600001", batch_key="schedule:promotion_2000:2026-08-28T20:05:00")
    newer = await append_item(new_item, q)
    new = await ledger_session.get(PromotionPredictionRun, newer.run_id)
    assert new.id != old.id
    assert json.loads(new.metadata_json)["quality_gate"] == q
    assert old.metadata_json == old_bytes
    q["route_contract"]["version"] = "modified_caller_object"
    assert json.loads(new.metadata_json)["quality_gate"]["route_contract"]["version"] == ROUTE_CONTRACT_VERSION


def test_research_uses_same_frozen_version_without_enabling_execution():
    from test_promotion_route_rank_research import fixture_run, compare
    run, rows = fixture_run()
    q = quality()
    q["route_contract"]["version"] = "unsupported_future"
    q["watermarks"] = [{"dataset": "fund_flow", "trade_date": run.as_of_at.date().isoformat(),
                        "status": "ok", "record_count": 100}]
    run.metadata_json = json.dumps({"quality_gate": q})
    result = compare(run, rows)
    assert result["execution"]["frozen_route_gate_passed"] is True
    assert result["execution"]["validated_route_gate_passed"] is None
    assert result["execution"]["status"] == "blocked"
    assert result["execution"]["buy_allowed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy_batch_only", "missing_route", "unsupported", "invalid", "false_route"])
async def test_paper_same_run_unknown_gate_blocks_without_fallback_or_writes(paper_client, monkeypatch, mode):
    _, maker = paper_client
    day = date(2026, 9, 7)
    at = datetime(2026, 9, 7, 9, 35)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=date(2026, 9, 4)))
    async with maker() as db:
        old = _governed_promotion_run(run_key="old-qualified", reference_trade_date=day,
            snapshot_context="promotion_0935", as_of_at=at)
        latest = _governed_promotion_run(run_key="new-missing-proof", reference_trade_date=day,
            snapshot_context="promotion_0935", as_of_at=at + timedelta(seconds=1))
        q = quality()
        if mode == "legacy_batch_only":
            q = {"gate_passed": True}
        elif mode == "missing_route":
            q["route_gates"].pop("second_board_promotion")
        elif mode == "unsupported":
            q["route_contract"]["version"] = "unknown"
        elif mode == "invalid":
            q["route_gates"]["second_board_promotion"] = ["bad"]
        else:
            q["route_gates"]["second_board_promotion"]["gate_passed"] = False
        latest.metadata_json = json.dumps({"quality_gate": q})
        db.add_all([old, latest])
        await db.flush()
        db.add_all([
            _governed_promotion_snapshot(run_id=old.id, record_key="old-stock", code="600001",
                prediction_trade_date=day, probability=.9),
            _governed_promotion_snapshot(run_id=latest.id, record_key="new-stock", code="600001",
                prediction_trade_date=day, probability=.9),
            StockSpot(code="600001", name="test", price=10.15, prev_close=10, change_pct=1.5, volume_ratio=1.8),
        ])
        await db.commit()
        before = (old.metadata_json, latest.metadata_json)
        diagnostics = []
        selected, notes = await paper._promotion_route_buy_candidates(db, limit=10, trade_date=day,
            account_name="promotion", now=at + timedelta(minutes=10), diagnostics=diagnostics)
        assert selected == [] and "禁止回退旧批次" in notes[0]
        assert diagnostics[0]["candidate"]["run_key"] == latest.run_key
        assert diagnostics[0]["candidate"]["requires_new_formal_attempt"] is True
        assert diagnostics[0]["candidate"]["recoverable"] is False
        # A frozen bad batch cannot become valid in-place. An outstanding old
        # order must cancel, not wait indefinitely or borrow a future run's ID.
        from types import SimpleNamespace
        monkeypatch.setattr(paper, "_stable_intraday_entry_quote", lambda *a, **kw: (True, "", None))
        status, _ = await paper._pending_primary_buy_confirmation(
            db, account_name="promotion", source="promotion_promotion",
            candidate={"code": "600001", "_source": "promotion_promotion",
                       "prediction_run_key": old.run_key},
            spot=SimpleNamespace(code="600001", price=10.15, prev_close=10,
                                 avg_price=10.1, high=10.2, limit_up=11,
                                 change_pct=1.5, volume_ratio=1.8),
            limit_price=10.2, now=at + timedelta(minutes=10),
        )
        assert status == "canceled"
        assert (old.metadata_json, latest.metadata_json) == before
        assert not db.new and not db.dirty and not db.deleted
