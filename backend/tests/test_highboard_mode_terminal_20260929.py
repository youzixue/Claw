"""A known E/E2 mode transition terminates old pending orders before unrelated quote unknowns."""
import copy
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select
from app.api.v1 import paper
from app.trading import service
from app.models.trading import TradeOrder,TradeFill
from test_highboard_entry_mode_20260928 import AT,quote,frozen
from test_paper_deferred_exit_provenance import memory_session

@pytest.mark.asyncio
@pytest.mark.parametrize("account",["tenbagger","challenger_e"])
@pytest.mark.parametrize("geometry",["switched","same","unknown"])
async def test_mode_terminal_precedes_missing_vwap(monkeypatch,account,geometry):
    candidate=frozen(account)
    if account=="challenger_e":candidate["entry_variant"]="e2_limit_touch"
    before=copy.deepcopy(candidate)
    fields=(dict(price=10.99,high=11.) if account=="challenger_e"
            else dict(price=9.99,low=9.98))
    if geometry=="same":
        fields=dict(price=11.,high=11.) if account=="challenger_e" else {}
    if geometry=="unknown":fields["prev_close"]=None
    q=quote(**{**fields,"avg_price":None})
    factory=AsyncMock(side_effect=AssertionError("No candidate regeneration through missing VWAP"))
    monkeypatch.setattr(paper,"_tenbagger_midline_candidates",factory)
    status,_=await paper._pending_primary_buy_confirmation(None,account_name=account,
        source="tenbagger_midline",candidate=candidate,spot=q,limit_price=11.,now=AT)
    assert status==("canceled" if geometry=="switched" else "waiting")
    assert candidate==before and not factory.called

@pytest.mark.asyncio
@pytest.mark.parametrize("account,queue",[("tenbagger",False),("challenger_e",True)])
async def test_real_order_mode_change_cannot_resurrect_on_next_frame(memory_session,monkeypatch,account,queue):
    db=memory_session
    monkeypatch.setattr(service,"experiment_active",lambda *_a,**_kw:False)
    risk=AsyncMock(return_value={"final_level":"pass","block_reasons":[],"warnings":[]})
    broker=AsyncMock()
    broker.place_order.side_effect=AssertionError("Old canceled order must never reach broker")
    monkeypatch.setattr(service,"_pre_trade_risk_check",risk)
    monkeypatch.setattr(service,"get_broker_adapter",lambda _:broker)
    fields=(dict(price=11.,high=11.,avg_price=10.7,change_pct=10.,
                 bid1_price=11.,ask1_price=0.,ask1_volume=0.) if queue else {})
    spot=quote(**{**fields,"volume":50000,"bid1_volume":100,"quote_round_id":"decision"})
    candidate=frozen(account)
    if queue:candidate["entry_variant"]="e2_limit_touch"
    monkeypatch.setattr(service,"_paper_execution_spot",AsyncMock(side_effect=lambda *_a,**_k:spot))
    factory=AsyncMock(return_value=([copy.deepcopy(candidate)],[]))
    monkeypatch.setattr(paper,"_tenbagger_midline_candidates",factory)
    metadata={"candidate":candidate,"confirmed_at":AT.isoformat(),"cancel_time":"14:50"}
    command=service.SubmitOrderCommand(code=spot.code,side="buy",quantity=100,price=spot.price,
        account_id=account,strategy_id="paper-auto-short",source="tenbagger_midline",
        strategy_version=paper._strategy_version(account),signal_id="mode-terminal",
        decision_at=AT,as_of_at=AT,decision_round_id="decision",
        queue_if_limit_up=queue,defer_until_next_round=not queue,
        queue_metadata=metadata if queue else None,
        deferred_metadata=metadata if not queue else None)
    result=await service.submit_order(db,command)
    assert result["order"]["status"]=="submitted",result
    order=await db.scalar(select(TradeOrder))
    initial=copy.deepcopy(json.loads(order.risk_json))
    key="paper_limit_up_queue" if queue else "paper_deferred_order"
    original_candidate=initial[key]["candidate"]
    at=AT+timedelta(seconds=30)
    spot.price=10.99 if queue else 9.99
    spot.avg_price=None
    async def step(at,round_id):
        spot.updated_at=spot.received_at=spot.source_quote_at=at
        spot.quote_round_id=round_id
        token=paper._QUOTE_ROUND_CONTEXT.set({"round_id":round_id,"quality_status":"ok",
            "as_of_at":at,"committed_at":at})
        try:
            if queue:return await service.reconcile_paper_limit_up_orders(db,account_id=account,now=at)
            return await service.reconcile_paper_deferred_orders(db,account_id=account,now=at,round_id=round_id)
        finally:paper._QUOTE_ROUND_CONTEXT.reset(token)
    first=await step(at,"mode-switched")
    assert first[0]["event"]=="canceled",first
    assert order.status=="canceled" and order.filled_quantity==0
    spot.price=11. if queue else 10.1
    spot.avg_price=10.7 if queue else 10.05
    spot.volume=50101
    assert await step(at+timedelta(seconds=30),"mode-returned")==[]
    assert order.status=="canceled" and order.filled_quantity==0
    assert list((await db.scalars(select(TradeFill))).all())==[]
    assert not broker.place_order.called and not factory.called
    assert json.loads(order.risk_json)[key]["candidate"]==original_candidate
    assert json.loads(order.risk_json)[key]["buy_validity"]==initial[key]["buy_validity"]


@pytest.mark.parametrize("contract",["HIGHBOARD_PENDING_MODE_CONTRACT_VERSION","LIMIT_QUEUE_EVIDENCE_CONTRACT_VERSION"])
def test_new_pending_contracts_rotate_only_highboard_accounts(monkeypatch,contract):
    from app.paper import experiment
    names=experiment.EXPERIMENT_ACCOUNTS
    before={name:paper._strategy_version(name) for name in names}
    monkeypatch.setattr(experiment,contract,"different-contract")
    assert {name for name in names if paper._strategy_version(name)!=before[name]}=={"tenbagger","challenger_e"}
