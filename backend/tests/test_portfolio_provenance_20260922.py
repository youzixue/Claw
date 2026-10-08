"""Origin-policy vs cash-wallet regressions; temporary SQLite only."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.v1 import paper, trading
from app.config.settings import settings
from app.models.paper import PaperAccount, PaperShadowEvent
from app.paper import portfolio_ingress as ingress, portfolio_provenance as provenance
from app.paper import strategy_iteration_challenger as challenger
from app.paper.account_policy import ACCOUNT_NAMES, ROUTE_ACCOUNT_NAMES
from app.paper.portfolio_contract import PORTFOLIO_ACCOUNT, entry_version
from app.trading import service
from test_paper_deferred_exit_provenance import memory_session

AT = datetime(2026, 9, 22, 13, 30)


@pytest.fixture
def active(monkeypatch):
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ACTIVATION_AT", "2026-09-22 13:00:00")


async def captured(db, name="default"):
    account = PaperAccount(account_name=name, initial_capital=50000,
                           current_capital=0, total_assets=50000, status="active")
    db.add(account)
    await db.flush()
    route = next((r for r, a in ROUTE_ACCOUNT_NAMES.items() if a == name), "")
    source = route or {
        "default": "next_day_plan", "promotion": "promotion_promotion",
        "mainline": "promotion_mainline", "auction": "promotion_auction",
        "tenbagger": "tenbagger_midline", "challenger_e": "tenbagger_midline",
        "reversal": "reversal_pullback",
    }[name]
    event_key = "event-"+name if route else ""
    source_id = challenger._signal_token(event_key, route) if route else "original-primary-"+name
    candidate = {"code": "600001", "_source": source}
    if route:
        event = PaperShadowEvent(event_key=event_key, route_id=route,
            route_version=challenger._route_shadow_version(route), trade_date=AT.date(),
            observed_at=AT, created_at=AT, code="600001", name="fixture",
            event_type="confirmed", status="confirmed", price=10, snapshot_json="{}")
        db.add(event)
        await db.flush()
        candidate = {**challenger._event_candidate(event, {}), "code": event.code}
        candidate["_source"] = route
    row = await ingress.capture_confirmed_signal(
        db, account=account, source=source, candidate=candidate,
        source_signal_id=source_id, confirmed_at=AT, observed_at=AT+timedelta(seconds=2),
        quote_context={"round_id": "origin-round", "committed_at": AT+timedelta(seconds=1), "as_of_at": AT},
        price=10., stop_loss_price=9.5, shadow_event_key=event_key)
    await db.commit()
    metadata = {"portfolio_signal_key": row.signal_key,
                "candidate": json.loads(row.candidate_json), "confirmed_at": AT.isoformat(), "stop_loss_price": 9.5}
    cmd = service.SubmitOrderCommand(code="600001", side="buy", price=10, quantity=100,
        account_id=PORTFOLIO_ACCOUNT, strategy_id="paper-challenger-forward" if route else "paper-auto-short",
        strategy_version=entry_version(row.origin_version, policy_version=row.portfolio_version),
        source=source, signal_id=source_id, decision_at=AT+timedelta(seconds=3),
        as_of_at=AT, decision_round_id="allocation-round", defer_until_next_round=True,
        deferred_metadata=metadata, stop_loss_price=9.5, idempotency_key="portfolio:"+row.signal_key)
    return row, cmd, metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("account", ACCOUNT_NAMES)
async def test_all_origins_keep_policy_and_use_only_shared_wallet(memory_session, active, account):
    row, cmd, meta = await captured(memory_session, account)
    origin = await provenance.command_origin(memory_session, cmd)
    assert cmd.account_id == PORTFOLIO_ACCOUNT
    assert origin["origin_account"] == account
    assert origin["origin_version"] == paper._strategy_version(account)
    assert origin["candidate"]["code"] == cmd.code
    await service._freeze_pending_buy_validity(memory_session, cmd, meta, decision_at=cmd.decision_at)
    validity = meta["buy_validity"]
    assert validity["status"] == "valid"
    assert validity["account_name"] == PORTFOLIO_ACCOUNT
    assert validity["origin_account"] == account
    assert validity["strategy_version"] == cmd.strategy_version
    assert validity["confirmed_at"] == AT.isoformat()
    order = SimpleNamespace(**vars(cmd), trade_date=AT.date())
    assert service._pending_buy_time_reason(order, meta, now=AT+timedelta(seconds=4)) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("source", "forged"), ("code", "600002"), ("signal_id", "forged"),
    ("strategy_version", "wrong"), ("strategy_id", "manual"),
    ("broker", "live"),
])
async def test_labels_cannot_forge_original_signal(memory_session, active, field, value):
    _, cmd, meta = await captured(memory_session)
    setattr(cmd, field, value)
    with pytest.raises(provenance.PortfolioIdentityError):
        await provenance.command_origin(memory_session, cmd)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing_key", "unknown_key", "future", "expired", "cross_day", "disabled", "version"])
async def test_original_identity_and_clock_fail_closed(memory_session, active, monkeypatch, kind):
    _, cmd, meta = await captured(memory_session)
    if kind == "missing_key":
        meta.pop("portfolio_signal_key")
    elif kind == "unknown_key":
        meta["portfolio_signal_key"] = "unpublished"
    elif kind == "future":
        cmd.decision_at = AT+timedelta(seconds=1)
    elif kind == "expired":
        cmd.decision_at = AT+timedelta(seconds=721)
    elif kind == "cross_day":
        cmd.decision_at += timedelta(days=1)
    elif kind == "disabled":
        monkeypatch.setattr(settings, "PAPER_PORTFOLIO_ENABLED", False)
    else:
        monkeypatch.setattr(paper, "_strategy_version", lambda _a: "new-source-version")
    with pytest.raises(provenance.PortfolioIdentityError):
        await provenance.command_origin(memory_session, cmd)
    assert await service._pending_order_version_reason(memory_session, cmd, meta, now=cmd.decision_at)


@pytest.mark.asyncio
async def test_pending_primary_reuses_original_not_shared_route(memory_session, active, monkeypatch):
    _, cmd, meta = await captured(memory_session, "promotion")
    await service._freeze_pending_buy_validity(memory_session, cmd, meta, decision_at=cmd.decision_at)
    original = AsyncMock(return_value=("waiting", "original predicate"))
    monkeypatch.setattr(paper, "_pending_primary_buy_confirmation", original)
    assert await service._pending_buy_current_status(memory_session, cmd, meta, None, now=cmd.decision_at) == (
        "waiting", "original predicate")
    assert original.call_args.kwargs["account_name"] == "promotion"


@pytest.mark.asyncio
async def test_public_order_route_cannot_impersonate_portfolio(active):
    req = trading.SubmitOrderRequest(code="600001", side="buy", price=10, quantity=100,
                                    account_id=PORTFOLIO_ACCOUNT, strategy_id="paper-auto-short")
    with pytest.raises(HTTPException) as exc:
        await trading.create_order(req, AsyncMock())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_invalid_provenance_blocks_before_market_or_risk_read(monkeypatch):
    load = AsyncMock()
    monkeypatch.setattr(service.stock_tagger, "load_status", load)
    cmd = service.SubmitOrderCommand(code="600001", side="buy", price=10, quantity=100,
                                    account_id=PORTFOLIO_ACCOUNT, decision_at=AT)
    result = await service._pre_trade_risk_check(AsyncMock(), cmd)
    assert result["final_level"] == "block"
    assert result["block_reasons"][0]["rule"] == "paper_portfolio_origin"
    load.assert_not_awaited()
