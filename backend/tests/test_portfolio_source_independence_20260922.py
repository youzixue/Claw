"""F4: isolated original-wallet restrictions must not consume shared technical evidence."""
from datetime import timedelta
from unittest.mock import AsyncMock
import json
import pytest
from sqlalchemy import select
from app.api.v1 import paper
from app.config.settings import settings
from app.models.paper import PaperAutoTradeLog, PaperPortfolioSignal
from app.models.stock import StockSpot, StockTag
from app.paper import portfolio_ingress
from test_paper_api import paper_client
from test_paper_buy_point_hooks import clock_and_guards, main_quote

@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("restriction", ["pending", "daily", "icepoint"])
@pytest.mark.parametrize("gate", ["ok", "quote", "continuation", "fund", "time", "market"])
async def test_source_wallet_does_not_swallow_capture(paper_client, monkeypatch, active, restriction, gate):
    _, maker = paper_client
    at, send, submit = clock_and_guards(monkeypatch)
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ENABLED", active)
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ACTIVATION_AT", "2026-09-08T00:00:00")
    source = {"pending":"next_day_plan", "daily":"daily_participation", "icepoint":"icepoint_reversal"}[restriction]
    candidate = dict(code="600888", name="隔离", _source=source, total_score=95,
        change_pct=.5, stop_loss_price=9.2, execution_confirmation=True)
    async def candidates(*args, **kwargs):
        enabled = restriction == "pending" or kwargs["include_" + (
            "daily_participation" if restriction == "daily" else "icepoint_reversal")]
        return ([dict(candidate)] if enabled else []), []
    monkeypatch.setattr(paper, "_paper_auto_buy_candidates", candidates)
    for name in ("_daily_participation_candidates", "_icepoint_reversal_candidates"):
        monkeypatch.setattr(paper, name, AsyncMock(return_value=[dict(candidate)]))
    monkeypatch.setattr(paper, "_paper_main_fund_map", AsyncMock(return_value={}))
    monkeypatch.setattr(paper, "_confirm_candidate_main_fund", AsyncMock(return_value="fund failed" if gate=="fund" else ""))
    monkeypatch.setattr(paper, "_active_paper_order_codes", AsyncMock(
        return_value={"600888"} if restriction == "pending" else set()))
    monkeypatch.setattr(paper, "_champion_intraday_confirmation_status", AsyncMock(return_value=(gate!="quote",3,60)))
    monkeypatch.setattr(paper, "_candidate_execution_value_reject_reason", lambda *a,**k: "")
    monkeypatch.setattr(paper, "_continuation_risk_reject_reason", AsyncMock(return_value="continuation failed" if gate=="continuation" else ""))
    monkeypatch.setattr(paper, "_strategy_buy_limits", lambda name: (0,10))
    monkeypatch.setattr(paper, "_is_daily_participation_time", lambda now: restriction=="daily" and gate!="time")
    monkeypatch.setattr(paper, "_market_allows_daily_participation", lambda x: gate!="market")
    monkeypatch.setattr(paper, "_is_icepoint_reversal_time", lambda now: restriction=="icepoint" and gate!="time")
    monkeypatch.setattr(paper, "_market_is_icepoint_reversal_setup", lambda x: gate!="market")
    monkeypatch.setattr(paper, "_public_order_clock", lambda: at)
    capture = AsyncMock(wraps=portfolio_ingress.capture_confirmed_signal)
    monkeypatch.setattr(portfolio_ingress, "capture_confirmed_signal", capture)
    quote = main_quote(at)
    quote.update(quote_round_id="independent", source_quote_at=at, received_at=at)
    token = paper._QUOTE_ROUND_CONTEXT.set(dict(round_id="independent", as_of_at=at,
        committed_at=at,quality_status="ok",records=[quote],code_version="test"))
    try:
        async with maker() as db:
            account = await paper._get_or_create_account(db, "default")
            db.add_all([StockTag(code="600888",board_type="main_sh",board_tag="tradeable"),StockSpot(**quote)])
            if restriction != "pending":
                db.add(PaperAutoTradeLog(account_id=account.id, run_id="prior", trade_date=at.date(),
                    trigger="test",source="next_day_plan",code="600777",action="buy",
                    decision="executed",reason="prior buy",created_at=at-timedelta(minutes=5)))
            await db.commit()
            await paper.run_paper_auto_trade(db,account_name="default",now=at,execute=True,
                execution_mode="intraday",include_position_risk=False)
            rows=list((await db.scalars(select(PaperAutoTradeLog))).all())
            signals=list((await db.scalars(select(PaperPortfolioSignal))).all())
        expected = active and gate not in {"quote","continuation","fund"} and (restriction=="pending" or gate not in {"time","market"})
        assert capture.await_count == int(expected)
        assert len(signals) == int(expected)
        if signals:
            assert signals[0].origin_account == "default"
            assert signals[0].source == source
        assert not submit.called and not send.called
        assert not any(r.action in {"confirm_buy","buy_signal"} for r in rows)
        if expected:
            assert capture.call_args.kwargs["account"].account_name == "default"
            assert any(r.action=="portfolio_confirm" for r in rows)
        if active and gate in {"fund", "continuation"}:
            assert any(r.action=="portfolio_skip" for r in rows)
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)

@pytest.mark.asyncio
async def test_shared_confirmation_history_cannot_accelerate_independent_account(paper_client, monkeypatch):
    _, maker = paper_client
    at, _, _ = clock_and_guards(monkeypatch)
    policy = paper.account_confirmation_policy("default")
    count = max(int(policy["min_samples"]), 2)
    step = max(int(policy["min_persistence_sec"]) // (count-1) + 1, 1)
    async with maker() as db:
        account = await paper._get_or_create_account(db, "default")
        for index in range(1, count):
            sample = at - timedelta(seconds=step*index)
            db.add(PaperAutoTradeLog(account_id=account.id,run_id="isolated",
                trade_date=at.date(),trigger="test",source="daily_participation",code="600888",
                action="portfolio_confirm",decision="wait",reason="technical observation",
                strategy_version=paper._strategy_version("default"),created_at=sample,
                candidate_json=json.dumps(dict(confirmation_version="champion_persistent_v1",
                    confirmation_sample_at=sample.isoformat(), confirmation_source_quote_at=sample.isoformat()))))
        await db.commit()
        args=dict(account_id=account.id,trade_date=at.date(),code="600888",
            source="daily_participation",current_at=at,account_name="default")
        independent = await paper._champion_intraday_confirmation_status(db,**args)
        shared = await paper._champion_intraday_confirmation_status(db,**args,portfolio_only=True)
        assert independent[0] is False
        assert shared[0] is True

