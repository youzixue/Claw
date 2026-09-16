"""Forward observation is not a signal, sell permission or old-state backfill."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, text

from app.api.v1 import paper
from app.models.paper import PaperAutoTradeLog, PaperPosition, PaperShadowEvent, PaperTradeLog
from app.paper import position_observation as m
from test_paper_api import paper_client

AT=datetime(2026,9,9,10,0)


@pytest.fixture(autouse=True)
def wall_clock(monkeypatch):
    monkeypatch.setattr(m,"_now",lambda:AT)


def position(**kw):
    return SimpleNamespace(**({"id":2,"account_id":4,"code":"000001","buy_time":AT-timedelta(days=1),
        "buy_price":10.0,"buy_amount":100,"strategy_version":"old-v1",
        "stop_loss_price":9.0,"hold_days":1,"is_closed":False}|kw))


def quote(**kw):
    return SimpleNamespace(**({"quote_round_id":"qr-1","source_quote_at":AT-timedelta(seconds=2),
        "received_at":AT-timedelta(seconds=1),"updated_at":AT,"price":10.2,
        "high":11.0,"low":9.8,"limit_up":11.0,"limit_down":9.0}|kw))


def context(**kw):
    return {"round_id":"qr-1","committed_at":AT,"as_of_at":AT-timedelta(seconds=2),
        "code_version":"runtime-code","config_version":"runtime-config",**kw}


def capture(**kw):
    args=dict(position=position(),account_name="default",trade_date=AT.date(),quote=quote(),
        quote_context=context(),evaluated_at=AT,quote_ok=True,quote_reason="",
        exit_parameters={"stop_loss_pct":4.0,"take_profit_pct":8.0,"max_hold_days":3},
        exit_policy={"basis":"frozen_entry_order","order_id":"original","missing_keys":[]},trigger_reason="")
    args.update(kw)
    return m.capture_position_frame(**args)


def test_owned_state_and_late_execution_are_distinct(monkeypatch):
    pos=position();q=quote();params={"stop_loss_pct":4.0}
    frame=capture(position=pos,quote=q,exit_parameters=params,trigger_reason="原卖点")
    pos.buy_amount=0;q.price=1;params["stop_loss_pct"]=99
    after=AT+timedelta(seconds=3)
    row=m._event(frame,{"available_sell_amount":100,"exit_trigger_reason":"原卖点",
        "execution_block_code":"quote_invalid","exit_execution_status":"blocked"},after)
    payload=json.loads(row["snapshot_json"])
    assert payload["pre_execution"]["position"]["buy_amount"]==100
    assert payload["pre_execution"]["quote"]["price"]==10.2
    assert payload["pre_execution"]["exit_parameters_observed"]["stop_loss_pct"]==4
    assert row["observed_at"]==after and payload["commit_known_at"] is None
    assert payload["execution_observation"]["available_sell_amount"]==100
    assert payload["execution_permission"] is None and payload["replay_ready"] is False
    assert row["event_type"]!="confirmed" and row["assumed_fill_price"] is None
    assert payload["pre_execution"]["session_high_scope"].startswith("session_quote")


@pytest.mark.parametrize("kwargs",[
    {"trade_date":(AT-timedelta(days=1)).date()}, {"evaluated_at":AT+timedelta(seconds=1)},
    {"evaluated_at":AT.replace(tzinfo=timezone.utc)},{"quote_context":{}},
    {"quote_context":context(committed_at=AT+timedelta(seconds=1))},
    {"quote_context":context(round_id="")},{"position":position(id=None)},
    {"position":position(account_id=True)},{"position":position(code="bad")},
])
def test_no_manual_historical_or_future_capture(kwargs):
    assert capture(**kwargs) is None


@pytest.mark.parametrize("qty",[None,True,-100,100.5,"100"])
def test_unchecked_or_invalid_quantity_is_not_zero_or_permission(qty):
    ctx={} if qty is None else {"available_sell_amount":qty}
    row=m._event(capture(),ctx,AT)
    payload=json.loads(row["snapshot_json"])
    assert payload["execution_observation"]["available_sell_amount"] is None
    assert payload["execution_permission"] is None


@pytest.mark.parametrize("bad",[True, False, -1, float("nan"), float("inf"), 10**400])
def test_invalid_column_price_is_not_coerced_to_measured_value(bad):
    assert m._event(capture(quote=quote(price=bad)),{},AT)["price"] is None


def test_semantic_dedup_and_changed_state_are_separate(monkeypatch):
    frame=capture()
    first=m._event(frame,{},AT)
    monkeypatch.setattr(m,"_now",lambda:AT+timedelta(seconds=2))
    repeat=m._event(capture(),{},AT+timedelta(seconds=2))
    assert first["event_key"]==repeat["event_key"]
    assert first["event_key"]!=m._event(capture(position=position(buy_amount=200)),{},AT+timedelta(seconds=2))["event_key"]
    assert first["event_key"]!=m._event(capture(position=position(account_id=5)),{},AT+timedelta(seconds=2))["event_key"]
    assert first["event_key"]!=m._event(frame,{"available_sell_amount":0},AT)["event_key"]


@pytest.mark.asyncio
async def test_append_dedup_then_caller_rollback_really_removes_event(paper_client):
    _,maker=paper_client
    async with maker() as db:
        await db.execute(text("SELECT 1"))  # SQLite logical transaction only, no prior write.
        assert (await m.append_position_frames(db,[(capture(),{})]))["status"]=="staged_not_committed"
        assert len((await db.scalars(select(PaperShadowEvent))).all())==1
        await db.rollback()
    async with maker() as db:
        assert (await db.scalars(select(PaperShadowEvent))).all()==[]
        await m.append_position_frames(db,[(capture(),{}),(capture(),{})])
        await db.commit()
    async with maker() as db:
        rows=(await db.scalars(select(PaperShadowEvent))).all()
        assert len(rows)==1 and rows[0].route_id==m.ROUTE_ID
        assert (await db.scalars(select(PaperAutoTradeLog))).all()==[]
        assert (await db.scalars(select(PaperTradeLog))).all()==[]


@pytest.mark.asyncio
async def test_audit_does_not_flush_dirty_business_objects(paper_client):
    _,maker=paper_client
    async with maker() as db:
        invalid=PaperPosition(account_id=None,code="bad")
        db.add(invalid)
        called=[]
        event.listen(db.sync_session,"before_flush",lambda *_:called.append(True))
        result=await m.append_position_frames(db,[(capture(),{})])
        assert result["status"]=="staged_not_committed" and called==[] and invalid in db.new
        await db.rollback()


@pytest.mark.asyncio
async def test_core_insert_failure_rolls_back_only_audit(paper_client):
    _,maker=paper_client
    async with maker() as db:
        conn=await db.connection()
        await conn.execute(text("CREATE TEMP TABLE retained_marker(value INTEGER)"))
        await conn.execute(text("INSERT INTO retained_marker VALUES (7)"))
        await conn.execute(text("CREATE TEMP TRIGGER reject_frame BEFORE INSERT ON paper_shadow_event BEGIN SELECT RAISE(ABORT, 'no audit'); END"))
        result=await m.append_position_frames(db,[(capture(),{})])
        assert result["status"]=="unavailable"
        assert (await conn.execute(text("SELECT value FROM retained_marker"))).scalar_one()==7
        assert (await conn.execute(text("SELECT count(*) FROM paper_shadow_event"))).scalar_one()==0
        await db.commit()


@pytest.mark.asyncio
async def test_cancellation_not_swallowed(paper_client,monkeypatch):
    _,maker=paper_client
    async with maker() as db:
        async def cancel():raise asyncio.CancelledError()
        monkeypatch.setattr(db,"connection",cancel)
        with pytest.raises(asyncio.CancelledError):await m.append_position_frames(db,[(capture(),{})])


async def prepare_scan(maker,monkeypatch,mode):
    async def build(*_a):return {"price":10.2,"high":11.0,"open":10.0,"stop_loss_price":9.0}
    async def fetch(*_a):return quote()
    async def policy(*_a,**_k):return {"stop_loss_pct":4}, {"basis":"frozen_entry_order","missing_keys":[]}
    available=AsyncMock(return_value=0 if mode=="t1" else 100)
    monkeypatch.setattr(paper,"_build_short_sell_context",build)
    monkeypatch.setattr(paper,"_spot_by_code",fetch)
    monkeypatch.setattr("app.paper.position_policy.position_exit_policy",policy)
    monkeypatch.setattr(paper,"_short_sell_reason",lambda *_a,**_k:"" if mode=="hold" else "原始止损")
    monkeypatch.setattr(paper,"_available_sell_amount",available)
    monkeypatch.setattr(paper,"_execution_quote_status",lambda *_a,**_k:(mode!="quote","test quote check"))
    monkeypatch.setattr(paper,"_conservative_execution_price",lambda *_:None)
    async with maker() as db:
        account=await paper._get_or_create_account(db,"default")
        pos=PaperPosition(account_id=account.id,code="000001",name="测试",buy_time=AT-timedelta(days=1),
            buy_price=10,buy_amount=100,current_price=10.2,hold_days=1,profit_pct=2,
            is_closed=False,strategy_version="old-v1",stop_loss_price=9.0)
        db.add(pos);await db.commit()
        return account.id,available


@pytest.mark.asyncio
@pytest.mark.parametrize("mode",["hold","t1","quote","depth"])
async def test_real_sell_pass_captures_hold_and_original_blocks_without_extra_checks(paper_client,monkeypatch,mode):
    _,maker=paper_client
    account_id,available=await prepare_scan(maker,monkeypatch,mode)
    token=paper._QUOTE_ROUND_CONTEXT.set(context())
    try:
        async with maker() as db:
            account=await db.get(paper.PaperAccount,account_id)
            logs=await paper._run_auto_sells(db,account=account,run_id="scan",trade_date=AT.date(),
                trigger="fixture",execute=True,quote_now=AT,log_holds=False)
            await db.commit()
            rows=(await db.scalars(select(PaperShadowEvent).where(PaperShadowEvent.route_id==m.ROUTE_ID))).all()
            assert len(rows)==1
            payload=json.loads(rows[0].snapshot_json)
            after=payload["execution_observation"]
            assert payload["pre_execution"]["position"]["strategy_version"]=="old-v1"
            if mode=="hold":
                assert logs==[] and available.await_count==0
                assert after["available_sell_amount"] is None and after["exit_execution_status"]=="not_requested"
            else:
                assert len(logs)==1 and available.await_count==1
                old=json.loads(logs[0].candidate_json)
                assert old["exit_trigger_reason"]=="原始止损"
                assert after["execution_block_code"]==old["execution_block_code"]
                assert after["available_sell_amount"]==(0 if mode=="t1" else 100)
            assert payload["replay_ready"] is False
    finally:paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_fill_mutation_and_audit_failure_do_not_change_original_order(paper_client,monkeypatch):
    _,maker=paper_client
    account_id,available=await prepare_scan(maker,monkeypatch,"fill")
    monkeypatch.setattr(paper,"_conservative_execution_price",lambda *_:10.1)
    monkeypatch.setattr(paper,"_today_sell_stats",AsyncMock(return_value={"amount":0}))
    monkeypatch.setattr(paper,"_has_active_paper_order",AsyncMock(return_value=False))
    monkeypatch.setattr(paper,"_auto_sell_amount",lambda *_a,**_k:100)
    commands=[]
    async def submit(db,command):
        commands.append(command)
        pos=await db.scalar(select(PaperPosition).where(PaperPosition.account_id==account_id))
        pos.buy_amount=0;pos.is_closed=True
        return {"order":{"status":"filled","order_id":"original-order"},"fills":[{"broker_trade_id":"7"}]}
    monkeypatch.setattr("app.trading.service.submit_order",submit)
    token=paper._QUOTE_ROUND_CONTEXT.set(context())
    try:
        async with maker() as db:
            account=await db.get(paper.PaperAccount,account_id)
            logs=await paper._run_auto_sells(db,account=account,run_id="filled",trade_date=AT.date(),
                trigger="fixture",execute=True,quote_now=AT,log_holds=False)
            await db.commit()
            row=await db.scalar(select(PaperShadowEvent).where(PaperShadowEvent.route_id==m.ROUTE_ID))
            payload=json.loads(row.snapshot_json)
            assert payload["pre_execution"]["position"]["buy_amount"]==100
            assert payload["pre_execution"]["position"]["is_closed"] is False
            assert payload["execution_observation"]["exit_execution_status"]=="filled"
            assert payload["execution_observation"]["exit_order_id"]=="original-order"
            assert len(commands)==1 and commands[0].quantity==100 and commands[0].strategy_version=="old-v1"
            assert logs[0].decision=="executed" and str(logs[0].executed_trade_id)=="7"
            # Separate synthetic scenario rejects only audit storage. No real order.
            pos=await db.scalar(select(PaperPosition).where(PaperPosition.account_id==account_id))
            pos.buy_amount=100;pos.is_closed=False;await db.commit()
            conn=await db.connection()
            await conn.execute(text("CREATE TEMP TRIGGER reject_frame BEFORE INSERT ON paper_shadow_event BEGIN SELECT RAISE(ABORT, 'no audit'); END"))
            logs=await paper._run_auto_sells(db,account=account,run_id="audit-fails",trade_date=AT.date(),
                trigger="fixture",execute=True,quote_now=AT,log_holds=False)
            assert logs[0].decision=="executed" and len(commands)==2 and available.await_count==2
            await db.commit()
    finally:paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure",["import","capture","append"])
async def test_optional_observation_failures_do_not_abort_original_exit(paper_client,monkeypatch,caplog,failure):
    import builtins
    _,maker=paper_client
    account_id,available=await prepare_scan(maker,monkeypatch,"t1")
    if failure=="import":
        original=builtins.__import__
        def broken_import(name,*a,**kw):
            if name=="app.paper.position_observation":
                raise ImportError("sensitive detail not logged")
            return original(name,*a,**kw)
        monkeypatch.setattr(builtins,"__import__",broken_import)
    elif failure=="capture":
        def broken_capture(**_kw):raise ValueError("sensitive detail not logged")
        monkeypatch.setattr(m,"capture_position_frame",broken_capture)
    else:
        async def broken_append(*_a):raise RuntimeError("sensitive detail not logged")
        monkeypatch.setattr(m,"append_position_frames",broken_append)
    token=paper._QUOTE_ROUND_CONTEXT.set(context())
    try:
        async with maker() as db:
            account=await db.get(paper.PaperAccount,account_id)
            logs=await paper._run_auto_sells(db,account=account,run_id="optional-fails",trade_date=AT.date(),
                trigger="fixture",execute=True,quote_now=AT,log_holds=False)
            await db.commit()
            assert len(logs)==1 and available.await_count==1
            assert json.loads(logs[0].candidate_json)["execution_block_code"]=="t_plus_one"
            assert (await db.scalars(select(PaperShadowEvent))).all()==[]
            assert "sensitive detail" not in caplog.text
    finally:paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
async def test_dry_run_never_creates_position_events(paper_client,monkeypatch):
    _,maker=paper_client
    account_id,_=await prepare_scan(maker,monkeypatch,"hold")
    token=paper._QUOTE_ROUND_CONTEXT.set(context())
    try:
        async with maker() as db:
            account=await db.get(paper.PaperAccount,account_id)
            await paper._run_auto_sells(db,account=account,run_id="dry",trade_date=AT.date(),
                trigger="fixture",execute=False,quote_now=AT,log_holds=False)
            await db.commit()
            assert (await db.scalars(select(PaperShadowEvent))).all()==[]
    finally:paper._QUOTE_ROUND_CONTEXT.reset(token)
