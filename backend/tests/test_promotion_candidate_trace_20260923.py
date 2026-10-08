"""Candidate audit gaps and point-in-time boundary, isolated SQLite only."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.api.v1 import paper
from app.config.settings import settings
from app.models.stock import StockSpot
from test_paper_api import paper_client, _governed_promotion_run, _governed_promotion_snapshot

DAY=date(2026,9,22)
AT=datetime(2026,9,22,9,40)

@pytest.fixture
def candidate_clock(monkeypatch):
    monkeypatch.setattr(paper,"_paper_now",lambda:AT)
    monkeypatch.setattr(paper.trade_calendar,"_cache",{
        DAY-timedelta(days=i):(DAY-timedelta(days=i)).weekday()<5 for i in range(10)})
    monkeypatch.setattr(settings,"PAPER_CONTINUOUS_EXPERIMENT_ENABLED",False)

async def seed(db, account="promotion", specs=None):
    cfg=paper.PAPER_PROMOTION_ACCOUNTS[account]
    run=_governed_promotion_run(run_key="trace-"+account,reference_trade_date=DAY,
        snapshot_context="promotion_0935",as_of_at=AT-timedelta(seconds=60))
    db.add(run);await db.flush()
    snapshots=[]
    for i, spec in enumerate(specs or [{}]):
        code=f"600{110+i}"
        row=_governed_promotion_snapshot(run_id=run.id,record_key=code,code=code,
            prediction_trade_date=DAY,target_board=cfg["target_board"],route=cfg["route"],
            probability=spec.get("probability",.8),actionable=spec.get("actionable",True),
            trade_gate_passed=spec.get("trade_gate_passed",True),watch_only=spec.get("watch_only",False))
        row.created_at=spec.get("created_at",AT-timedelta(seconds=30))
        row.rank_scope=spec.get("rank_scope","trade_pool")
        row.features_json=spec.get("features_json","{}")
        row.rank_position=i+1
        db.add(row)
        if not spec.get("missing_quote"):
            db.add(StockSpot(code=code,name=code,price=10.15,prev_close=10,limit_up=11,
                change_pct=spec.get("change_pct",1.5),volume_ratio=spec.get("volume_ratio",1.6)))
        snapshots.append(row)
    await db.commit()
    return run,snapshots

def traces(ds):
    return [d for d in ds if (d.get("candidate") or {}).get("candidate_trace")]

@pytest.mark.asyncio
async def test_all_snapshot_exclusions_have_individual_reason(paper_client, candidate_clock):
    _,maker=paper_client
    async with maker() as db:
        run,rows=await seed(db,specs=[{"probability":.2},{"watch_only":True},{"actionable":False},
            {"trade_gate_passed":False},{"missing_quote":True},{"volume_ratio":.3},{},{}])
        ds=[]; candidates,_=await paper._promotion_route_buy_candidates(db,limit=1,
            trade_date=DAY,account_name="promotion",now=AT,diagnostics=ds)
    assert [c["code"] for c in candidates]==["600116"]
    records={d["code"]:d for d in traces(ds)}
    assert set(records)=={r.code for r in rows}
    assert [records[f"600{110+i}"]["reason_code"] for i in range(8)]==[
        "candidate_probability_below_floor","candidate_watch_only","candidate_not_actionable",
        "candidate_trade_gate_failed","candidate_quote_missing","candidate_volume_ratio_below_min",
        "candidate_selected","candidate_scan_limit"]
    assert all(d["candidate"]["prediction_run_id"]==run.id for d in records.values())
    assert all(d["candidate"]["prediction_snapshot_id"]==r.id for r in rows for d in [records[r.code]])
    assert records["600117"]["candidate"]["candidate_trace"]["technical_evaluated"] is False

@pytest.mark.asyncio
@pytest.mark.parametrize("account",["promotion","mainline","auction"])
async def test_snapshot_created_after_decision_cannot_be_consumed(paper_client,candidate_clock,account):
    _,maker=paper_client
    async with maker() as db:
        await seed(db,account,specs=[{"created_at":AT+timedelta(seconds=1)}])
        ds=[]; candidates,_=await paper._promotion_route_buy_candidates(db,limit=5,trade_date=DAY,
            account_name=account,now=AT,diagnostics=ds)
    assert candidates==[]
    assert any(d["reason_code"]=="prediction_not_visible" for d in ds)

@pytest.mark.asyncio
@pytest.mark.parametrize("field,value",[("price",float("nan")),("price",float("inf")),
    ("price","bad"),("price",True),("price",0),("price",-1),
    ("prev_close",float("nan")),("prev_close","bad"),("limit_up",float("nan"))])
async def test_bad_quote_is_individual_wait_not_candidate_or_whole_scan_crash(
    paper_client,candidate_clock,monkeypatch,field,value):
    _,maker=paper_client
    async with maker() as db:
        await seed(db)
        quote=SimpleNamespace(code="600110",name="隔离",price=10.15,prev_close=10,
            limit_up=11,change_pct=1.5,volume_ratio=1.6)
        setattr(quote,field,value)
        monkeypatch.setattr(paper,"_spot_by_code",AsyncMock(return_value=quote))
        ds=[]; candidates,_=await paper._promotion_route_buy_candidates(db,limit=5,trade_date=DAY,
            account_name="promotion",now=AT,diagnostics=ds)
    assert candidates==[]
    assert any(d["reason_code"]=="candidate_quote_invalid" for d in traces(ds))


@pytest.mark.asyncio
async def test_snapshot_at_cutoff_is_visible_but_later_than_quote_round_waits(paper_client,candidate_clock):
    _,maker=paper_client
    async with maker() as db:
        _, rows=await seed(db,specs=[{"created_at":AT}])
        cs,_=await paper._promotion_route_buy_candidates(db,limit=1,trade_date=DAY,
            account_name="promotion",now=AT)
        assert len(cs)==1
        token=paper._QUOTE_ROUND_CONTEXT.set({"round_id":"older-round", "as_of_at":AT-timedelta(seconds=1)})
        try:
            ds=[]; cs,_=await paper._promotion_route_buy_candidates(db,limit=1,trade_date=DAY,
                account_name="promotion",now=AT,diagnostics=ds)
            assert not cs and ds[0]["reason_code"]=="prediction_not_visible"
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_index",[0,1])
async def test_future_clock_cannot_hide_known_invalid_probability_contract(paper_client,candidate_clock,bad_index):
    import json
    from test_paper_probability_contract import evidence
    _,maker=paper_client
    async with maker() as db:
        specs=[{"created_at":AT+timedelta(seconds=1)},{}]
        specs[bad_index]["features_json"]=json.dumps({"probability_contract":evidence(None)})
        await seed(db,specs=specs)  # Freeze invalid evidence on insert, never mutate an immutable row.
        ds=[];cs,_=await paper._promotion_route_buy_candidates(db,limit=5,trade_date=DAY,
            account_name="promotion",now=AT,diagnostics=ds)
        assert not cs and any(d["reason_code"]=="probability_contract_invalid" for d in ds)


@pytest.mark.asyncio
async def test_bad_first_quote_does_not_hide_later_valid_candidate(paper_client,candidate_clock,monkeypatch):
    _,maker=paper_client
    async with maker() as db:
        await seed(db,specs=[{},{}])
        original=paper._spot_by_code
        async def quotes(db,code):
            if code=="600110":
                return SimpleNamespace(price="bad",prev_close=10,limit_up=11)
            return await original(db,code)
        monkeypatch.setattr(paper,"_spot_by_code",quotes)
        ds=[]; cs,_=await paper._promotion_route_buy_candidates(db,limit=1,trade_date=DAY,
            account_name="promotion",now=AT,diagnostics=ds)
        assert [c["code"] for c in cs]==["600111"]
        assert [d["reason_code"] for d in traces(ds)]==["candidate_quote_invalid","candidate_selected"]
