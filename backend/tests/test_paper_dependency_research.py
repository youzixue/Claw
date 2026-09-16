"""Existing paper payload fixtures, no business DB/network/production writes."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json

import pytest

from app.config.settings import settings
from app.paper.dependency_research import DependencyScenario, build_dependency_research_report

AT = datetime(2026,9,9,10)
ASOF = AT+timedelta(seconds=5)
BASE = DependencyScenario("research:all", ("funds","sentiment","auction"))
NO_AUCTION = DependencyScenario("research:without-auction", ("funds","sentiment"))
CONFIRM = DependencyScenario("research:confirmation-only", ())


def batch(**kw):
    return {"batch_id":"batch1","account_id":8,"account_name":"default","account_ref":"account:8",
        "strategy_version":"old-a","decision_run_id":"run1","quote_round_id":"q1",
        "universe_ref":"frozen-scan:run1","frozen_at":ASOF.isoformat(),"expected_log_ids":[1],**kw}


def payload(**kw):
    return {"confirmation_evidence":{"historical_quote_path_confirmed":"true",
        "historical_confirmed_at":(AT-timedelta(seconds=30)).isoformat(),"historical_log_id":99,
        "current_setup_valid":"true","execution_permitted":"false","order_result":"not_submitted",
        "observed_at":AT.isoformat()},"main_net_inflow":None,"fund_data_degraded":True,
        "component_watermarks":{"fund_flow_observed_at":AT.isoformat()},
        "prediction_run_key":"prediction:known", "probability":.8,"snapshot_actionable":True,
        **kw}


def log(**kw):
    return {"id":1,"account_id":8,"account_name":"default","strategy_version":"old-a",
        "run_id":"run1","quote_round_id":"q1","trade_date":"2026-09-09","as_of_at":AT.isoformat(),
        "created_at":ASOF.isoformat(),"code":"600001","source":"sudden_pullback",
        "price":10,"action":"skip_buy","decision":"blocked","reason_code":"risk_blocked",
        "candidate_score":80,"executed_trade_id":None,"candidate_json":json.dumps(payload()),
        "risk_json":json.dumps({"passed":False,"block_reasons":["quality missing"]}),**kw}


def attachment(**kw):
    return {"log_id":1,"account_id":8,"account_name":"default","strategy_version":"old-a",
        "decision_run_id":"run1","quote_round_id":"q1","code":"600001","evidence_ref":"frozen-source:1",
        "source":"fund_flow","source_version":"frozen-provider-v1","trade_date":"2026-09-09",
        "source_quote_at":(AT-timedelta(seconds=30)).isoformat(),
        "received_at":(AT-timedelta(seconds=20)).isoformat(),"observed_at":(AT-timedelta(seconds=10)).isoformat(),
        "main_net_inflow":0,"main_net_inflow_pct":0,"quality_status":"ok","score":50,"phase":"recovery",**kw}


def report(records=None,batches=None,scenarios=(BASE,NO_AUCTION,CONFIRM)):
    return build_dependency_research_report([log()] if records is None else records,
        [batch()] if batches is None else batches,as_of=ASOF,scenarios=scenarios)


def case(r):
    return r["batches"][0]["cases"][0]


def test_existing_general_log_adapts_without_fabricating_source_provenance():
    r = report()
    c = case(r)
    assert c["adaptation"]["production_facts"]["confirmation"]["execution_permitted"] == "false"
    assert c["adaptation"]["production_facts"]["ranking"]["probability"] == .8
    assert c["adaptation"]["observed_payload"]["fund_values"]["main_net_inflow"] is None
    assert c["scenarios"][0]["hypothesis_condition"] == "unknown"
    assert c["scenarios"][2]["hypothesis_condition"] == "true"
    assert all(s["execution_permission"] is s["fillable_return"] is None for s in c["scenarios"])


def test_real_buy_signal_v2_market_context_does_not_prove_funds_or_sentiment():
    p = {"account_name":"default","notification_schema":"paper_buy_point_v2",
         "research_capture_schema":"all_confirmed_v1","signal_observed_at":AT.isoformat(),
         "decision_run_id":"run1","signal_key":"confirmed:1",
         "market_context":{"schema":"signal_market_context_v1","price":10,"prev_close":9.8,
            "source_quote_at":AT.isoformat(),"quote_round_id":"q1","confirmation_sample_count":3},
         "signal_labels":{"schema":"signal_labels_v1","strategy_version":"old-a","status":"recorded",
            "observed_at":AT.isoformat(),"sentiment":"unknown","regime":"recovery"}}
    r = report([log(action="buy_signal",decision="confirmed",run_id="bp-hashed",candidate_json=json.dumps(p))])
    c = case(r)
    assert c["adaptation"]["signal"]["evidence_status"] == "valid"
    assert c["adaptation"]["production_facts"]["original_signal_confirmed"] == "true"
    assert c["adaptation"]["production_facts"]["confirmation"]["execution_permitted"] == "unknown"
    assert all(s["hypothesis_condition"] == "unknown" for s in c["scenarios"])


def test_zero_is_known_fund_value_not_missing_or_positive_signal():
    r = report([log(dependency_evidence={"funds":attachment(),"sentiment":attachment(source="market_sentiment")})])
    c = case(r)
    assert c["scenarios"][0]["dependency_facts"]["funds"]["values"]["main_net_inflow"] == 0
    assert c["scenarios"][0]["hypothesis_condition"] == "unknown"
    assert c["scenarios"][1]["hypothesis_condition"] == "true"
    assert c["adaptation"]["production_facts"]["confirmation"]["execution_permitted"] == "false"


@pytest.mark.parametrize("value",[None,True,float("nan"),float("inf"),"0"])
def test_invalid_fund_values_never_fill_zero(value):
    c = case(report([log(dependency_evidence={"funds":attachment(main_net_inflow=value)})]))
    assert c["scenarios"][0]["dependency_facts"]["funds"]["availability"] == "unknown"


@pytest.mark.parametrize("change",[{"account_id":9},{"account_name":"promotion"},{"strategy_version":"new"},
    {"decision_run_id":"other"},{"quote_round_id":"other"},{"log_id":2},{"code":"600002"},
    {"source_quote_at":None},{"received_at":None},{"observed_at":"2026-09-09T10:00:01"},
    {"source_quote_at":"2026-09-08T10:00:00"},{"evidence_ref":[]},{"source":"tencent_realtime"}])
def test_source_attachment_identity_and_cutoff_fail_closed(change):
    c = case(report([log(dependency_evidence={"funds":attachment(**change)})]))
    assert c["scenarios"][0]["dependency_facts"]["funds"]["availability"] == "unknown"


def test_bare_auction_complete_and_even_derived_stamp_not_raw_proof():
    for p in (payload(auction_feed_complete=True), payload(auction_feed_complete=True,
        auction_evidence_status="ok",auction_evidence_contract="auction_provenance_v1")):
        row = log(candidate_json=json.dumps(p),dependency_evidence={"auction":attachment(
            auction_feed_complete=True,auction_evidence_status="ok",auction_evidence_contract="auction_provenance_v1")})
        c = case(report([row]))
        assert c["scenarios"][0]["dependency_facts"]["auction"]["availability"] == "unknown"
        assert c["scenarios"][0]["hypothesis_condition"] == "unknown"


def test_all_scenarios_keep_same_log_and_candidate_denominators_with_missing_slots():
    r = report([log()], [batch(expected_log_ids=[1,2])])
    b = r["batches"][0]
    assert b["paired_log_slots"] == 2 and len(b["cases"]) == 2
    assert b["declared_candidate_codes"] == ["600001"]
    assert b["cases"][1]["adaptation"]["reason"] == "log_missing_or_duplicate"
    assert all(s["hypothesis_condition"] == "unknown" for s in b["cases"][1]["scenarios"])


@pytest.mark.parametrize("change",[{"account_id":9},{"account_name":"promotion"},{"strategy_version":"new"},
    {"run_id":"other"},{"quote_round_id":"other"},{"code":"６００００１"},
    {"created_at":"2026-09-09T10:00:06"},{"as_of_at":None}])
def test_log_instance_version_batch_clock_rejected_but_slot_retained(change):
    r = report([log(**change)])
    assert len(r["batches"][0]["cases"]) == 1
    assert case(r)["adaptation"]["status"] == "unknown"


def test_confirmation_layers_are_not_reconstructed_from_bool_or_future_logs():
    p = payload(execution_confirmation=True)
    p.pop("confirmation_evidence")
    assert case(report([log(candidate_json=json.dumps(p))]))["scenarios"][2]["hypothesis_condition"] == "unknown"
    p = payload()
    p["confirmation_evidence"]["observed_at"] = "2026-09-09T10:00:06"
    assert case(report([log(candidate_json=json.dumps(p))]))["scenarios"][2]["hypothesis_condition"] == "unknown"


def test_mask_is_only_hypothesis_does_not_change_facts_rank_or_frozen_inputs(monkeypatch):
    row = log(dependency_evidence={"funds":attachment(),"sentiment":attachment(source="market_sentiment")})
    original = deepcopy(row)
    masked = DependencyScenario("research:fund-outage",("funds","sentiment"),mask_unavailable=("funds",))
    a = report([row],scenarios=(NO_AUCTION,masked))
    c = case(a)
    assert c["scenarios"][0]["hypothesis_condition"] == "true"
    assert c["scenarios"][1]["hypothesis_condition"] == "unknown"
    assert c["scenarios"][0]["dependency_facts"] == c["scenarios"][1]["dependency_facts"]
    monkeypatch.setattr(settings,"FUND_FLOW_SOURCE_MAX_AGE_SEC",1)
    monkeypatch.setattr(settings,"AUCTION_SOURCE_MAX_AGE_SEC",1)
    assert report([row],scenarios=(NO_AUCTION,masked)) == a
    assert row == original


def test_legacy_signal_record_reference_is_not_current_confirmation():
    p = {"notification_schema":"paper_buy_point_v1","account_name":"default","decision_run_id":"run1","signal_key":"legacy"}
    c = case(report([log(action="buy_signal",candidate_json=json.dumps(p))]))
    assert c["adaptation"]["signal"]["evidence_status"] == "legacy_record_reference"
    assert c["adaptation"]["production_facts"]["original_signal_confirmed"] == "unknown"


def test_repeated_log_duplicate_batch_empty_and_malformed_inputs():
    assert case(report([log(),log()]))["adaptation"]["reason"] == "log_missing_or_duplicate"
    r = report(batches=[batch(),batch()])
    assert all(b["status"] == "unknown" for b in r["batches"])
    assert report(records=[],batches=[])["batches"] == []
    assert report(records=[{"id":[]}])["unassigned_log_count"] == 1
    assert case(report([log(candidate_json="bad")]))["scenarios"][0]["hypothesis_condition"] == "unknown"


def test_hash_binds_bad_source_and_missing_vs_nonfinite_and_future_does_not_change_slots():
    a = report([log(dependency_evidence={"funds":attachment(source_quote_at="bad-a")})])
    b = report([log(dependency_evidence={"funds":attachment(source_quote_at="bad-b")})])
    assert a["data_hash"] != b["data_hash"]
    base = report()
    future = log(id=2,created_at="2026-09-09T11:00:00")
    added = report([log(),future])
    assert case(base) == case(added)
    assert added["unassigned_log_count"] == 1
    assert report([log(dependency_evidence={"funds":attachment(main_net_inflow=None)})])["data_hash"] != report(
        [log(dependency_evidence={"funds":attachment(main_net_inflow=float("nan"))})])["data_hash"]


def test_raw_malformed_payload_hashes_and_signal_decision_are_not_collapsed():
    assert report([log(candidate_json="bad-a")])["data_hash"] != report([log(candidate_json="bad-b")])["data_hash"]
    assert report([log(risk_json="bad-a")])["data_hash"] != report([log(risk_json="bad-b")])["data_hash"]
    p = {"account_name":"default","notification_schema":"paper_buy_point_v2","decision_run_id":"run1",
         "signal_key":"signal1","signal_observed_at":AT.isoformat()}
    c = case(report([log(action="buy_signal",decision="blocked",candidate_json=json.dumps(p))]))
    assert c["adaptation"]["production_facts"]["original_signal_confirmed"] == "unknown"


def test_B_latest_batch_identity_and_risk_facts_not_replaced_by_A_scenario():
    b = batch(account_id=9,account_name="promotion",strategy_version="b-v1")
    row = log(account_id=9,account_name="promotion",strategy_version="b-v1",
        risk_json=json.dumps({"final_level":"block","block_reasons":[{"category":"sentiment","rule":"quality"}]}))
    r = report([row],[b])
    c = case(r)
    assert c["adaptation"]["production_facts"]["risk"]["final_level"] == "block"
    assert c["scenarios"][2]["hypothesis_condition"] == "true"
    assert c["scenarios"][2]["execution_permission"] is None
    wrong = dict(row,quote_round_id="older")
    r = report([wrong],[b])
    assert case(r)["adaptation"]["status"] == "unknown"
    assert case(r)["frozen_logs"][0]["risk"]["final_level"] == "block"


def test_evidence_age_is_research_versioned_not_live_production_threshold():
    row = log(dependency_evidence={"funds":attachment()})
    strict = DependencyScenario("research:fresh-10sec",("funds",),max_evidence_age_sec=10)
    relaxed = DependencyScenario("research:fresh-60sec",("funds",),max_evidence_age_sec=60)
    c = case(report([row],scenarios=(strict,relaxed)))
    assert c["scenarios"][0]["hypothesis_condition"] == "unknown"
    assert c["scenarios"][1]["hypothesis_condition"] == "true"
    assert c["adaptation"]["production_facts"]["confirmation"]["execution_permitted"] == "false"


def test_same_name_old_instance_source_version_unassigned_or_rejected_not_combined():
    a, b = log(), log(id=2,account_id=99)
    r = report([a,b],[batch(expected_log_ids=[1,2])])
    assert r["batches"][0]["paired_log_slots"] == 2
    assert r["batches"][0]["cases"][1]["adaptation"]["status"] == "unknown"
    assert all(s["hypothesis_condition"] == "unknown" for s in r["batches"][0]["cases"][1]["scenarios"])


@pytest.mark.parametrize("change",[{"version":"production"},{"required_dependencies":("funds","funds")},
    {"required_dependencies":("other",)},{"required_confirmation_layers":("execution_permitted",)},
    {"max_evidence_age_sec":True},{"mask_unavailable":["funds"]}])
def test_research_only_scenario_validation(change):
    with pytest.raises(ValueError):
        replace(BASE,**change)
