"""Real isolated ledger tests: lock/query waits must not backdate a new fill."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.v1 import paper
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog, PaperSaleAccounting
from app.models.trading import TradeOrder, TradeFill
from app.paper import entry_fee_allocation
from app.trading import service
from app.trading.broker import BrokerOrderRequest
from app.trading.paper_authorization import (
    _paper_execution_scope, authorize_broker_request, authorize_ledger_request,
    validate_ledger_clock, ledger_timing_evidence,
)
from app.trading.paper_public_execution import public_fill_evidence
from paper_immediate_fixture import seed_immediate_quote
from test_paper_public_boundary_20260914 import (
    AT, LocalDB, quote, public_env, paper_client, qualified_execution_risk,
)
from test_paper_immediate_boundary_20260914 import seed_exit


@pytest.fixture(autouse=True)
def fixture_calendar_loader_only(monkeypatch):
    from app.core.trade_calendar import TradeCalendar
    # trade_days_between unconditionally calls _ensure_loaded even when every
    # date is cached. This non-calendar test supplies that loader explicitly;
    # actual day selection/T+1 remain the production methods using fixture days.
    async def loaded_fixture(self,year):
        assert year==2026 and self is paper.trade_calendar
        for i in range(4):
            day=AT.date()-timedelta(days=i)
            assert type(self._cache.get(day)) is bool, day
    monkeypatch.setattr(TradeCalendar,"_ensure_loaded",loaded_fixture)
    sync=AsyncMock(side_effect=AssertionError("clock fixtures must not sync network calendar"))
    monkeypatch.setattr(TradeCalendar,"_sync_from_source",sync)
    yield
    sync.assert_not_awaited()


class ObservedLock:
    def __init__(self):
        self.lock=asyncio.Lock()
        self.waiting=asyncio.Event()
    async def __aenter__(self):
        self.waiting.set()
        await self.lock.acquire()
        return self
    async def __aexit__(self,*args):
        self.lock.release()


async def prepare(factory,monkeypatch,side,at):
    async with factory() as db:
        if side=="sell":
            await seed_exit(db)
        round_id=await seed_immediate_quote(db,monkeypatch,code="000001",at=at,
            price=10 if side=="buy" else 9.99,side=side)
        account=await paper._get_or_create_account(db)
        await paper._refresh_account(db,account)
        baseline={"cash":account.current_capital,
                  "trades":await db.scalar(select(func.count(PaperTradeLog.id)))}
    cmd=service.SubmitOrderCommand(code="000001",side=side,
        price=10.2 if side=="buy" else 9.9,quantity=100,account_id="default",
        decision_at=at,as_of_at=at-timedelta(seconds=3),decision_round_id=round_id,
        signal_id="clock-fixture-"+side,require_immediate_quote=False)
    return cmd,baseline


async def assert_no_new_fill(factory,result,side,baseline,phase):
    if result["order"]["status"] == "risk_blocked":
        # Invalid/cross-day actual clocks now fail BEFORE invoking the risk engine/broker.
        proof = result["risk"]["paper_locked_risk"]
        assert phase == "lock_acquired"
        assert proof["status"] == "blocked"
        assert proof["reason_code"] == "invalid_risk_evaluation_clock"
        assert "broker_rejection_http_status" not in result["risk"]
    else:
        assert result["order"]["status"]=="rejected",result
        assert phase in result["order"]["error_message"]
        assert result["risk"]["broker_rejection_http_status"]==409
    assert result["risk"]["paper_immediate_execution"]["status"]=="fillable"
    assert result["fills"]==[]
    async with factory() as db:
        assert await db.scalar(select(func.count(PaperTradeLog.id)))==baseline["trades"]
        assert await db.scalar(select(func.count(TradeFill.id)))==0
        assert await db.scalar(select(func.count(PaperSaleAccounting.id)))==0
        account=await db.scalar(select(PaperAccount))
        assert account.current_capital==baseline["cash"]
        if side=="sell":
            pos=await db.scalar(select(PaperPosition))
            assert pos.buy_amount==100 and not pos.is_closed
        else:
            assert await db.scalar(select(func.count(PaperPosition.id)))==0


@pytest.mark.asyncio
@pytest.mark.parametrize("side",["buy","sell"])
@pytest.mark.parametrize("at,later",[
    (AT,AT+timedelta(seconds=91)),
    (AT.replace(hour=11,minute=29,second=59),AT.replace(hour=11,minute=30)),
    (AT.replace(hour=14,minute=56,second=59),AT.replace(hour=14,minute=57)),
    (AT,AT+timedelta(days=1)),(AT,AT-timedelta(microseconds=1)),
    (AT,None),(AT,AT.replace(tzinfo=timezone.utc)),
])
async def test_actual_lock_wait_cannot_use_frozen_dispatch_time(public_env,monkeypatch,side,at,later):
    _,factory,_=public_env
    cmd,baseline=await prepare(factory,monkeypatch,side,at)
    now=[at]
    monkeypatch.setattr(paper,"_public_order_clock",lambda:now[0])
    lock=ObservedLock()
    monkeypatch.setattr(paper,"_TRADE_LOCK",lock)
    await lock.lock.acquire()
    async def submit():
        async with factory() as db:
            return await service.submit_order(db,cmd)
    task=asyncio.create_task(submit())
    try:
        await asyncio.wait_for(lock.waiting.wait(),5)
        now[0]=later
        lock.lock.release()
        result=await asyncio.wait_for(task,5)
    finally:
        if not task.done():
            task.cancel()
        if lock.lock.locked():
            lock.lock.release()
        await asyncio.gather(task,return_exceptions=True)
    await assert_no_new_fill(factory,result,side,baseline,"lock_acquired")


@pytest.mark.asyncio
@pytest.mark.parametrize("side",["buy","sell"])
@pytest.mark.parametrize("stage",["account_refresh","last_preparation"])
@pytest.mark.parametrize("later",[AT+timedelta(seconds=91),AT.replace(hour=16),AT-timedelta(microseconds=1)])
async def test_preparation_queries_cannot_outlive_final_mutation_guard(public_env,monkeypatch,side,stage,later):
    _,factory,_=public_env
    cmd,baseline=await prepare(factory,monkeypatch,side,AT)
    now=[AT]
    monkeypatch.setattr(paper,"_public_order_clock",lambda:now[0])
    owner,name=(paper,"_refresh_account") if stage=="account_refresh" else (
        (paper,"_stock_info") if side=="buy" else (entry_fee_allocation,"load_entry_fee_plan"))
    original=getattr(owner,name)
    async def delayed(*args,**kwargs):
        result=await original(*args,**kwargs)
        # Shared refresh/stock helpers also run BEFORE dispatch during real risk.
        # Inject the wait only in the actual authorized ledger stage under test.
        from app.trading.paper_authorization import _SCOPE
        scope=_SCOPE.get()
        if scope is not None and scope.stage=="ledger":
            now[0]=later
        return result
    monkeypatch.setattr(owner,name,delayed)
    async with factory() as db:
        result=await service.submit_order(db,cmd)
    await assert_no_new_fill(factory,result,side,baseline,"before_mutation")


@pytest.mark.asyncio
@pytest.mark.parametrize("side",["buy","sell"])
async def test_logical_fill_clock_is_final_mutation_check_not_dispatch(public_env,monkeypatch,side):
    _,factory,_=public_env
    cmd,_=await prepare(factory,monkeypatch,side,AT)
    # Two extra observations delimit the new locked-risk work, not ledger mutation.
    clocks=iter([AT,AT,AT,AT,AT+timedelta(seconds=1),AT+timedelta(seconds=2)])
    monkeypatch.setattr(paper,"_public_order_clock",lambda:next(clocks,AT+timedelta(seconds=2)))
    async with factory() as db:
        result=await service.submit_order(db,cmd)
        assert result["order"]["status"]=="filled",result["order"]["error_message"]
        fill=await db.scalar(select(TradeFill))
        trade=await db.get(PaperTradeLog,int(fill.broker_trade_id))
        assert fill.filled_at==trade.trade_time==AT+timedelta(seconds=2)
        timing=json.loads(fill.raw_json)["ledger_execution_timing"]
        assert timing==result["risk"]["paper_ledger_timing"]
        assert timing["dispatch_validated_at"]==AT.isoformat()
        assert timing["lock_acquired_checked_at"]==(AT+timedelta(seconds=1)).isoformat()
        assert timing["before_mutation_checked_at"]==(AT+timedelta(seconds=2)).isoformat()
        assert timing["physical_commit_at"] is None
        assert cmd.decision_at==AT
        if side=="sell":
            assert trade.strategy_version=="legacy-fixture"


async def scope_inputs(monkeypatch):
    monkeypatch.setattr(service,"_paper_execution_spot",AsyncMock(return_value=quote()))
    monkeypatch.setattr(paper,"_public_order_clock",lambda:AT)
    proof=await public_fill_evidence(LocalDB(),service.SubmitOrderCommand(
        code="000001",side="buy",price=10.2,quantity=100,decision_at=AT),now=AT)
    assert proof["status"]=="fillable"
    req=BrokerOrderRequest(order_id="clock",code="000001",side="buy",price=10,
        quantity=100,decision_round_id="fixture-round",fill_round_id="fixture-round",filled_at=AT)
    return proof,req,paper.SimBuyRequest(code="000001",price=10,amount=100,signal_id="clock")


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value",[
    ("code","000002"),("side","sell"),("account_id","promotion"),
    ("fill_price",11),("filled_quantity",True),("filled_quantity",200),
    ("quote_round_id","different"),("status","unknown"),("mandatory",False),
    ("contract_version","unknown"),("quote_max_age_sec",0),("quote_max_age_sec",True),
    ("quote_expires_at",(AT+timedelta(days=1)).isoformat()),
    ("session_end_at",AT.replace(hour=16).isoformat()),("source_quote_at",None),
])
async def test_frozen_scope_proof_must_match_execution_and_original_deadline(monkeypatch,key,value):
    proof,req,book=await scope_inputs(monkeypatch)
    proof[key]=value
    db=object()
    with _paper_execution_scope(db,req,immediate_evidence_json=json.dumps(proof)):
        authorize_broker_request(db,req)
        authorize_ledger_request(db,book,side="buy",account_name="default")
        with pytest.raises(HTTPException) as error:
            validate_ledger_clock(db,phase="lock_acquired")
        assert error.value.status_code==409


@pytest.mark.asyncio
async def test_terminal_guard_requires_lock_and_cannot_be_reused_or_cross_task(monkeypatch):
    proof,req,book=await scope_inputs(monkeypatch)
    db=object()
    with _paper_execution_scope(db,req,immediate_evidence_json=json.dumps(proof)):
        authorize_broker_request(db,req)
        authorize_ledger_request(db,book,side="buy",account_name="default")
        with pytest.raises(HTTPException):
            validate_ledger_clock(db,phase="before_mutation")
        validate_ledger_clock(db,phase="lock_acquired")
        async def wrong_task():
            with pytest.raises(HTTPException) as error:
                validate_ledger_clock(db,phase="before_mutation")
            assert error.value.status_code==403
        await asyncio.create_task(wrong_task())
        validate_ledger_clock(db,phase="before_mutation")
        with pytest.raises(HTTPException):
            validate_ledger_clock(db,phase="before_mutation")
        result=ledger_timing_evidence(db)
        result["status"]="mutated-owned-copy"
        assert ledger_timing_evidence(db)["status"]=="validated"


@pytest.mark.asyncio
@pytest.mark.parametrize("offset,allowed",[(87,True),(87.000001,False)])
async def test_exact_frozen_age_boundary_cannot_be_extended_by_live_config(monkeypatch,offset,allowed):
    proof,req,book=await scope_inputs(monkeypatch)
    db=object()
    with _paper_execution_scope(db,req,immediate_evidence_json=json.dumps(proof)):
        authorize_broker_request(db,req)
        authorize_ledger_request(db,book,side="buy",account_name="default")
        validate_ledger_clock(db,phase="lock_acquired")
        monkeypatch.setattr(paper.settings,"PAPER_EXECUTION_QUOTE_MAX_AGE_SEC",900)
        monkeypatch.setattr(paper,"_public_order_clock",lambda:AT+timedelta(seconds=offset))
        if allowed:
            assert validate_ledger_clock(db,phase="before_mutation")==AT+timedelta(seconds=87)
        else:
            with pytest.raises(HTTPException):
                validate_ledger_clock(db,phase="before_mutation")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload",["","broken","null","[]","false"])
async def test_present_invalid_scope_proof_is_not_a_deferred_scope(monkeypatch,payload):
    _,req,book=await scope_inputs(monkeypatch)
    db=object()
    with _paper_execution_scope(db,req,immediate_evidence_json=payload):
        authorize_broker_request(db,req)
        authorize_ledger_request(db,book,side="buy",account_name="default")
        with pytest.raises(HTTPException) as error:
            validate_ledger_clock(db,phase="lock_acquired")
        assert error.value.status_code==409
