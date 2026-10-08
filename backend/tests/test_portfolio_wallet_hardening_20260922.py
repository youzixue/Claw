"""F1 effective protection/F3 genuine confirmation layers; isolated DB only."""
import json
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.api.v1 import paper
from app.models.paper import PaperPortfolioSignal,PaperAccount,PaperTradeLog
from app.models.trading import TradeOrder,TradeFill
from app.paper import portfolio_wallet as wallet
from app.paper.portfolio_contract import entry_version
from app.paper.portfolio_provenance import PortfolioIdentityError
from test_paper_deferred_exit_provenance import memory_session
from test_portfolio_provenance_20260922 import active

AT=datetime(2026,9,22,14,30)

def origin():
    return dict(portfolio_signal_key="new",origin_account="challenger_a",origin_account_id=2,
        origin_version="source-v1",portfolio_version="policy-v1",entry_version=entry_version("source-v1",policy_version="policy-v1"),
        confirmed_at=AT.isoformat(),as_of_at=AT.isoformat(),candidate={"code":"600001"},
        entry_policy={"stop_loss_price":9.5,"parameters":{"account":{"PAPER_CHALLENGER_A_POSITION_PCT":.2},
            "route_execution":{"target_top_up_enabled":True,"top_up_cooldown_sec":60,
                "top_up_max_daily_layers":3,"top_up_max_cost_return_pct":5,
                "opening_risk_end":"10:00","opening_position_factor":1,"cash_buffer_pct":0}}})

def state(held_stop=None):
    o=origin()
    held=None if held_stop is None else SimpleNamespace(strategy_version=o["entry_version"],
        buy_time=AT-timedelta(days=1),buy_price=10.,buy_amount=100,stop_loss_price=held_stop)
    return dict(account=SimpleNamespace(id=1,initial_capital=50000,max_drawdown=0),
        positions={"600001":held} if held else {},pending={},held={"600001":1000} if held else {},
        total_assets=50000.,cash=49000. if held else 50000.,reserved_cash=0.,
        held_exposure=1000. if held else 0.,pending_exposure=0.,
        symbol_held_exposure=1000. if held else 0.,symbol_pending_exposure=0.,
        occupied_symbols=1 if held else 0,daily_new_symbols=0,is_new_symbol=not bool(held))

@pytest.mark.asyncio
@pytest.mark.parametrize("held_stop,existing_stop,expected",[(None,None,9.5),(9.8,None,9.8),(9.8,9.9,9.9),(9.9,9.7,9.9)])
async def test_all_budget_paths_return_identical_protection(active,monkeypatch,held_stop,existing_stop,expected):
    monkeypatch.setattr(wallet,"wallet_state",AsyncMock(return_value=state(held_stop)))
    result=await wallet.budget_for_origin(None,origin=origin(),price=10.1,now=AT,
        enforce_original_size=False,existing_order_stop_loss_price=existing_stop)
    assert result["allowed"]
    assert result["effective_stop_loss_price"]==expected

@pytest.mark.asyncio
async def test_top_up_risk_uses_same_returned_effective_stop(active,monkeypatch):
    monkeypatch.setattr(wallet,"wallet_state",AsyncMock(return_value=state(9.8)))
    monkeypatch.setattr(wallet,"shared_buy_layers",AsyncMock(return_value=(1,AT-timedelta(minutes=5))),raising=False)
    stops=[]
    def risk(amount,**kwargs):
        stops.append(kwargs["stop_loss"])
        return amount
    monkeypatch.setattr(paper,"_scale_in_risk_amount",risk)
    result=await wallet.budget_for_origin(None,origin=origin(),price=10.1,now=AT,
        existing_order_stop_loss_price=9.9)
    assert result["allowed"] and stops==[9.9]
    assert result["effective_stop_loss_price"]==9.9

@pytest.mark.asyncio
@pytest.mark.parametrize("bad",[float("nan"),float("inf"),-1,0])
async def test_invalid_frozen_protection_fails_closed(active,monkeypatch,bad):
    monkeypatch.setattr(wallet,"wallet_state",AsyncMock(return_value=state(9.8)))
    with pytest.raises(PortfolioIdentityError):
        await wallet.budget_for_origin(None,origin=origin(),price=10.1,now=AT,
            enforce_original_size=False,existing_order_stop_loss_price=bad)

@pytest.mark.asyncio
async def test_stop_hit_never_passes_even_remainder(active,monkeypatch):
    monkeypatch.setattr(wallet,"wallet_state",AsyncMock(return_value=state(10.2)))
    result=await wallet.budget_for_origin(None,origin=origin(),price=10.1,now=AT,enforce_original_size=False)
    assert not result["allowed"] and result["amount"]==0

@pytest.mark.asyncio
async def test_actual_fill_price_below_effective_stop_rejected(monkeypatch):
    db=SimpleNamespace(scalar=AsyncMock(return_value=None))
    cmd=SimpleNamespace(idempotency_key="portfolio:new",quantity=100,price=9.7,decision_at=AT)
    monkeypatch.setattr(wallet,"budget_for_origin",AsyncMock(return_value={
        "allowed":True,"amount":100,"reason_code":"budget_available","effective_stop_loss_price":9.8}))
    with pytest.raises(PortfolioIdentityError,match="protection"):
        await wallet.validate_command_budget(db,cmd,origin())

@pytest.mark.asyncio
@pytest.mark.parametrize("cmd_stop,metadata,existing_stop,allowed", [
    (9.8,{"stop_loss_price":9.8},None,True),
    (9.7,{"stop_loss_price":9.8},None,False),
    (9.8,{"stop_loss_price":9.7},None,False),
    (9.8,{},None,False),
    (None,{"stop_loss_price":9.8},None,False),
    (9.8,{"stop_loss_price":9.8},9.7,False),
    (9.8,{"stop_loss_price":9.8},9.8,True),
])
async def test_locked_effective_stop_must_match_cmd_metadata_and_old_order(
        monkeypatch,cmd_stop,metadata,existing_stop,allowed):
    o=origin()
    existing=None if existing_stop is None else SimpleNamespace(
        code="600001",source="momentum_retest",signal_id="s",strategy_version=o["entry_version"],
        side="buy",status="partial",quantity=200,filled_quantity=100,price=10.1,
        risk_json=json.dumps({"paper_deferred_order":{"stop_loss_price":existing_stop}}))
    db=SimpleNamespace(scalar=AsyncMock(return_value=existing))
    cmd=SimpleNamespace(idempotency_key="portfolio:new",quantity=100,price=10.1,decision_at=AT,
        code="600001",source="momentum_retest",signal_id="s",strategy_version=o["entry_version"],
        stop_loss_price=cmd_stop,deferred_metadata=metadata,queue_metadata=None)
    monkeypatch.setattr(wallet,"budget_for_origin",AsyncMock(return_value={
        "allowed":True,"amount":100,"reason_code":"budget_available","effective_stop_loss_price":9.8}))
    if allowed:
        assert (await wallet.validate_command_budget(db,cmd,o))["allowed"]
    else:
        with pytest.raises(PortfolioIdentityError):
            await wallet.validate_command_budget(db,cmd,o)

async def fill_order(db, key, times, *, code="600001", account="shared_50k", source_version="source-v1"):
    ev=entry_version(source_version,policy_version="policy-v1")
    signal=PaperPortfolioSignal(signal_key=key,portfolio_version="policy-v1",origin_account="challenger_a",
        origin_account_id=2,origin_version=source_version,source="momentum_retest",code=code,
        source_signal_id="confirmation-"+key,confirmed_at=times[0],decision_round_id=key,
        as_of_at=times[0],observed_at=times[0],candidate_json="{}",entry_policy_json="{}",exit_policy_json="{}")
    db.add(signal)
    db.add(TradeOrder(order_id=key,broker="paper",account_id=account,code=code,side="buy",order_type="limit",
        price=10,quantity=100*len(times),filled_quantity=100*len(times),status="filled",source=signal.source,
        signal_id=signal.source_signal_id,strategy_version=ev,idempotency_key="portfolio:"+key,
        risk_json=json.dumps({"paper_portfolio_origin":{"portfolio_signal_key":key,
            "origin_account":"challenger_a","origin_account_id":2,"origin_version":source_version,
            "portfolio_version":"policy-v1","entry_version":ev}}),created_at=times[0]))
    for i,at in enumerate(times):
        db.add(TradeFill(fill_id=f"{key}-{i}",order_id=key,broker="paper",code=code,side="buy",
            price=10,quantity=100,filled_at=at))
        db.add(PaperTradeLog(account_id=1,code=code,trade_type="buy",price=10,amount=100,
            trade_time=at,signal_id=f"pf-slice-{key}-{i}",strategy_version=ev))
    await db.commit()

@pytest.mark.asyncio
async def test_three_slices_one_layer_new_confirmation_second(memory_session):
    db=memory_session
    db.add(PaperAccount(id=1,account_name="shared_50k",initial_capital=50000))
    await db.commit()
    times=[AT-timedelta(minutes=n) for n in (6,5,4)]
    await fill_order(db,"first",times)
    layers,last=await wallet.shared_buy_layers(db,account_id=1,code="600001",now=AT,origin=origin())
    assert layers==1 and last==times[-1]
    await fill_order(db,"second",[AT-timedelta(minutes=2)])
    await fill_order(db,"future",[AT+timedelta(minutes=2)])
    await fill_order(db,"other",[AT-timedelta(minutes=1)],account="default")
    layers,last=await wallet.shared_buy_layers(db,account_id=1,code="600001",now=AT,origin=origin())
    assert layers==2 and last==AT-timedelta(minutes=2)

@pytest.mark.asyncio
async def test_filled_unknown_or_other_origin_cannot_be_ignored(memory_session):
    db=memory_session
    db.add(PaperAccount(id=1,account_name="shared_50k",initial_capital=50000))
    await db.commit()
    await fill_order(db,"foreign",[AT-timedelta(minutes=2)],source_version="unrelated-v2")
    with pytest.raises(PortfolioIdentityError):
        await wallet.shared_buy_layers(db,account_id=1,code="600001",now=AT,origin=origin())

@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["not-json", "[]", '{"paper_deferred_order":{}}',
    '{"paper_limit_up_queue":{"stop_loss_price":null}}',
    '{"paper_limit_up_queue":{"stop_loss_price":"9.8"}}'])
async def test_bad_pending_protection_does_not_fall_back(active,memory_session,monkeypatch,payload):
    db=memory_session
    db.add(TradeOrder(order_id="bad",broker="paper",account_id="shared_50k",code="600001",side="buy",
        order_type="limit",price=10.1,quantity=100,filled_quantity=0,status="submitted",
        idempotency_key="portfolio:new",risk_json=payload))
    await db.commit()
    monkeypatch.setattr(wallet,"wallet_state",AsyncMock(return_value=state(9.8)))
    with pytest.raises(PortfolioIdentityError):
        await wallet.budget_for_origin(db,origin=origin(),price=10.1,now=AT,
            excluding_order_key="portfolio:new",enforce_original_size=False)

@pytest.mark.asyncio
async def test_missing_origin_on_real_fill_is_not_zero_layers(memory_session):
    from sqlalchemy import select
    db=memory_session
    db.add(PaperAccount(id=1,account_name="shared_50k",initial_capital=50000))
    await db.commit()
    await fill_order(db,"broken",[AT-timedelta(minutes=2)])
    order=await db.scalar(select(TradeOrder).where(TradeOrder.order_id=="broken"))
    order.risk_json="{}"
    await db.commit()
    with pytest.raises(PortfolioIdentityError):
        await wallet.shared_buy_layers(db,account_id=1,code="600001",now=AT,origin=origin())

@pytest.mark.asyncio
@pytest.mark.parametrize("kind",["paper_deferred_order","paper_limit_up_queue"])
async def test_existing_order_frozen_stop_from_real_metadata(active,memory_session,monkeypatch,kind):
    db=memory_session
    o=origin()
    db.add(TradeOrder(order_id="remaining",broker="paper",account_id="shared_50k",code="600001",side="buy",
        order_type="limit",price=10.1,quantity=300,filled_quantity=100,status="partial",
        idempotency_key="portfolio:new",risk_json=json.dumps({kind:{"stop_loss_price":9.95}})))
    await db.commit()
    monkeypatch.setattr(wallet,"wallet_state",AsyncMock(return_value=state(9.8)))
    result=await wallet.budget_for_origin(db,origin=o,price=10.1,now=AT,
        excluding_order_key="portfolio:new",enforce_original_size=False)
    assert result["effective_stop_loss_price"]==9.95
