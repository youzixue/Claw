"""Confirmed outbox -> real submitted paper order; no forged fills or live I/O."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from paper_pending_fixture import accepted_frame
from sqlalchemy import select

from app.api.v1 import paper
from app.config.settings import settings
from app.models.paper import PaperPortfolioDecision, PaperTradeLog
from app.models.trading import TradeOrder, TradeFill
from app.paper import portfolio
from app.paper.account_policy import ACCOUNT_NAMES
from app.trading import service
from test_portfolio_provenance_20260922 import active, captured, AT
from test_portfolio_wallet_20260922 import cash_wallet
from test_paper_deferred_exit_provenance import memory_session

ALL = {name: {"status": "completed", "quote_round_id": "allocation-round",
              "source_scan_status": "completed"} for name in ACCOUNT_NAMES}


@pytest_asyncio.fixture
async def execution_fixture(monkeypatch, active, memory_session):
    clock = [AT+timedelta(seconds=3)]
    context = {"round_id": "allocation-round", "as_of_at": AT,
               "committed_at": AT, "quality_status": "ok",
               "config_version": "fixture-config", "code_version": "fixture-code",
               "records": [{"code": "600001"}]}
    await accepted_frame(memory_session, context)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: clock[0])
    monkeypatch.setattr(paper, "_paper_now", lambda: clock[0])
    monkeypatch.setattr(paper, "_quote_round_context", lambda: context)
    monkeypatch.setattr(paper, "_paper_order_window_status", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(paper, "_spot_by_code", AsyncMock(return_value=SimpleNamespace(price=10.)))
    monkeypatch.setattr(paper, "_execution_quote_status", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr(paper, "_pending_primary_buy_confirmation", AsyncMock(return_value=("valid", "")))
    monkeypatch.setattr(paper, "_resolve_candidate_entry_sector", AsyncMock(return_value={}))
    monkeypatch.setattr(service, "_pre_trade_risk_check", AsyncMock(return_value={
        "final_level": "pass", "warnings": [], "block_reasons": []}))
    monkeypatch.setattr(settings, "PAPER_AUTO_TRADE_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_INTRADAY_AUTO_TRADE_ENABLED", True)
    # Pre-trade/rule units are tested separately; this fixture covers the real
    # order commit, its immutable source contract and crash/audit reconciliation.
    monkeypatch.setattr(service, "experiment_active", lambda *_a, **_kw: False)
    return clock, context


@pytest.mark.asyncio
async def test_forward_submission_keeps_cash_and_does_not_claim_fill(memory_session, execution_fixture):
    db = memory_session
    account = await cash_wallet(db)
    signal, _, _ = await captured(db, "promotion")
    result = await portfolio.run_shared_portfolio(db, source_receipts=ALL, manage_positions=False)
    assert result["status"] == "completed" and result["allocated"] == 1
    order = await db.scalar(select(TradeOrder))
    assert order.status == "submitted" and order.account_id == "shared_50k"
    assert order.source == signal.source and order.signal_id == signal.source_signal_id
    contract = json.loads(order.risk_json)["paper_deferred_order"]["buy_validity"]
    assert contract["origin_account"] == "promotion" and contract["confirmed_at"] == AT.isoformat()
    assert (await db.scalar(select(PaperPortfolioDecision))).order_id == order.order_id
    assert account.current_capital == 50000
    assert await db.scalar(select(PaperTradeLog.id)) is None
    assert await db.scalar(select(TradeFill.id)) is None
    again = await portfolio.run_shared_portfolio(db, source_receipts=ALL, manage_positions=False)
    assert again["allocated"] == 0
    assert len((await db.scalars(select(TradeOrder))).all()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("receipts", [None, {}, {**ALL, "promotion": {"status": "failed", "quote_round_id": "allocation-round"}}])
async def test_source_failure_never_becomes_successful_allocation(memory_session, execution_fixture, receipts):
    db = memory_session
    await cash_wallet(db)
    await captured(db, "promotion")
    result = await portfolio.run_shared_portfolio(db, source_receipts=receipts, manage_positions=False)
    assert result["status"] == "degraded" and result["allocated"] == 0
    assert await db.scalar(select(TradeOrder.id)) is None
    audit = await db.scalar(select(PaperPortfolioDecision))
    assert audit.decision == "source_wait" and "source_" in audit.reason_code


@pytest.mark.asyncio
async def test_expired_sample_kept_without_order(memory_session, execution_fixture):
    db = memory_session
    clock, _ = execution_fixture
    await cash_wallet(db)
    await captured(db, "promotion")
    clock[0] = AT+timedelta(seconds=121)
    result = await portfolio.run_shared_portfolio(db, source_receipts=ALL, manage_positions=False)
    assert result["rejected"] == 1
    assert (await db.scalar(select(PaperPortfolioDecision))).decision == "expired"
    assert await db.scalar(select(TradeOrder.id)) is None


@pytest.mark.asyncio
async def test_commit_before_receipt_failure_reconciles_original_order(
    memory_session, execution_fixture, monkeypatch,
):
    db = memory_session
    await cash_wallet(db)
    await captured(db, "promotion")
    original = service.submit_order
    async def uncertain(session, cmd):
        await original(session, cmd)
        raise RuntimeError("fixture lost return after commit")
    monkeypatch.setattr(service, "submit_order", uncertain)
    with pytest.raises(RuntimeError, match="lost return"):
        await portfolio.run_shared_portfolio(db, source_receipts=ALL, manage_positions=False)
    assert len((await db.scalars(select(TradeOrder))).all()) == 1
    assert await db.scalar(select(PaperPortfolioDecision.id)) is None
    monkeypatch.setattr(service, "submit_order", original)
    execution_fixture[0][0] = AT+timedelta(seconds=4)
    execution_fixture[1]["round_id"] = "recovery-round"
    result = await portfolio.run_shared_portfolio(db, source_receipts=ALL, manage_positions=False)
    assert result["allocated"] == 0
    assert len((await db.scalars(select(TradeOrder))).all()) == 1
    assert (await db.scalar(select(PaperPortfolioDecision))).reason_code == "recovered_committed_order"


@pytest.mark.asyncio
async def test_disabled_portfolio_has_no_wallet_or_order_io(monkeypatch):
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ENABLED", False)
    db = AsyncMock()
    assert (await portfolio.run_shared_portfolio(db))["status"] == "disabled"
    assert not db.mock_calls
