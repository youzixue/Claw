"""Two clocks: observe the response later without moving the decision cutoff."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest
from app.paper import confirmation_evidence as c
from app.paper.dependency_research import adapt_paper_record, build_dependency_research_report, DependencyScenario
from test_paper_dependency_research import AT, ASOF, batch, log, attachment

OBSERVED = AT+timedelta(seconds=2)
FUND_ONLY = DependencyScenario("research:fund-only-clock", ("funds",), required_confirmation_layers=())
HISTORY_ONLY = DependencyScenario("research:history-only-clock", (), required_confirmation_layers=("historical_quote_path_confirmed",))


def source_record(monkeypatch, mode="filled", legacy=False):
    monkeypatch.setattr(c, "_observation_now", lambda: OBSERVED)
    earlier = AT-timedelta(days=1) if mode == "canceled" else AT-timedelta(seconds=30)
    prior = {} if legacy else {"confirmation_evidence":{
        "historical_quote_path_confirmed":"true", "historical_confirmed_at":(earlier-timedelta(seconds=2)).isoformat(),
        "historical_log_id":7, "current_setup_valid":"true", "observed_at":earlier.isoformat()}}
    event = "waiting" if mode in ("waiting", "partial_wait") else mode
    status = "submitted" if mode == "waiting" else "partial" if mode == "partial_wait" else mode
    fills = ([{"order_id":"order-1","broker":"paper","code":"600001","side":"buy","quantity":100}]
             if event in ("filled", "partial") else [])
    outcome = {"event":event, "order":{"order_id":"order-1","account_id":"default","broker":"paper",
        "code":"600001","side":"buy","strategy_version":"old-a","decision_round_id":"entry-round",
        "last_fill_round_id":"q1" if fills else "old-fill-round",
        "decision_at":earlier.isoformat(),"trade_date":earlier.date().isoformat(),"status":status}, "fills":fills}
    frozen = c.deferred_buy_log_evidence(prior, outcome=outcome, code="600001", evaluated_at=AT)
    shape = (("buy","executed","fill") if fills else ("deferred_buy","wait","execution") if event == "waiting"
             else ("skip_buy","skipped" if event == "canceled" else "blocked","execution"))
    return log(created_at=AT.isoformat(), action=shape[0], decision=shape[1], stage_code=shape[2],
        reason_code="deferred_"+event, candidate_json=json.dumps(frozen)), frozen


def report(row, frozen_at=ASOF):
    return build_dependency_research_report([row], [batch(frozen_at=frozen_at.isoformat())],
        as_of=ASOF, scenarios=(FUND_ONLY, HISTORY_ONLY))


@pytest.mark.parametrize("mode", ["filled", "partial", "waiting", "partial_wait", "risk_blocked", "rejected", "canceled"])
@pytest.mark.parametrize("legacy", [False, True])
def test_real_helper_response_visible_only_at_physical_cutoff(monkeypatch, mode, legacy):
    row, payload = source_record(monkeypatch, mode, legacy)
    original = deepcopy(row)
    before = report(row, OBSERVED-timedelta(microseconds=1))
    assert before["batches"][0]["cases"][0]["adaptation"]["status"] == "unknown"
    assert before["batches"][0]["paired_log_slots"] == 1
    after = report(row, OBSERVED)
    adapted = after["batches"][0]["cases"][0]["adaptation"]
    assert adapted["status"] == "adapted"
    assert adapted["decision_at"] == AT.isoformat()
    assert adapted["response_available_at"] == OBSERVED.isoformat()
    assert adapted["production_facts"]["order_result"] == payload["confirmation_evidence"]["order_result"]
    assert adapted["confirmation"]["execution_permitted"] == payload["confirmation_evidence"]["execution_permitted"]
    assert adapted["confirmation"]["current_setup_valid"] == "unknown"
    assert adapted["confirmation"]["historical_quote_path_confirmed"] == (
        "unknown" if legacy or mode == "canceled" else "true")
    assert after["schema_version"] == "ab_dependency_research_v2"
    case = after["batches"][0]["cases"][0]
    assert case["frozen_logs"][0]["payload"]["deferred_order_observation"] == payload["deferred_order_observation"]
    assert all(x["execution_permission"] is x["fillable_return"] is None for x in case["scenarios"])
    assert row == original


def test_later_response_never_admits_post_decision_funds_or_sentiment(monkeypatch):
    row, _ = source_record(monkeypatch)
    row["dependency_evidence"] = {
        "funds":attachment(source_quote_at=(AT+timedelta(microseconds=1)).isoformat(),
            received_at=(AT+timedelta(seconds=1)).isoformat(), observed_at=OBSERVED.isoformat()),
        "sentiment":attachment(source="market_sentiment", source_quote_at=AT.isoformat(),
            received_at=(AT+timedelta(seconds=1)).isoformat(), observed_at=OBSERVED.isoformat()),
    }
    case = report(row)["batches"][0]["cases"][0]
    assert case["adaptation"]["production_facts"]["order_result"] == "filled"
    for name in ("funds", "sentiment"):
        assert case["scenarios"][0]["dependency_facts"][name]["availability"] == "unknown"
    assert case["scenarios"][0]["hypothesis_condition"] == "unknown"
    row["dependency_evidence"]["funds"] = attachment()
    case = report(row)["batches"][0]["cases"][0]
    assert case["scenarios"][0]["dependency_facts"]["funds"]["availability"] == "true"
    assert case["scenarios"][0]["hypothesis_condition"] == "true"
    assert case["scenarios"][0]["execution_permission"] is None


@pytest.mark.parametrize("section,key,value", [
    ("observation","schema_version","unknown"),
    ("observation","original_decision_scope","unknown"),
    ("observation","current_setup_basis","rechecked"),
    ("observation","replay_ready",True),
    ("observation","commit_known_at","2026-09-09T10:00:00"),
    ("observation","response_observed_at",None),
    ("observation","response_observed_at","2026-09-09T09:59:59"),
    ("observation","response_observed_at","2026-09-10T10:00:00"),
    ("observation","response_observed_at","2026-09-09T10:00:02+08:00"),
    ("observation","strategy_evaluated_at","2026-09-09T10:00:01"),
    ("observation","execution_permission_basis","permission"),
    ("observation","outcome_event","waiting"),
    ("observation","reported_filled_quantity_this_round",True),
    ("observation","reported_filled_quantity_this_round",0),
    ("observation","reported_filled_quantity_this_round",100.1),
    ("observation","reported_fill_order_identity_matches",False),
    ("order","order_id",None),
    ("order","account_id","promotion"),
    ("order","code","600002"),
    ("order","side","sell"),
    ("order","strategy_version","other"),
    ("order","strategy_version",True),
    ("order","decision_round_id",None),
    ("order","decision_round_id","q1"),
    ("order","last_fill_round_id","previous"),
    ("order","decision_at",None),
    ("order","decision_at","2026-09-09T10:00:01"),
    ("order","trade_date","2026-09-08"),
    ("order","status","partial"),
    ("evidence","observed_at","2026-09-09T10:00:00"),
    ("evidence","execution_permitted","false"),
    ("evidence","current_setup_valid","true"),
    ("evidence","order_result","not_submitted"),
    ("evidence","historical_log_id",8),
    ("record","action","buy_signal"),
    ("record","decision","blocked"),
    ("record","reason_code","deferred_partial"),
    ("record","stage_code","confirmation"),
    ("record","account_id",9),
    ("record","quote_round_id","wrong"),
    ("record","run_id","other"),
])
def test_marker_alone_cannot_relax_legacy_clock_or_log_identity(monkeypatch,section,key,value):
    row, payload = source_record(monkeypatch)
    targets = {"observation":payload["deferred_order_observation"],
        "order":payload["deferred_order_observation"]["order_response"],
        "evidence":payload["confirmation_evidence"], "record":row}
    targets[section][key] = value
    row["candidate_json"] = json.dumps(payload)
    result = report(row)
    assert result["batches"][0]["cases"][0]["adaptation"]["status"] == "unknown"
    assert result["batches"][0]["paired_log_slots"] == 1


@pytest.mark.parametrize("key,value", [
    ("observed_at","2026-09-09T09:59:31"),
    ("historical_confirmed_at","2026-09-09T09:59:31"),
    ("historical_log_id",True),
])
def test_original_history_cannot_come_from_after_entry_decision(monkeypatch,key,value):
    row, payload = source_record(monkeypatch)
    prior = payload["deferred_order_observation"]["original_decision_confirmation"]
    prior[key] = value
    if key != "observed_at":
        payload["confirmation_evidence"][key] = value
    row["candidate_json"] = json.dumps(payload)
    assert report(row)["batches"][0]["cases"][0]["adaptation"]["status"] == "unknown"


def test_legacy_missing_version_is_explicit_and_not_current_version(monkeypatch):
    row,payload = source_record(monkeypatch, legacy=True)
    row["strategy_version"] = "legacy_unversioned"
    payload["deferred_order_observation"]["order_response"]["strategy_version"] = None
    row["candidate_json"] = json.dumps(payload)
    b = batch(strategy_version="legacy_unversioned")
    assert adapt_paper_record(row,b)["status"] == "adapted"
    b["strategy_version"] = "old-a"
    assert adapt_paper_record(row,b)["status"] == "unknown"


def test_legacy_future_evidence_does_not_use_new_late_clock_rule():
    old = log()
    p = json.loads(old["candidate_json"])
    old["created_at"] = AT.isoformat()
    p["confirmation_evidence"]["observed_at"] = OBSERVED.isoformat()
    old["candidate_json"] = json.dumps(p)
    result = adapt_paper_record(old,batch())
    assert result["status"] == "adapted"  # unchanged old shape, unknown evidence
    assert result["confirmation"]["execution_permitted"] == "unknown"
    assert result["production_facts"]["order_result"] == "unknown"
    assert "response_available_at" not in result


def test_changed_response_evidence_is_hashed_without_mutating_inputs(monkeypatch):
    row,payload = source_record(monkeypatch)
    first = report(row)
    original = deepcopy(row)
    payload["deferred_order_observation"]["response_observed_at"] = (OBSERVED+timedelta(microseconds=1)).isoformat()
    payload["confirmation_evidence"]["observed_at"] = (OBSERVED+timedelta(microseconds=1)).isoformat()
    second = report({**row,"candidate_json":json.dumps(payload)})
    assert first["data_hash"] != second["data_hash"]
    assert row == original
