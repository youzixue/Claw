"""Recovery arrival order must not hide an earlier exact decision or invent fills."""
import json
from datetime import timedelta
import pytest
from app.models.paper import PaperAccount
from app.paper import signal_research as research
from test_paper_signal_research import AT, DAY, db, log_row

def recovered_signal():
    s=log_row(id=10,created_at=AT+timedelta(seconds=40))
    payload=json.loads(s.candidate_json)
    payload["ingress_recovery"]={"schema":"paper_buy_point_ingress_recovery_v1",
        "original_signal_observed_at":AT.isoformat(),"recovered_at":(AT+timedelta(seconds=39)).isoformat(),
        "decision_run_id":"decision","original_error_type":"OperationalError"}
    s.candidate_json=json.dumps(payload)
    return s

async def report(db):
    await db.flush()
    return await research.build_signal_research_report(db,start_date=DAY,end_date=DAY,
                                                       as_of=AT+timedelta(minutes=1))

@pytest.mark.asyncio
async def test_recovered_notification_keeps_prior_exact_decision_and_native_clocks(db):
    db.add(PaperAccount(id=1,account_name="default",initial_capital=50000,status="active"))
    decision=log_row(id=2,action="skip_buy",decision="blocked",run_id="decision",created_at=AT)
    payload=json.loads(decision.candidate_json)
    payload["execution_timing"]={"schema":"paper_entry_wall_clock_v1",
       "clock_status":"ok","log_clock_status":"ok",
       "consumer_started_at":(AT+timedelta(seconds=2)).isoformat(),
       "log_observed_at":(AT+timedelta(seconds=3)).isoformat()}
    decision.candidate_json=json.dumps(payload)
    db.add_all([decision,recovered_signal()])
    s=(await report(db))["signals"][0]
    assert s["ingress_recovered"] is True and s["first_decision_id"]==2
    assert s["first_decision_state"]=="skip_buy:blocked" and s["actual_fill_quantity"]==0
    assert s["first_decision_wall_timing"]["consumer_to_log_seconds"]==1
    assert s["first_decision_wall_timing"]["commit_known_at"] is None
    assert not db.new and not db.dirty

@pytest.mark.asyncio
@pytest.mark.parametrize("kind",["no_marker","wrong_schema","wrong_observed","wrong_run","future_recovery",
                                "other_account","other_version","other_code","other_source","other_round","older"])
async def test_no_recovery_or_wrong_identity_cannot_borrow_earlier_log(db,kind):
    db.add(PaperAccount(id=1,account_name="default",initial_capital=50000,status="active"))
    s=recovered_signal()
    p=json.loads(s.candidate_json)
    if kind=="no_marker":p.pop("ingress_recovery")
    elif kind=="wrong_schema":p["ingress_recovery"]["schema"]="unknown"
    elif kind=="wrong_observed":p["ingress_recovery"]["original_signal_observed_at"]=(AT-timedelta(seconds=1)).isoformat()
    elif kind=="wrong_run":p["ingress_recovery"]["decision_run_id"]="wrong"
    elif kind=="future_recovery":p["ingress_recovery"]["recovered_at"]=(AT+timedelta(days=1)).isoformat()
    s.candidate_json=json.dumps(p)
    changes={
       "other_account":{"account_id":2},"other_version":{"strategy_version":"wrong"},
       "other_code":{"code":"600002"},"other_source":{"source":"wrong"},
       "other_round":{"quote_round_id":"other"},"older":{"created_at":AT-timedelta(seconds=1)},
    }.get(kind,{})
    d=log_row(id=2,action="deferred_buy",decision="wait",run_id="decision",**changes)
    db.add_all([d,s])
    r=(await report(db))["signals"][0]
    assert r["first_decision_id"] is None and r["actual_fill_quantity"]==0
