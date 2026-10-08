"""Healthy source isolation, immutable audit, real service and completion scope."""
from datetime import timedelta
import pytest
from sqlalchemy import select,func
from app.api.v1 import paper
from app.config.settings import settings
from app.models.paper import PaperPortfolioDecision,PaperPortfolioSignal
from app.models.trading import TradeOrder,TradeFill
from app.models.stock import StockTag
from app.paper import portfolio
from app.paper.account_policy import ACCOUNT_NAMES
from test_paper_deferred_exit_provenance import memory_session
from test_portfolio_dispatch_20260922 import execution_fixture,ALL
from test_portfolio_provenance_20260922 import captured,AT,active
from test_portfolio_wallet_20260922 import cash_wallet
from test_portfolio_execution_20260922 import real_execution,quote_execution_env,frame,accept

@pytest.mark.asyncio
async def test_failed_backlog_does_not_starve_healthy_origin(memory_session, execution_fixture):
    db = memory_session
    await cash_wallet(db)
    original, _, _ = await captured(db, "auction")
    fields = {column.name: getattr(original, column.name)
              for column in PaperPortfolioSignal.__table__.columns if column.name != "id"}
    for index in range(129):
        db.add(PaperPortfolioSignal(**{**fields, "signal_key": f"failed-backlog-{index}"}))
    await db.commit()
    await captured(db, "promotion")
    source = receipts()
    source["auction"]["status"] = "failed"
    result = await portfolio.run_shared_portfolio(db, source_receipts=source, manage_positions=False)
    assert result["allocated"] == 1 and result["waiting"] == 128
    assert (await db.scalars(select(TradeOrder))).one().source == "promotion_promotion"
    assert await db.scalar(select(func.count(PaperPortfolioSignal.id))) == 131
    assert await db.scalar(select(PaperPortfolioDecision.id).where(
        PaperPortfolioDecision.decision == "superseded")) is None


def receipts(round_id="allocation-round"):
    return {name:{"status":"completed","quote_round_id":round_id,
                  "source_scan_status":"completed"} for name in ACCOUNT_NAMES}

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["failed","degraded","not_scanned","skipped","missing","wrong_round","legacy_bool","scan_failed"])
async def test_d_failure_does_not_block_healthy_b_and_failure_is_audited(
        memory_session,execution_fixture,bad):
    db=memory_session
    await cash_wallet(db)
    await captured(db,"auction")
    await captured(db,"promotion")
    source=receipts()
    if bad=="missing":
        source.pop("auction")
    elif bad=="wrong_round":
        source["auction"]["quote_round_id"]="old"
    elif bad=="legacy_bool":
        source["auction"]=True
    elif bad=="scan_failed":
        source["auction"]["source_scan_status"]="failed"
    else:
        source["auction"]["status"]=bad
    result=await portfolio.run_shared_portfolio(db,source_receipts=source,manage_positions=False)
    assert result["status"]=="degraded" and result["allocated"]==1
    orders=(await db.scalars(select(TradeOrder))).all()
    assert len(orders)==1 and orders[0].source=="promotion_promotion"
    decisions=(await db.execute(select(PaperPortfolioSignal.origin_account,PaperPortfolioDecision.decision)
        .join(PaperPortfolioDecision,PaperPortfolioDecision.signal_id==PaperPortfolioSignal.id))).all()
    assert ("auction","source_wait") in decisions and ("promotion","submitted") in decisions
    assert any(row["origin_account"]=="auction" for row in result["source_failures"])
    assert await db.scalar(select(func.count(TradeFill.id)))==0

@pytest.mark.asyncio
@pytest.mark.parametrize("kind",["all_failed","missing_all","wrong_round"])
async def test_no_current_healthy_origin_never_submits(memory_session,execution_fixture,kind):
    db=memory_session
    await cash_wallet(db)
    await captured(db,"promotion")
    source=receipts("old" if kind=="wrong_round" else "allocation-round")
    if kind=="all_failed":
        for row in source.values(): row["status"]="failed"
    elif kind=="missing_all":
        source={}
    result=await portfolio.run_shared_portfolio(db,source_receipts=source,manage_positions=False)
    assert result["allocated"]==0 and result["status"]=="degraded"
    assert await db.scalar(select(TradeOrder.id)) is None
    assert (await db.scalar(select(PaperPortfolioDecision))).decision=="source_wait"

@pytest.mark.asyncio
async def test_completed_empty_is_legal_not_an_execution_failure(memory_session,execution_fixture):
    await cash_wallet(memory_session)
    result=await portfolio.run_shared_portfolio(memory_session,source_receipts=receipts(),manage_positions=False)
    assert result["status"]=="completed" and result["allocated"]==0
    assert result["source_failures"]==[]

@pytest.mark.asyncio
async def test_failed_origin_old_outbox_expires_without_erasing_wait_sample(memory_session,execution_fixture):
    db=memory_session
    await cash_wallet(db)
    signal,_,_=await captured(db,"auction")
    source=receipts()
    source["auction"]["status"]="failed"
    await portfolio.run_shared_portfolio(db,source_receipts=source,manage_positions=False)
    execution_fixture[0][0]=AT+timedelta(seconds=121)
    execution_fixture[1]["round_id"]="next"
    source=receipts("next")
    source["auction"]["status"]="failed"
    await portfolio.run_shared_portfolio(db,source_receipts=source,manage_positions=False)
    rows=(await db.scalars(select(PaperPortfolioDecision).where(PaperPortfolioDecision.signal_id==signal.id)
        .order_by(PaperPortfolioDecision.id))).all()
    assert [row.decision for row in rows]==["source_wait","expired"]
    assert await db.scalar(select(TradeOrder.id)) is None

@pytest.mark.asyncio
@pytest.mark.parametrize("gate",["auto","intraday","allow_entries"])
async def test_healthy_source_does_not_bypass_global_execution_switch(memory_session,execution_fixture,monkeypatch,gate):
    db=memory_session
    await cash_wallet(db)
    await captured(db,"promotion")
    if gate!="allow_entries":
        monkeypatch.setattr(settings,"PAPER_AUTO_TRADE_ENABLED" if gate=="auto" else "PAPER_INTRADAY_AUTO_TRADE_ENABLED",False)
    result=await portfolio.run_shared_portfolio(db,source_receipts=receipts(),
        allow_entries=gate!="allow_entries",manage_positions=False)
    assert result["allocated"]==0
    assert await db.scalar(select(TradeOrder.id)) is None

@pytest.mark.asyncio
async def test_real_healthy_c2_service_submission_survives_d_failure(quote_execution_env,real_execution):
    clock=real_execution
    async with quote_execution_env() as db:
        db.add(StockTag(code="600001",name="fixture",board_type="main_sh",board_tag="tradeable",
            is_st=False,is_suspended=False,is_delisting=False,is_ipo_recent=False))
        await db.commit()
        await captured(db,"auction")
        healthy,_,_=await captured(db,"challenger_c")
        payload=frame(clock[0],"allocation-round")
        await accept(db,payload)
        source=receipts()
        source["auction"]["status"]="failed"
        token=paper._QUOTE_ROUND_CONTEXT.set(payload)
        try:
            result=await portfolio.run_shared_portfolio(db,source_receipts=source,manage_positions=False)
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)
        assert result["allocated"]==1 and result["status"]=="degraded"
        order=(await db.scalars(select(TradeOrder))).one()
        assert order.source==healthy.source and order.quantity==900 and order.status=="submitted"
        assert await db.scalar(select(func.count(TradeFill.id)))==0
