"""Complete new prediction facts without widening execution; isolated SQLite only."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from app.api.v1 import paper, promotion as p
from app.models.promotion import PromotionPredictionRun, PromotionPredictionSnapshot
from app.models.signal import PromotionPredictionRecord
from app.promotion import ledger
from app.promotion.direction_research import validate_frozen_direction_universe
from app.promotion.modeling.ledger_dataset import _batch_error
from app.promotion.versioning import ProbabilityContractError
from test_promotion_ledger import ledger_session

DAY = date(2026, 9, 24)
AT = datetime(2026, 9, 24, 20)


def candidate(code="600001", *, seed=False, raw_trade_ready=False,
              route="mainline_spread_start", direction=.6):
    return {"code": code, "name": "隔离记录性样本", "target_board": 1,
        "candidate_route": route, "probability": .12, "raw_probability": .2,
        "direction_probability": direction, "is_tradeable": True,
        "trade_ready": raw_trade_ready, "keep_in_diagnostics": True,
        "time_horizon": "sprint", "time_horizon_label": "测试", "time_horizon_reason": "测试",
        "probability_factors": {"prediction_shape_seed": seed,
            "learning_direction_probability_method": "synthetic_route_direction",
            "prediction_model_version": p.PROMOTION_MODEL_VERSION}}


def annotation(raw, *, formal_count=1):
    eligible = p._rank_first_board_candidates(raw, len(raw))
    formal = eligible[:formal_count]
    universe, supplement = p._prediction_record_input_universe(raw, eligible)
    rows = p._annotate_prediction_record_metadata(universe, formal,
        ranked_limit=12, recall_ranked_candidates=eligible, recall_ranked_limit=30,
        rank_eligible_candidates=eligible, recordability_supplement_keys=supplement,
        snapshot_source="schedule", snapshot_context="promotion_2000", recorded_at=AT,
        candidate_anchor_trade_date=DAY)
    return rows, eligible, formal, supplement


class LedgerClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 24, 20, 0, 1)


async def persist(db, rows, monkeypatch):
    monkeypatch.setattr(ledger, "datetime", LedgerClock)
    return await p._record_promotion_predictions(db, rows, {1: DAY},
        snapshot_source="schedule", return_details=True,
        quality_gate={"gate_passed": True, "route_gates": {
            route: {"gate_passed": True, "status": "ok"}
            for route in ("mainline_spread_start", "auction_surge_start", "second_board_promotion")}})


@pytest.mark.parametrize("raw_trade_ready", [False, True])
def test_union_uses_original_facts_and_original_nonexecution_gates(raw_trade_ready):
    raw = [candidate(seed=True), candidate("600002", raw_trade_ready=raw_trade_ready, direction=.9)]
    before = deepcopy(raw)
    rows, eligible, formal, supplement = annotation(raw)
    assert raw == before
    assert len(eligible) == 2 and len(rows) == 2
    assert supplement == {p._prediction_record_key(raw[1])}
    assert [r["code"] for r in rows] == [r["code"] for r in raw]
    assert not p._is_base_recordable_prediction_candidate(raw[1])
    assert all(p._is_recordable_prediction_candidate(row) for row in rows)
    assert [r["probability"] for r in rows] == [r["probability"] for r in raw]
    assert [r["raw_probability"] for r in rows] == [r["raw_probability"] for r in raw]
    assert [r["trade_ready"] for r in rows] == [r["trade_ready"] for r in raw]
    extra = rows[1]
    frozen = extra["probability_factors"]
    source_ranked = next(r for r in eligible if r["code"] == extra["code"])
    assert source_ranked["prediction_actionable"] is False
    assert frozen["prediction_trade_gate_passed"] is source_ranked["trade_ready"] is False
    assert frozen["prediction_watch_only"] is source_ranked["prediction_watch_only"] is True
    assert frozen["prediction_actionable"] is False
    assert frozen["prediction_rank_eligible_count"] == 2
    assert frozen["direction_research"]["eligible_count"] == 2
    assert frozen["direction_research"]["rank_contract_complete"] is True
    assert frozen["prediction_record_admission"] == "rank_eligible_forecast_only"
    assert frozen["prediction_record_base_exclusions"]["keep_in_diagnostics"] is True
    validate_frozen_direction_universe([(r["code"],r["probability_factors"]) for r in rows])
    # Existing base-pool execution flags are identical to the old annotation path.
    old = p._annotate_prediction_record_metadata(raw[:1], formal, ranked_limit=12,
        recall_ranked_candidates=eligible, recall_ranked_limit=30,
        rank_eligible_candidates=eligible, snapshot_source="schedule",
        snapshot_context="promotion_2000", recorded_at=AT)[0]
    for field in ("prediction_trade_gate_passed", "prediction_watch_only", "prediction_actionable",
                  "prediction_ranked_position", "prediction_recall_ranked_position"):
        assert rows[0]["probability_factors"][field] == old["probability_factors"][field]


@pytest.mark.parametrize("field,bad", [
    ("prediction_rank_eligible", False), ("prediction_rank_eligible", 1),
    ("prediction_rank_eligible", "true"), ("prediction_rank_contract_complete", False),
    ("prediction_rank_contract_version", "unknown"),
    ("prediction_record_policy_version", "unknown"),
    ("prediction_record_admission", None), ("prediction_trade_gate_passed", True),
    ("prediction_watch_only", False), ("prediction_actionable", True),
])
def test_supplement_recordability_requires_complete_typed_nonexecuting_contract(field, bad):
    rows, _, _, _ = annotation([candidate()])
    rows[0]["probability_factors"][field] = bad
    assert not p._is_recordable_prediction_candidate(rows[0])


@pytest.mark.parametrize("field,value", [("trade_ready", True),
    ("prediction_watch_only", False), ("prediction_actionable", True)])
def test_conflicting_rank_execution_gates_are_rejected_not_rewritten(field, value):
    raw = [candidate()]
    eligible = p._rank_first_board_candidates(raw, 1)
    eligible[0][field] = value
    universe, supplement = p._prediction_record_input_universe(raw, eligible)
    with pytest.raises(ValueError, match="supplement_not_forecast_only"):
        p._annotate_prediction_record_metadata(universe, eligible, ranked_limit=12,
            rank_eligible_candidates=eligible, recordability_supplement_keys=supplement)
    assert eligible[0][field] == value


def test_missing_original_eligible_is_not_silently_removed():
    with pytest.raises(ValueError, match="eligible_candidate_missing_from_source"):
        p._prediction_record_input_universe([candidate()], [candidate("600999")])


def test_weak_noneligible_stays_excluded_and_seed_and_second_board_stay_recordable():
    weak = candidate()
    weak["time_horizon"] = "weak_watch"
    assert p._rank_first_board_candidates([weak], 1) == []
    assert p._prediction_record_input_universe([weak], []) == ([], set())
    assert p._is_recordable_prediction_candidate(candidate(seed=True))
    assert p._is_recordable_prediction_candidate({**weak, "target_board": 2})


def test_missing_direction_probability_still_blocks_whole_complete_universe():
    rows, _, _, _ = annotation([candidate(seed=True), candidate("600002", direction=None)])
    payload = p._direction_research_payload(rows)
    assert len(rows) == 2
    assert payload["candidate_count"] == 2 and payload["eligible_count"] == 2
    assert payload["status"] == "blocked" and payload["missing_probability_count"] == 1
    assert payload["selected_count"] == 0


@pytest.mark.asyncio
async def test_all_971_eligible_survive_both_writers_and_old_failed_proof_unchanged(ledger_session, monkeypatch):
    # Match deployed SQLite WAL: the legacy ensure-storage helper checks schema
    # on a second connection after the compatibility writer has flushed 970 rows.
    await ledger_session.execute(text("PRAGMA journal_mode=WAL"))
    await ledger_session.commit()
    raw = [candidate(f"{600000+i:06d}", seed=True) for i in range(970)]
    raw.append(candidate("601999"))
    eligible = p._rank_first_board_candidates(raw, len(raw))
    assert len(eligible) == 971
    # Freeze an old incomplete synthetic run. Never update it after the repair.
    old = p._annotate_prediction_record_metadata(raw[:-1], eligible[:12], ranked_limit=12,
        rank_eligible_candidates=eligible, snapshot_source="schedule",
        snapshot_context="promotion_2000", recorded_at=AT-timedelta(minutes=1))
    assert old[0]["probability_factors"]["direction_research"]["error"] == "eligible_candidates_not_recordable"
    old_result = await persist(ledger_session, old, monkeypatch)
    old_snapshots = list((await ledger_session.scalars(select(PromotionPredictionSnapshot).where(
        PromotionPredictionSnapshot.run_id == old_result.ledger.run_id))).all())
    old_bytes = [(s.id, s.record_key, s.features_json) for s in old_snapshots]
    rows, _, _, supplement = annotation(raw, formal_count=12)
    assert len(supplement) == 1 and len(rows) == 971
    result = await persist(ledger_session, rows, monkeypatch)
    await ledger_session.commit()
    assert result.input_count == result.recordable_count == result.prepared_count == result.touched == 971
    run = await ledger_session.get(PromotionPredictionRun, result.ledger.run_id)
    snapshots = list((await ledger_session.scalars(select(PromotionPredictionSnapshot).where(
        PromotionPredictionSnapshot.run_id == run.id))).all())
    records = list((await ledger_session.scalars(select(PromotionPredictionRecord))).all())
    assert run.candidate_count == len(snapshots) == len(records) == 971
    assert run.ranked_count == 12
    assert _batch_error(run, snapshots, AT + timedelta(minutes=1)) is None
    assert {s.code for s in snapshots} == {r["code"] for r in raw}
    assert {r.code for r in records} == {r["code"] for r in raw}
    assert [(s.id,s.record_key,s.features_json) for s in old_snapshots] == old_bytes
    validate_frozen_direction_universe([(s.code,json.loads(s.features_json)) for s in snapshots])
    s = next(s for s in snapshots if s.code == "601999")
    assert s.trade_gate_passed is False and s.watch_only is True and s.actionable is False
    retry = await persist(ledger_session, rows, monkeypatch)
    assert retry.ledger.run_id == run.id and retry.ledger.created is False


@pytest.mark.asyncio
async def test_invalid_production_probability_cannot_be_hidden_by_supplement_policy(monkeypatch):
    rows, _, _, _ = annotation([candidate()])
    rows[0]["probability"] = float("nan")
    with pytest.raises(ProbabilityContractError):
        await persist(None, rows, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("account,route", [("mainline", "mainline_spread_start"),
    ("auction", "auction_surge_start"), ("promotion", "mainline_spread_start")])
async def test_bcd_consumer_cannot_admit_persisted_forecast_only_candidate(
        ledger_session, monkeypatch, account, route):
    raw = [candidate(route=route, raw_trade_ready=True)]
    rows, _, _, _ = annotation(raw)
    result = await persist(ledger_session, rows, monkeypatch)
    await ledger_session.commit()
    snapshot = await ledger_session.scalar(select(PromotionPredictionSnapshot).where(
        PromotionPredictionSnapshot.run_id == result.ledger.run_id))
    monkeypatch.setattr(paper.settings, "PAPER_MAINLINE_ROUTE_POOL_ENABLED", True)
    assert paper._mainline_frozen_route_eligible(snapshot) is False
    for scope in ("pool_unranked", "ranked", "recall_ranked"):
        proxy = SimpleNamespace(rank_scope=scope, features_json=snapshot.features_json,
            trade_gate_passed=snapshot.trade_gate_passed, watch_only=snapshot.watch_only)
        assert paper._mainline_frozen_route_eligible(proxy) is False
    monkeypatch.setattr(paper, "experiment_active", lambda *args, **kwargs: True)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=DAY))
    monkeypatch.setattr(paper, "_quote_round_context", lambda: {})
    diagnostics = []
    result, _ = await paper._promotion_route_buy_candidates(ledger_session, limit=5,
        trade_date=DAY+timedelta(days=1), account_name=account,
        now=AT+timedelta(days=1), diagnostics=diagnostics)
    assert result == []
    if account != "promotion":
        assert any(d.get("reason_code") == "candidate_trade_gate_failed" for d in diagnostics)
    assert snapshot.trade_gate_passed is False and snapshot.actionable is False and snapshot.watch_only is True


@pytest.mark.asyncio
async def test_complete_pool_affects_pool_statistics_without_inventing_formal_membership(ledger_session, monkeypatch):
    raw = [candidate(f"60000{i}", seed=True) for i in range(1, 5)]
    raw.append(candidate("600005"))
    rows, eligible, formal, _ = annotation(raw, formal_count=4)
    assert len(eligible) == 5 and len(formal) == 4
    extra = next(row for row in rows if row["code"] == "600005")
    assert extra["probability_factors"]["learning_eligible"] is False
    await persist(ledger_session, rows, monkeypatch)
    records = list((await ledger_session.scalars(select(PromotionPredictionRecord))).all())
    for record in records:
        record.outcome_status = "success" if record.code == "600005" else "failed"
    await ledger_session.commit()
    stats = await p._load_promotion_learning_stats(ledger_session)
    bucket = stats["T1:mainline_spread_start"]
    assert bucket["sample_count"] == 4 and bucket["success_count"] == 0
    assert bucket["pool_sample_count"] == 5 and bucket["pool_success_count"] == 1
    assert bucket["pool_directional_sample_count"] == 0  # no invented direction bars
    assert bucket["pool_directional_unknown_count"] == 5
