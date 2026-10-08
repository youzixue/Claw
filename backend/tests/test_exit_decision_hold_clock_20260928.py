"""Real exit orchestration + calendar, synthetic in-memory facts, no ledger writes."""
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.api.v1 import paper
from app.core import trade_calendar as calendar_module
from app.db import session as session_module
from app.models.governance import TradeCalendarModel
from app.paper import exit_audit, position_policy, position_observation

AT = datetime(2026, 9, 28, 10)
ACCOUNTS = ("default", "promotion", "mainline", "auction", "tenbagger", "reversal",
            "challenger_a", "challenger_b", "challenger_c", "challenger_d",
            "challenger_e", "challenger_f2")
MIDLINE = ("challenger_a", "tenbagger", "challenger_e", "reversal", "challenger_f2")


def buy_time_for_days(days):
    # Independent finite fixture calendar, matching the facts seeded below.
    prior = [AT.date()-timedelta(days=i) for i in range(100)
             if (AT.date()-timedelta(days=i)).weekday() < 5
             and not calendar_module.is_official_closed_day(AT.date()-timedelta(days=i))]
    return datetime.combine(prior[days], AT.time())


@pytest_asyncio.fixture
async def env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(TradeCalendarModel.__table__.create)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        day = date(2026, 1, 1)
        while day.year == 2026:
            db.add(TradeCalendarModel(trade_date=day,
                is_trade_day=day.weekday() < 5 and not calendar_module.is_official_closed_day(day)))
            day += timedelta(days=1)
        await db.commit()
    monkeypatch.setattr(session_module, "async_session", maker)
    monkeypatch.setattr(calendar_module, "async_session", maker)
    monkeypatch.setattr(paper.trade_calendar, "_cache", {})
    async def no_network(*args, **kwargs):
        raise AssertionError("calendar sync must never be used")
    monkeypatch.setattr(paper.trade_calendar, "_sync_from_source", no_network)
    state = NS(now=AT, name="challenger_e", price=9.99, available=200,
        context={}, calls=0, logs=[], position=NS(id=1,account_id=1,code="600001",
            name="isolated",buy_time=datetime(2026,9,23,10),buy_price=10.,
            current_price=9.99,profit_pct=-.1,hold_days=99,buy_amount=200,
            stop_loss_price=9.,strategy_version="frozen",is_closed=False))
    monkeypatch.setattr(paper,"_paper_now",lambda:state.now)
    async def open_positions(*args): return getattr(state, "positions", [state.position])
    monkeypatch.setattr(paper,"_open_positions",open_positions)
    async def noop(*args,**kwargs): return None
    monkeypatch.setattr(paper,"_refresh_expired_paper_rows",noop)
    async def forbidden(*args,**kwargs): raise AssertionError("must not refresh account/NAV")
    monkeypatch.setattr(paper,"_refresh_account",forbidden)
    async def policy(db,**kwargs):
        return dict(kwargs["defaults"]),{"basis":"frozen_entry_order"}
    monkeypatch.setattr(position_policy,"position_exit_policy",policy)
    async def context(*args):
        return dict(price=state.price,**state.context)
    monkeypatch.setattr(paper,"_build_short_sell_context",context)
    async def spot(*args): return NS(price=state.price,updated_at=state.now)
    monkeypatch.setattr(paper,"_spot_by_code",spot)
    monkeypatch.setattr(paper,"_execution_quote_status",lambda *a,**k:(True,"ok"))
    async def extrema(*a,**k): return {"coverage":"test"}
    monkeypatch.setattr(exit_audit,"observe_position_extrema",extrema)
    monkeypatch.setattr(exit_audit,"attach_exit_audit",lambda *a,**k:None)
    monkeypatch.setattr(position_observation,"capture_position_frame",lambda **k:None)
    monkeypatch.setattr(position_observation,"append_position_frames",noop)
    monkeypatch.setattr(position_observation,"append_deferred_execution_frames",noop)
    async def available(*a): return state.available
    monkeypatch.setattr(paper,"_available_sell_amount",available)
    monkeypatch.setattr(paper,"_conservative_execution_price",lambda *a:state.price)
    async def stats(*a): return {"amount":0}
    monkeypatch.setattr(paper,"_today_sell_stats",stats)
    async def log(*a,**kwargs):
        item=NS(**kwargs);state.logs.append(item);return item
    monkeypatch.setattr(paper,"_add_auto_log",log)
    monkeypatch.setattr(paper,"_log_t1_skip_once",log)
    original=paper._trade_day_hold_days
    async def count(*args):
        state.calls+=1
        return await original(*args)
    monkeypatch.setattr(paper,"_trade_day_hold_days",count)
    @asynccontextmanager
    async def nested(): yield
    state.db=NS(begin_nested=nested)
    async def run(**kwargs):
        state.logs.clear()
        return await paper._run_auto_sells(state.db,account=NS(id=1,account_name=state.name),
            run_id="isolated-clock",trade_date=state.now.date(),trigger="test",
            execute=kwargs.pop("execute", False),quote_now=state.now,**kwargs)
    state.run=run
    state.maker=maker
    yield state
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("stale",[0,1,99,None])
async def test_holiday_and_stale_cache_use_decision_day(env,stale):
    env.position.hold_days=stale
    logs=await env.run()
    audit=logs[0].candidate["exit_hold_clock"]
    assert audit["status"]=="known" and audit["hold_days"]==2 # Sep24 + Sep28, not Sep25 holiday
    assert logs[0].action=="hold"
    assert env.position.hold_days is stale
    assert env.calls==1


@pytest.mark.asyncio
async def test_expiry_uses_original_buy_not_refresh_time(env):
    env.position.buy_time=datetime(2026,9,22,10)
    env.position.hold_days=0
    logs=await env.run()
    assert logs[0].candidate["exit_hold_clock"]["hold_days"]==3
    assert "到期平仓" in logs[0].reason
    assert logs[0].action=="sell" and logs[0].decision=="dry_run"
    assert env.position.hold_days==0


@pytest.mark.asyncio
async def test_first_sellable_day_short_rung(env):
    env.name="challenger_c"
    env.position.buy_time=datetime(2026,9,24,10)
    env.position.hold_days=0
    env.context=dict(open=10.01,avg_price=10.02,ma5=10.03,min5_change=-.6,high=10.)
    logs=await env.run()
    assert logs[0].candidate["exit_hold_clock"]["hold_days"]==1
    assert "跌破分时均价" in logs[0].reason
    assert env.position.hold_days==0


@pytest.mark.asyncio
async def test_same_day_zero_is_known_but_t1_remains(env):
    env.position.buy_time=AT.replace(hour=9)
    env.position.hold_days=999
    env.price=8.9;env.available=0
    logs=await env.run()
    assert logs[0].candidate["exit_hold_clock"]["hold_days"]==0
    assert logs[0].candidate["exit_hold_clock"]["status"]=="known"
    assert logs[0].reason.startswith("A股T+1")
    assert "止损" in logs[0].candidate["exit_trigger_reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name",ACCOUNTS)
async def test_calendar_exception_preserves_original_hard_stop(env,monkeypatch,name):
    env.name=name;env.price=8.9
    async def broken(*a): raise RuntimeError("calendar unavailable")
    monkeypatch.setattr(paper,"_trade_day_hold_days",broken)
    logs=await env.run(log_holds=False)
    assert logs[0].candidate["exit_hold_clock"]["status"]=="unknown"
    assert logs[0].candidate["exit_hold_clock"]["hold_days"] is None
    assert "止损" in logs[0].reason and logs[0].action=="sell"


@pytest.mark.asyncio
async def test_missing_calendar_never_invents_expiry_or_zero(env):
    from sqlalchemy import delete
    async with env.maker() as db:
        await db.execute(delete(TradeCalendarModel).where(TradeCalendarModel.trade_date==date(2026,9,24)))
        await db.commit()
    env.position.buy_time=datetime(2026,9,1,10)
    env.position.hold_days=99
    logs=await env.run(log_holds=False)
    assert len(logs)==1
    assert logs[0].candidate["exit_hold_clock"]["status"]=="unknown"
    assert logs[0].candidate["exit_hold_clock"]["hold_days"] is None
    assert logs[0].action=="hold" and "未知" in logs[0].reason
    assert env.calls==0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad",[None,True,-1,1.5])
async def test_invalid_calendar_count_is_unknown(env,monkeypatch,bad):
    async def invalid(*a): return bad
    monkeypatch.setattr(paper,"_trade_day_hold_days",invalid)
    logs=await env.run(log_holds=False)
    assert logs[0].candidate["exit_hold_clock"]["hold_days"] is None
    assert logs[0].action=="hold"


@pytest.mark.asyncio
async def test_missing_calendar_preserves_forced_exit(env):
    env.position.buy_time=None
    logs=await env.run(forced_exit_reason_by_code={"600001":"失效版本隔离退出"})
    assert logs[0].reason=="失效版本隔离退出"
    assert logs[0].candidate["exit_hold_clock"]["status"]=="unknown"


@pytest.mark.asyncio
async def test_future_buy_clock_cannot_be_known_zero(env):
    env.position.buy_time=AT+timedelta(days=1)
    logs=await env.run(log_holds=False)
    assert logs[0].candidate["exit_hold_clock"]["hold_days"] is None
    assert "未知" in logs[0].reason


@pytest.mark.asyncio
async def test_sparse_year_blocks_sync_but_not_hard_stop(env):
    from sqlalchemy import delete
    async with env.maker() as db:
        await db.execute(delete(TradeCalendarModel))
        await db.commit()
    env.price=8.9
    logs=await env.run()
    assert logs[0].candidate["exit_hold_clock"]["reason_code"]=="calendar_year_unavailable"
    assert logs[0].candidate["exit_hold_clock"]["hold_days"] is None
    assert "止损" in logs[0].reason and env.calls==0


@pytest.mark.asyncio
async def test_unknown_day_cannot_enable_short_age_rung(env,monkeypatch):
    env.name="challenger_c"
    env.context=dict(open=10.01,avg_price=10.02,ma5=10.03,min5_change=-.6,high=10.)
    async def broken(*a): raise RuntimeError("calendar unavailable")
    monkeypatch.setattr(paper,"_trade_day_hold_days",broken)
    logs=await env.run(log_holds=False)
    assert logs[0].action=="hold"
    assert logs[0].candidate["exit_trigger_reason"]==""
    assert logs[0].candidate["exit_hold_clock"]["hold_days"] is None


@pytest.mark.asyncio
async def test_original_grace_is_preserved(env):
    env.price=10.1
    env.position.buy_time=datetime(2026,9,22,10)
    logs=await env.run()
    assert logs[0].candidate["exit_hold_clock"]["hold_days"]==3
    assert logs[0].candidate["exit_parameters"]["expiry_grace_days"]==paper.settings.PAPER_MIDLINE_EXPIRY_GRACE_DAYS
    assert logs[0].action=="hold"


@pytest.mark.asyncio
@pytest.mark.parametrize("name",ACCOUNTS)
@pytest.mark.parametrize("offset",[-1,0])
async def test_twelve_accounts_loss_expiry_boundary(env,name,offset):
    env.name=name
    params=paper._strategy_sell_params(NS(account_name=name))
    days=params["max_hold_days"]+offset
    env.position.buy_time=buy_time_for_days(days)
    env.position.hold_days=0 if offset==0 else 999
    logs=await env.run()
    assert logs[0].candidate["exit_hold_clock"]["hold_days"]==days
    assert logs[0].candidate["exit_parameters"]==params
    assert logs[0].action==("sell" if offset==0 else "hold")
    if offset==0:
        assert ("到期平仓" if name in MIDLINE else "时间止损") in logs[0].reason
    assert env.position.hold_days==(0 if offset==0 else 999)


@pytest.mark.asyncio
@pytest.mark.parametrize("name",MIDLINE)
@pytest.mark.parametrize("offset",[-1,0])
async def test_midline_expiry_grace_end_not_calendar_days(env,name,offset):
    env.name=name
    params=paper._strategy_sell_params(NS(account_name=name))
    days=params["max_hold_days"]+params["expiry_grace_days"]+offset
    env.position.buy_time=buy_time_for_days(days)
    env.position.hold_days=0 if offset==0 else 999
    env.price=10.1
    logs=await env.run()
    assert logs[0].candidate["exit_hold_clock"]["hold_days"]==days
    assert logs[0].candidate["exit_parameters"]==params
    assert logs[0].action==("sell" if offset==0 else "hold")
    if offset==0:
        assert "到期平仓" in logs[0].reason and "宽限" in logs[0].reason


@pytest.mark.asyncio
@pytest.mark.parametrize("same_day",[True,False])
async def test_same_buy_date_reuses_one_calculation_only_within_pass(env,same_day):
    other=NS(**vars(env.position))
    other.id=2;other.code="600002";other.hold_days=777
    other.buy_time=(env.position.buy_time+timedelta(hours=1) if same_day
                    else datetime(2026,9,24,10))
    env.positions=[env.position,other]
    logs=await env.run()
    assert len(logs)==2
    assert [x.candidate["exit_hold_clock"]["hold_days"] for x in logs]==[2,2 if same_day else 1]
    assert env.calls==(1 if same_day else 2)
    assert env.position.hold_days==99 and other.hold_days==777
    await env.run()
    assert env.calls==(2 if same_day else 4) # no cross-pass stale clock cache


@pytest.mark.asyncio
async def test_scale_in_inventory_cannot_restart_exit_clock(env):
    # Exit-boundary integration: emulate already-booked added inventory; this
    # does not claim to test the separate ledger booking transaction.
    env.position.buy_time=buy_time_for_days(3)
    original_buy_time=env.position.buy_time
    env.position.hold_days=0
    before=(await env.run())[0]
    env.position.buy_amount+=100
    env.position.buy_price=10.01
    after=(await env.run())[0]
    assert before.candidate["exit_hold_clock"]["hold_days"]==3
    assert after.candidate["exit_hold_clock"]["hold_days"]==3
    assert before.action==after.action=="sell"
    assert "到期平仓" in before.reason and "到期平仓" in after.reason
    assert env.position.buy_time==original_buy_time
    assert env.position.hold_days==0


def overlapping_weak_context(env):
    env.name="default";env.price=12.02;env.available=600
    env.position.buy_price=12.23;env.position.stop_loss_price=11.62
    env.position.buy_amount=600;env.position.buy_time=datetime(2026,9,24,10)
    env.context=dict(open=12.05,high=12.1,low=12.02,avg_price=12.09,ma5=12.,
                     change_pct=-3.,volume_ratio=2.,min5_change=-1.,orderbook_imbalance=-.2)


async def unavailable_clock(*args):
    raise RuntimeError("calendar unavailable")


def track_upgrade(monkeypatch):
    from app.trading import service
    calls={"cancel":0,"submit":0}
    async def active(*args,**kwargs): return True
    async def cancel(*args,**kwargs):
        calls["cancel"]+=1
        return {"status":"canceled","canceled_remaining_quantity":100}
    async def submit(*args,**kwargs):
        calls["submit"]+=1
        return {"order":{"status":"submitted","order_id":"isolated-new"},"fills":[]}
    monkeypatch.setattr(paper,"_has_active_paper_order",active)
    monkeypatch.setattr(service,"cancel_reduction_for_protective_exit",cancel)
    monkeypatch.setattr(service,"submit_order",submit)
    monkeypatch.setattr(position_observation,"capture_sell_execution_frame",lambda **k:None)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("tail",["volume","sector"])
async def test_unknown_age_cannot_upgrade_masked_volume_exit(env,monkeypatch,tail):
    overlapping_weak_context(env)
    if tail=="sector":
        env.context.update(volume_ratio=.8,ma5=12.1,sector_retreat_reason="板块退潮：隔离测试")
    known=(await env.run())[0]
    assert known.candidate["exit_hold_clock"]["hold_days"]==1
    assert "跌破分时均价" in known.reason
    assert known.amount==100 < env.position.buy_amount
    assert not paper._is_full_exit_reason(known.candidate["exit_trigger_reason"])
    calls=track_upgrade(monkeypatch)
    await env.run(execute=True)
    assert calls=={"cancel":0,"submit":0}
    monkeypatch.setattr(paper,"_trade_day_hold_days",unavailable_clock)
    unknown=(await env.run(execute=True,log_holds=False))[0]
    assert calls=={"cancel":0,"submit":0}
    assert unknown.action=="hold"
    assert unknown.candidate["exit_trigger_reason"]==""
    clock=unknown.candidate["exit_hold_clock"]
    assert clock["hold_days"] is None and clock["status"]=="unknown"
    assert clock["priority_status"]=="ambiguous"
    assert clock["suppressed_reason"].startswith("放量阴线" if tail=="volume" else "板块退潮")


@pytest.mark.asyncio
async def test_unknown_age_hard_stop_still_upgrades(env,monkeypatch):
    overlapping_weak_context(env);env.price=11.3
    monkeypatch.setattr(paper,"_trade_day_hold_days",unavailable_clock)
    calls=track_upgrade(monkeypatch)
    logs=await env.run(execute=True)
    assert calls=={"cancel":1,"submit":1}
    assert any("止损" in item.reason for item in logs)
    assert logs[-1].candidate["exit_hold_clock"]["hold_days"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind",["volume","take_profit","forced"])
async def test_unknown_age_does_not_blanket_disable_age_independent_exit(env,monkeypatch,kind):
    overlapping_weak_context(env)
    monkeypatch.setattr(paper,"_trade_day_hold_days",unavailable_clock)
    kwargs={}
    if kind=="volume":
        env.context.update(ma5=11.7,avg_price=11.8,min5_change=0.,orderbook_imbalance=.3)
        expected="放量阴线"
    elif kind=="take_profit":
        env.price=13.0
        env.context.update(open=12.8,high=13.,avg_price=12.9,ma5=12.,min5_change=.2,change_pct=5.)
        expected="触发短线止盈"
    else:
        kwargs["forced_exit_reason_by_code"]={"600001":"失效版本隔离退出"}
        expected="失效版本隔离退出"
    log=(await env.run(**kwargs))[0]
    assert log.action=="sell" and expected in log.reason
    assert log.candidate["exit_hold_clock"]["hold_days"] is None


@pytest.mark.asyncio
async def test_unknown_age_cannot_create_masked_full_order_without_pending_order(env,monkeypatch):
    overlapping_weak_context(env)
    calls=track_upgrade(monkeypatch)
    async def no_order(*args,**kwargs): return False
    monkeypatch.setattr(paper,"_has_active_paper_order",no_order)
    monkeypatch.setattr(paper,"_trade_day_hold_days",unavailable_clock)
    logs=await env.run(execute=True,log_holds=False)
    assert logs[0].action=="hold"
    assert calls=={"cancel":0,"submit":0}


def test_priority_probe_matches_current_short_age_predicate_contract():
    # Future ladder changes must revalidate the probe, not silently treat 1 as
    # a universal substitute for unknown age.
    import ast
    import inspect
    tree=ast.parse(inspect.getsource(paper._short_sell_reason))
    comparisons=sorted((n for n in ast.walk(tree) if isinstance(n,ast.Compare)
        and isinstance(n.left,ast.Name) and n.left.id=="hold_days"),key=lambda n:n.lineno)
    assert all(len(n.ops)==1 and isinstance(n.ops[0],ast.GtE) for n in comparisons)
    keys=[n.comparators[0].value if isinstance(n.comparators[0],ast.Constant)
          else n.comparators[0].id for n in comparisons]
    assert keys==["max_hold_days"]+[1]*6+["max_hold_days"]
    function=tree.body[0]
    # EX2's extra expiry is confined to a closed opening-noise branch. With
    # age=-1 this branch returns TP or empty; it cannot fall through to soft rungs.
    opening=next(n for n in function.body if isinstance(n,ast.If)
                 and comparisons[0] in list(ast.walk(n)))
    assert not any(isinstance(n,ast.Name) and n.id=="hold_days" for n in ast.walk(opening.test))
    assert isinstance(opening.body[-1],ast.Return) and opening.body[-1].value.value==""
    assert len(opening.body)==3 and all(isinstance(n,ast.If) for n in opening.body[:2])
    assert ast.unparse(opening.body[0].test)=="profit_pct >= take_profit_pct"
    assert comparisons[0] in list(ast.walk(opening.body[1].test))
    assert comparisons[-1] in list(ast.walk(function.body[-2]))
    assert isinstance(function.body[-1],ast.Return) and function.body[-1].value.value==""


@pytest.mark.asyncio
@pytest.mark.parametrize("take_profit",[False,True])
async def test_unknown_age_opening_fixed_contract_scope(env,monkeypatch,take_profit):
    env.name="default";env.now=AT.replace(hour=9,minute=44,second=59)
    env.position.hold_days=999
    monkeypatch.setattr(paper,"_trade_day_hold_days",unavailable_clock)
    if take_profit:
        env.price=10.56
    log=(await env.run(log_holds=False))[0]
    assert log.candidate["exit_hold_clock"]["hold_days"] is None
    if take_profit:
        assert log.action=="sell" and "触发短线止盈" in log.reason
        assert log.candidate["exit_hold_clock"]["priority_status"]=="age_invariant"
    else:
        assert log.action=="hold" and log.candidate["exit_trigger_reason"]==""
    assert env.position.hold_days==999
