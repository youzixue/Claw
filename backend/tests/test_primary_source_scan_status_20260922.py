"""Explicit source health, not string matching or candidate-count inference."""
from datetime import date
from unittest.mock import AsyncMock
import pytest
from app.api.v1 import paper, tenbagger
from app.config.settings import settings
from test_paper_api import paper_client
from test_paper_buy_point_hooks import clock_and_guards, run_main

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["next_day_plan", "anomaly_buy_point"])
async def test_caught_read_exception_keeps_note_and_explicit_health(monkeypatch, kind):
    target = "prewarm_next_day_plan_snapshot" if kind=="next_day_plan" else "prewarm_anomaly_snapshot"
    monkeypatch.setattr(tenbagger, target, AsyncMock(side_effect=RuntimeError("synthetic source outage")))
    db=AsyncMock()
    func=paper._next_day_plan_buy_candidates if kind=="next_day_plan" else paper._anomaly_buy_point_candidates
    candidates, notes=await func(db,limit=5,trade_date=date(2026,9,8))
    assert candidates==[] and len(notes)==1
    assert notes.issues==[dict(source=kind,reason_code="source_read_failed")]
    db.rollback.assert_awaited_once()

@pytest.mark.asyncio
async def test_enrichment_failure_remains_auditable(monkeypatch):
    monkeypatch.setattr(tenbagger,"prewarm_anomaly_snapshot",
        AsyncMock(return_value={"trade_date":"2026-09-07","anomalies":[]}))
    monkeypatch.setattr(tenbagger,"_enrich_stock_rows_with_b1",
        AsyncMock(side_effect=RuntimeError("synthetic enrichment outage")))
    monkeypatch.setattr(paper,"_is_signal_snapshot_valid_for_trade",AsyncMock(return_value=True))
    monkeypatch.setattr(paper,"_paper_main_fund_map",AsyncMock(return_value={}))
    candidates,notes=await paper._anomaly_buy_point_candidates(
        AsyncMock(),limit=5,trade_date=date(2026,9,8))
    assert candidates==[]
    assert notes.issues==[dict(source="anomaly_buy_point",reason_code="source_enrichment_failed")]
    assert any("B1增强读取失败" in text for text in notes)

@pytest.mark.asyncio
@pytest.mark.parametrize("published",[False,True])
async def test_missing_outbox_publication_is_not_completed(paper_client,monkeypatch,published):
    from app.paper import portfolio_ingress
    _,maker=paper_client
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_ENABLED",True)
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_ACTIVATION_AT","2026-09-08T00:00:00")
    monkeypatch.setattr(portfolio_ingress,"capture_confirmed_signal",
        AsyncMock(return_value=object() if published else None))
    _,_,result,_=await run_main(maker,monkeypatch,"default","next_day_plan")
    assert result["summary"]["source_scan_status"]==("completed" if published else "degraded")
    assert result["summary"]["source_scan_issues"]==(
        [] if published else [dict(source="next_day_plan",code="600888",reason_code="publication_missing")])


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False,True])
async def test_zero_candidates_health_reaches_run_summary(paper_client,monkeypatch,failed):
    _,maker=paper_client
    at,send,submit=clock_and_guards(monkeypatch)
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_ENABLED",False)
    # Misleading text is intentional: health never parses notes.
    notes=paper._SourceScanNotes(["加载失败字样仅是样本，不得由文本分类"],
        issues=[dict(source="next_day_plan",reason_code="source_read_failed")] if failed else [])
    monkeypatch.setattr(paper,"_next_day_plan_buy_candidates",AsyncMock(return_value=([],notes)))
    for name in ("_green_limit_reversal_candidates","_underwater_reversal_candidates",
        "_ma5_pullback_candidates","_anomaly_buy_point_candidates","_restore_armed_reversal_candidates"):
        monkeypatch.setattr(paper,name,AsyncMock(return_value=([],[])))
    monkeypatch.setattr(paper,"_paper_main_fund_map",AsyncMock(return_value={}))
    monkeypatch.setattr(paper,"_is_daily_participation_time",lambda _:False)
    monkeypatch.setattr(paper,"_is_icepoint_reversal_time",lambda _:False)
    async with maker() as db:
        result=await paper.run_paper_auto_trade(db,account_name="default",now=at,
            execute=True,execution_mode="intraday",include_position_risk=False)
    assert result["summary"]["source_scan_status"]==("degraded" if failed else "completed")
    assert result["summary"]["source_scan_issues"]==notes.issues
    assert any("加载失败字样" in log["reason"] for log in result["logs"])
    assert not submit.called and not send.called

@pytest.mark.asyncio
@pytest.mark.parametrize("reason_code,expected", [
    ("candidate_contract_empty","completed"),
    ("probability_contract_invalid","degraded"),
    ("candidate_data_missing","degraded"),
    ("prediction_not_visible","degraded"),
    ("prediction_batch_blocked","degraded"),
])
async def test_probability_zero_is_not_source_failure(paper_client,monkeypatch,reason_code,expected):
    _,maker=paper_client
    at,_,_=clock_and_guards(monkeypatch)
    monkeypatch.setattr(settings,"PAPER_PORTFOLIO_ENABLED",False)
    async def route(*args,diagnostics,**kwargs):
        diagnostics.append(dict(reason="synthetic diagnostic",reason_code=reason_code,
            stage_code="strategy_filter" if reason_code=="candidate_contract_empty" else "data_gate"))
        return [],[]
    monkeypatch.setattr(paper,"_promotion_route_buy_candidates",route)
    async with maker() as db:
        result=await paper.run_paper_auto_trade(db,account_name="promotion",now=at,
            execute=True,execution_mode="intraday",include_position_risk=False)
    assert result["summary"]["source_scan_status"]==expected
