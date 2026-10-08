"""Owned-leaf observations, exact returns, serial call order and cancellation cleanup."""
import asyncio
from datetime import date
import json
from unittest.mock import AsyncMock
import pytest
from app.api.v1 import paper
from app.paper import confirmation_evidence as evidence

@pytest.mark.asyncio
async def test_wrapper_identity_and_owned_leaves(monkeypatch,caplog):
    ticks=iter([1000000,2000000,9000000])
    monkeypatch.setattr(evidence,"perf_counter_ns",lambda:next(ticks))
    secret=object()
    notes=paper._SourceScanNotes(["human text"],issues=[
        {"source":"plan","reason_code":"source_read_failed","private":secret}])
    rows=[{"secret":secret}]
    result=(rows,notes)
    operation=AsyncMock(return_value=result)
    with caplog.at_level("INFO",logger=evidence.__name__):
        returned=await evidence.observe_candidate_stage("plan",operation,
            round_id="round-one",span_account_id=12)
    assert returned is result and returned[1] is notes and returned[0] is rows
    records=[json.loads(r.getMessage().split("] ",1)[1]) for r in caplog.records]
    assert [r["status"] for r in records]==["started","completed"]
    assert records[1]["elapsed_ms"]==8 and records[1]["candidate_count"]==1
    assert records[1]["source_scan_status"]=="degraded"
    assert records[1]["source_scan_issues"]==[{"source":"plan","reason_code":"source_read_failed"}]
    assert records[0]["span_id"]==records[1]["span_id"]
    assert records[1]["account_id"]==12 and records[1]["round_id"]=="round-one"
    assert "secret" not in caplog.text and "cache" not in caplog.text

@pytest.mark.asyncio
@pytest.mark.parametrize("error",[RuntimeError("original"),asyncio.CancelledError("cancel")])
async def test_exception_identity_and_next_span_has_no_leaked_state(monkeypatch,error):
    records=[]
    monkeypatch.setattr(evidence,"_emit_candidate_stage",records.append)
    with pytest.raises(type(error)) as caught:
        await evidence.observe_candidate_stage("plan",AsyncMock(side_effect=error),round_id="bad")
    assert caught.value is error
    expected="cancelled" if isinstance(error,asyncio.CancelledError) else "failed"
    assert [r["status"] for r in records]==["started",expected]
    await evidence.observe_candidate_stage("green",AsyncMock(return_value=([],[])),round_id="next")
    assert [r["status"] for r in records]==["started",expected,"started","completed"]
    assert records[-1]["round_id"]=="next" and records[-1]["source_scan_issues"]==[]
    assert records[-1]["span_id"]!=records[0]["span_id"]

@pytest.mark.asyncio
async def test_actual_task_cancellation_runs_source_cleanup(monkeypatch):
    records=[]; entered=asyncio.Event(); cleaned=[]
    monkeypatch.setattr(evidence,"_emit_candidate_stage",records.append)
    async def source():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)
    task=asyncio.create_task(evidence.observe_candidate_stage("plan",source,round_id="cancel-round"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned==[True]
    assert [r["status"] for r in records]==["started","cancelled"]
    assert records[-1]["elapsed_ms"]>=0


@pytest.mark.asyncio
async def test_broken_logging_cannot_change_source_result(monkeypatch):
    monkeypatch.setattr(evidence.logger,"info",lambda *a,**k: (_ for _ in ()).throw(ValueError("logger")))
    result=([],paper._SourceScanNotes())
    assert await evidence.observe_candidate_stage("plan",AsyncMock(return_value=result)) is result

@pytest.mark.asyncio
@pytest.mark.parametrize("failure",[None,"green","cancel"])
async def test_real_aggregator_calls_serial_stages_and_stops_at_failure(monkeypatch,failure):
    stages=["plan","green","underwater","ma5","anomaly","daily","icepoint","restore","fund"]
    names=["_next_day_plan_buy_candidates","_green_limit_reversal_candidates",
        "_underwater_reversal_candidates","_ma5_pullback_candidates","_anomaly_buy_point_candidates",
        "_daily_participation_candidates","_icepoint_reversal_candidates",
        "_restore_armed_reversal_candidates","_paper_main_fund_map"]
    calls=[]; records=[]; active=0
    sentinel=object()
    monkeypatch.setattr(evidence,"_emit_candidate_stage",records.append)
    def factory(stage):
        async def run(db,**kwargs):
            nonlocal active
            assert db is sentinel and active==0
            active+=1; calls.append(stage)
            try:
                await asyncio.sleep(0)
                if stage=="green" and failure:
                    if failure=="cancel": raise asyncio.CancelledError()
                    raise RuntimeError("source failure")
                if stage=="fund": return {}
                if stage in ("daily","icepoint"): return []
                return [],paper._SourceScanNotes(["note"])
            finally:
                active-=1
        return run
    for stage,name in zip(stages,names):
        monkeypatch.setattr(paper,name,factory(stage))
    token=paper._QUOTE_ROUND_CONTEXT.set({"round_id":"actual-round"})
    try:
        coro=paper._paper_auto_buy_candidates(sentinel,limit=5,trade_date=date(2026,9,22),
            account_id=7,include_daily_participation=True,include_icepoint_reversal=True)
        if failure:
            with pytest.raises(asyncio.CancelledError if failure=="cancel" else RuntimeError):
                await coro
            assert calls==["plan","green"]
            assert records[-1]["status"]==("cancelled" if failure=="cancel" else "failed")
        else:
            candidates,notes=await coro
            assert candidates==[] and isinstance(notes,paper._SourceScanNotes)
            assert calls==stages
            assert [r["stage"] for r in records if r["status"]=="completed"]==stages
            assert records[-1]["result_count"]==0 and records[-1]["input_candidate_count"]==0
        assert active==0
        assert all(r["round_id"]=="actual-round" and r["account_id"]==7 for r in records)
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)
