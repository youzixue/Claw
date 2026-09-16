"""Entry fees are allocated, not paid twice. All orders below use isolated DBs."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError

from app.api.v1 import paper
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog, PaperSaleAccounting
from app.models.stock import StockTag
from app.models.trading import TradeFill
from app.paper.entry_fee_allocation import (
    VERSION, plan_entry_fee, proportional_entry_fee, load_entry_fee_plan, sale_evidence_payload,
)
from app.trading import service
from test_paper_api import paper_client
from paper_immediate_fixture import seed_immediate_quote

AT = datetime(2026, 9, 14, 10)


@pytest.fixture
def fee_calendar(monkeypatch):
    """Explicit calendar for both real-ledger fee cases; no lazy remote loading."""
    monkeypatch.setattr(paper.trade_calendar, "_cache", {
        (AT-timedelta(days=i)).date(): (AT-timedelta(days=i)).weekday() < 5 for i in range(32)})
    async def loaded(self, year):
        assert self is paper.trade_calendar and year == AT.year
        assert self._cache[AT.date()] is True
    monkeypatch.setattr(type(paper.trade_calendar), "_ensure_loaded", loaded)
    network = AsyncMock(side_effect=AssertionError("fee fixture forbids calendar network"))
    monkeypatch.setattr(type(paper.trade_calendar), "_sync_from_source", network)
    yield
    network.assert_not_awaited()


def trade(tid, side="buy", quantity=100, fee=5, **kw):
    return dict(id=tid,account_id=1,code="000001",trade_type=side,amount=quantity,
                price=10,commission=fee,tax=0,trade_time=AT+timedelta(seconds=tid),**kw)


def plan(rows, quantity, sale, proofs=None):
    return plan_entry_fee(rows,account_id=1,code="000001",position_quantity=quantity,
                          sell_quantity=sale,at=AT+timedelta(hours=1),prior_evidence=proofs)


def test_partial_sell_rebuy_uses_remaining_fees_not_all_historical_buys():
    rows=[trade(1,quantity=200),trade(2,"sell"),trade(3)]
    original=deepcopy(rows)
    out=plan(rows,200,200)
    assert out["status"]=="known"
    assert out["entry_fee_before"]==7.5 and out["allocated_entry_fee"]==7.5
    assert out["entry_fee_after"]==0 and out["cycle_entry_fees_paid"]==10
    assert out["legacy_sell_count_in_open_cycle"]==1
    assert out["prior_sale_allocations_evidenced"] is False
    assert rows==original
    old_bug=round(10*200/300,2)
    assert old_bug==6.67 and old_bug!=out["allocated_entry_fee"]


def test_cent_allocations_exhaust_exact_remainder():
    rows=[trade(1,quantity=300)]
    first=plan(rows,300,100)
    rows.append(trade(2,"sell"))
    first["sale_trade_id"]=2
    second=plan(rows,200,100,{2:first})
    second["sale_trade_id"]=3
    rows.append(trade(3,"sell"))
    final=plan(rows,100,100,{2:first,3:second})
    assert [x["allocated_entry_fee"] for x in (first,second,final)]==[1.67,1.67,1.66]
    assert sum(Decimal(str(x["allocated_entry_fee"])) for x in (first,second,final))==5
    assert final["entry_fee_after"]==0 and final["prior_sale_allocations_evidenced"] is True


def test_old_closed_cycle_does_not_leak_fees_into_new_entry():
    result=plan([trade(1,quantity=100,fee=9),trade(2,"sell"),trade(3,fee=5)],100,100)
    assert result["allocated_entry_fee"]==5
    assert result["trade_ids_in_open_cycle"]==[3]


def test_observed_zero_fee_is_not_missing():
    result=plan([trade(1,fee=0)],100,100)
    assert result["status"]=="known" and result["allocated_entry_fee"]==0


@pytest.mark.parametrize("field,value,reason",[
    ("account_id",2,"cross_account"),("code","000002","cross_account"),
    ("id",None,"trade_id"),("id",True,"trade_id"),
    ("amount",None,"number"),("amount",0,"quantity"),("amount",1.5,"quantity"),
    ("price",float("nan"),"nonfinite"),("price",0,"price"),
    ("commission",None,"number"),("commission",-1,"fee"),("commission",.001,"cents"),
    ("tax",float("inf"),"nonfinite"),("tax",True,"number"),
    ("trade_type","transfer","trade_type"),
    ("trade_time",AT+timedelta(days=1),"future"),("trade_time",None,"clock"),
])
def test_bad_history_unknown_not_zero(field,value,reason):
    row=trade(1);row[field]=value
    result=plan([row],100,100)
    assert result["status"]=="unknown" and reason in result["reason"]
    assert result["allocated_entry_fee"] is None


@pytest.mark.parametrize("rows,quantity,sale",[
    ([],100,100),([trade(1),trade(1)],100,100),
    ([trade(1,"sell")],100,100),([trade(1)],200,100),
    ([trade(1)],100,200),([trade(1)],100,0),
])
def test_missing_duplicate_or_conflicting_inventory_fails_closed(rows,quantity,sale):
    result=plan(rows,quantity,sale)
    assert result["status"]=="unknown" and result["reason"]
    assert result["allocated_entry_fee"] is None


@pytest.mark.parametrize("field,value",[
    ("version","unknown"),("account_id",2),("entry_fee_before",9),
    ("allocated_entry_fee",0),("entry_fee_after",7),("status","unknown"),
])
def test_conflicting_prior_new_proof_is_not_legacy_missing(field,value):
    first=plan([trade(1,quantity=200)],200,100)
    first[field]=value
    result=plan([trade(1,quantity=200),trade(2,"sell")],100,100,{2:first})
    assert result["status"]=="unknown" and result["reason"]=="prior_fee_evidence_conflict"


@pytest.mark.parametrize("mutation", ["clock", "price", "fee"])
def test_mutated_prior_history_is_detected_by_original_proof_hash(mutation):
    rows=[trade(1,quantity=200)]
    first=plan(rows,200,100)
    first["sale_trade_id"]=2
    rows.append(trade(2,"sell"))
    if mutation=="clock":
        rows[0]["trade_time"]+=timedelta(microseconds=1)
    elif mutation=="price":
        rows[0]["price"]=10.1
    else:
        rows[0]["commission"]=6
    result=plan(rows,100,100,{2:first})
    assert result["status"]=="unknown" and result["reason"]=="prior_fee_evidence_conflict"


def test_orphan_proof_and_invalid_clock_do_not_certify_unknown_history():
    assert plan([trade(1)],100,100,{99:{}})["reason"]=="orphan_fee_evidence"
    result=plan_entry_fee([],account_id=1,code="000001",position_quantity=100,
                         sell_quantity=100,at=None)
    assert result["status"]=="unknown" and result["allocated_entry_fee"] is None


@pytest.mark.asyncio
async def test_real_ledger_partial_rebuy_cash_and_fee_evidence_conservation(paper_client,monkeypatch,fee_calendar):
    _,factory=paper_client
    # Isolate accounting risk; quote evidence below is explicit, not a validator mock.
    monkeypatch.setattr(service,"_pre_trade_risk_check",AsyncMock(return_value={
        "final_level":"pass","evaluation_status":"complete","block_reasons":[],"warnings":[]}))
    async with factory() as db:
        db.add(StockTag(code="000001",name="隔离费用样本",board_type="main_sz",
                       board_tag="tradeable",is_st=False,is_suspended=False,is_delisting=False))
        await db.commit()
        results=[]
        for idx,(side,qty,price,day) in enumerate([
            ("buy",200,10,10),("sell",100,11,11),("buy",100,12,11),("sell",200,13,14),
        ]):
            at=datetime(2026,9,day,10,idx)
            await seed_immediate_quote(db,monkeypatch,code="000001",at=at,
                price=price,side=side,name="隔离费用样本")
            result=await service.submit_order(db,service.SubmitOrderCommand(
                code="000001",side=side,price=price,quantity=qty,signal_id=f"fee-{idx}",
                decision_at=at,account_id="default",idempotency_key=f"fee-order-{idx}"))
            assert result["order"]["status"]=="filled",result
            results.append(result)
        trades=(await db.scalars(select(PaperTradeLog).order_by(PaperTradeLog.id))).all()
        proofs=(await db.scalars(select(PaperSaleAccounting).order_by(PaperSaleAccounting.id))).all()
        assert len(proofs)==2
        payloads=[json.loads(x.payload_json) for x in proofs]
        assert [p["allocated_entry_fee"] for p in payloads]==[2.5,7.5]
        assert payloads[-1]["entry_fee_after"]==0
        assert payloads[-1]["prior_sale_allocations_evidenced"] is True
        assert sum(p["allocated_entry_fee"] for p in payloads)==sum(t.commission+t.tax for t in trades if t.trade_type=="buy")
        expected=500-sum(t.commission+t.tax for t in trades)
        realized=sum(t.realized_pnl for t in trades if t.trade_type=="sell")
        assert realized==pytest.approx(expected)
        account=await db.scalar(select(PaperAccount))
        assert account.current_capital-account.initial_capital==pytest.approx(expected)
        assert account.total_assets==account.current_capital
        fills=(await db.scalars(select(TradeFill).where(TradeFill.side=="sell"))).all()
        assert all(json.loads(f.raw_json)["entry_fee_allocation"]["version"]==VERSION for f in fills)
        # Reload/call replay must not append a duplicate proof or apply another fee.
        repeat=await service.submit_order(db,service.SubmitOrderCommand(
            code="000001",side="sell",price=13,quantity=200,signal_id="fee-3",
            idempotency_key="fee-order-3",decision_at=AT))
        assert repeat["idempotent_replay"] is True
        assert repeat["order"]["order_id"]==results[-1]["order"]["order_id"]
        duplicate=await service.submit_order(db,service.SubmitOrderCommand(
            code="000001",side="sell",price=13,quantity=200,signal_id="fee-3",
            idempotency_key="different-order-same-fill-request",decision_at=at))
        assert duplicate["order"]["status"]=="rejected" and duplicate["fills"]==[]
        assert "重复认领" in duplicate["order"]["error_message"]
        assert await db.scalar(select(func.count(TradeFill.id)))==4
        assert await db.scalar(select(func.count(PaperSaleAccounting.id)))==2
        from app.paper.accounting import load_accounting
        report=await load_accounting(db,account,as_of=AT.date())
        assert report["sale_fee_evidence_scope"]=="new_sales_only_legacy_not_backfilled"
        assert report["trades"][trades[-1].id]["entry_fee_allocation"]["allocated_entry_fee"]==7.5


@pytest.mark.parametrize("field,value", [
    ("allocated_entry_fee",float("inf")),("allocated_entry_fee",True),
    ("entry_fee_before",-1),("entry_fee_after",.001),("sell_quantity",True),
    ("position_quantity",0),("input_sha256","not-a-digest"),
    ("sale_trade_id",99),("account_id",2),("code","000002"),
])
def test_malformed_display_evidence_never_claims_known(field,value):
    from types import SimpleNamespace
    payload=plan([trade(1)],100,100)
    payload["sale_trade_id"]=2
    proof=SimpleNamespace(version=VERSION,trade_id=2,account_id=1,code="000001",
                          payload_json=json.dumps(payload))
    assert sale_evidence_payload(proof)["status"]=="known"
    payload[field]=value
    proof.payload_json=json.dumps(payload)
    assert sale_evidence_payload(proof)["status"]=="unknown"


@pytest.mark.asyncio
async def test_missing_entry_history_prevents_zero_fee_sale(paper_client,monkeypatch,fee_calendar):
    _,factory=paper_client
    monkeypatch.setattr(service,"_pre_trade_risk_check",AsyncMock(return_value={
        "final_level":"pass","block_reasons":[],"warnings":[]}))
    monkeypatch.setattr(paper,"_paper_now",lambda:AT)
    async with factory() as db:
        account=await paper._get_or_create_account(db)
        db.add(PaperPosition(account_id=account.id,code="000001",buy_price=10,buy_amount=100,
            buy_time=AT-timedelta(days=3),current_price=10,is_closed=False))
        await db.commit()
        await seed_immediate_quote(db,monkeypatch,code="000001",at=AT,price=10,side="sell")
        result=await service.submit_order(db,service.SubmitOrderCommand(
            code="000001",side="sell",price=10,quantity=100,decision_at=AT))
        assert result["order"]["status"]=="rejected"
        assert "分摊依据不完整" in result["order"]["error_message"]
        assert await db.scalar(select(func.count(PaperTradeLog.id)))==0
        assert await db.scalar(select(func.count(PaperSaleAccounting.id)))==0
        assert (await db.scalar(select(PaperPosition))).buy_amount==100
        assert await db.scalar(select(func.count(TradeFill.id)))==0


@pytest.mark.asyncio
async def test_evidence_table_append_only_and_unique(paper_client):
    _,factory=paper_client
    async with factory() as db:
        db.add(PaperTradeLog(id=99,account_id=1,code="000001",trade_type="sell",
            price=10,amount=100,commission=5,tax=0,trade_time=AT))
        await db.flush()
        db.add(PaperSaleAccounting(trade_id=99,account_id=1,code="000001",version=VERSION,
            recorded_at=AT,payload_json='{"fixture":true}'))
        await db.commit()
        for sql in [
            "UPDATE paper_sale_accounting SET payload_json='{}' WHERE trade_id=99",
            "DELETE FROM paper_sale_accounting WHERE trade_id=99",
        ]:
            with pytest.raises(IntegrityError,match="append-only"):
                await db.execute(text(sql))
            await db.rollback()
        db.add(PaperSaleAccounting(trade_id=99,account_id=1,code="000001",version=VERSION,
            recorded_at=AT,payload_json='{}'))
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()
