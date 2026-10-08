"""Fixed TP/expiry are not weak-price evidence; real functions, isolated facts."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from app.api.v1 import paper
from app.paper.account_policy import account_sell_params
from test_exit_decision_hold_clock_20260928 import env  # noqa: F401

SHORT = ("default", "promotion", "mainline", "auction",
         "challenger_b", "challenger_c", "challenger_d")
BEFORE = datetime(2026, 9, 28, 9, 44, 59)
AFTER = datetime(2026, 9, 28, 9, 45)


def context(price=9.99, evidence=0):
    # Synthetic input, not a reconstructed historical trade.
    return dict(price=price, open=price-.01, high=price, avg_price=price-.01,
        ma5=price-.02 if evidence < 1 else price+.02,
        min5_change=.1 if evidence < 2 else -.6,
        orderbook_imbalance=.3, volume_ratio=.5, change_pct=.1,
        prev_was_limit_up=False)


def profile(name="challenger_c"):
    params=account_sell_params(name)
    params.update(open_noise_end="09:45", open_noise_weak_min_evidence=2,
                  open_noise_stop_min_evidence=2, weak_exit_min_evidence=2)
    return params


def decision(*, params=None, profit=-.1, days=1, now=BEFORE, ctx=None):
    params=profile() if params is None else params
    position=SimpleNamespace(buy_price=10., stop_loss_price=8., buy_amount=1000)
    return paper._short_sell_reason(position,
        context(10*(1+profit/100)) if ctx is None else ctx,
        profit_pct=profit, hold_days=days, trade_date=now.date(), now=now, params=params)


@pytest.mark.parametrize("name",SHORT)
@pytest.mark.parametrize("now",(BEFORE,AFTER))
@pytest.mark.parametrize("kind",("take_profit","expiry"))
def test_fixed_contract_survives_zero_weak_evidence(name,now,kind):
    params=profile(name)
    profit=params["take_profit_pct"]+.1 if kind=="take_profit" else -.1
    days=1 if kind=="take_profit" else params["max_hold_days"]
    reason=decision(params=params,profit=profit,days=days,now=now)
    assert reason.startswith("触发短线止盈" if kind=="take_profit" else "持仓")


@pytest.mark.parametrize("delta,expected",[(-.001,False),(0,True),(.001,True)])
def test_take_profit_exact_boundary(delta,expected):
    p=profile()
    assert decision(params=p,profit=p["take_profit_pct"]+delta).startswith("触发短线止盈")==expected


@pytest.mark.parametrize("offset,profit,expected",[(-1,-.1,False),(0,-.1,True),
    (0,0,True),(0,.001,False),(1,-.1,True)])
def test_expiry_age_and_profit_boundary(offset,profit,expected):
    p=profile()
    reason=decision(params=p,days=p["max_hold_days"]+offset,profit=profit)
    assert reason.startswith("持仓")==expected


@pytest.mark.parametrize("evidence",(0,1,2))
@pytest.mark.parametrize("now",(BEFORE,AFTER))
def test_weak_signal_still_needs_its_original_evidence(evidence,now):
    ctx=context(evidence=evidence);ctx["avg_price"]=10.01
    result=decision(now=now,ctx=ctx)
    assert (result.startswith("跌破分时均价"))==(evidence==2)


def test_eligible_weak_rung_keeps_priority_over_expiry():
    p=profile()
    ctx=context(evidence=2);ctx["avg_price"]=10.01
    assert decision(params=p,days=p["max_hold_days"],ctx=ctx).startswith("跌破分时均价")


@pytest.mark.parametrize("severe",(False,True))
def test_original_opening_stop_exception_and_severe_priority_unchanged(severe):
    p=profile()
    profit=-(p["open_severe_stop_loss_pct"]+.1) if severe else -(p["stop_loss_pct"]+.1)
    reason=decision(params=p,profit=profit,days=p["max_hold_days"])
    assert reason.startswith("触发硬止损") if severe else reason==""


def test_prior_limit_risk_stays_ahead_of_fixed_take_profit():
    p=profile();profit=p["take_profit_pct"]+.1
    ctx=context(10*(1+profit/100));ctx.update(prev_was_limit_up=True,
        open_gap_from_prev_close_pct=-5.)
    assert decision(params=p,profit=profit,ctx=ctx).startswith("昨日涨停次日转弱")


@pytest.mark.asyncio
@pytest.mark.parametrize("case",("t1","unfillable","lot"))
async def test_real_exit_orchestration_keeps_execution_gates(env,monkeypatch,case):
    env.name="challenger_c";env.now=BEFORE
    p=account_sell_params(env.name)
    env.price=10*(1+(p["take_profit_pct"]+.1)/100)
    env.position.buy_amount=1000
    env.available=1000
    env.context=context(env.price);env.context.pop("price")
    if case=="t1":
        env.available=0
        env.position.buy_time=BEFORE.replace(hour=9,minute=31)
    elif case=="unfillable":
        monkeypatch.setattr(paper,"_conservative_execution_price",lambda *a:None)
    log=(await env.run())[0]
    assert log.candidate["exit_trigger_reason"].startswith("触发短线止盈")
    if case=="t1":
        assert log.reason.startswith("A股T+1")
        assert log.candidate["execution_block_code"]=="t_plus_one"
    elif case=="unfillable":
        assert log.action=="skip_sell"
        assert log.candidate["execution_block_code"]=="orderbook_unfillable"
    else:
        assert log.action=="sell" and log.decision=="dry_run"
        assert 100<=log.amount<=1000 and log.amount%100==0
        assert log.amount==paper._auto_sell_amount(env.position,1000,
            log.candidate["exit_trigger_reason"],params=p)
