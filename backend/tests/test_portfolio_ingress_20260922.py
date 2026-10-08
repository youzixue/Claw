"""Shared-wallet outbox: isolated clocks/transactions, no market or network I/O."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json

import pytest
from sqlalchemy import select

from app.paper import portfolio_ingress as ingress
from app.models.paper import PaperPortfolioSignal
from test_paper_deferred_exit_provenance import memory_session

AT = datetime(2026, 9, 22, 13, 30)


def arguments(**changes):
    values = dict(
        account=SimpleNamespace(id=1, account_name="default", status="active"),
        source="next_day_plan",
        candidate={"code": "600001", "name": "测试", "_source": "next_day_plan", "score": 90},
        source_signal_id="auto-source-round-600001", confirmed_at=AT,
        quote_context={"round_id": "qr-1", "as_of_at": AT-timedelta(seconds=1), "committed_at": AT},
        price=10., stop_loss_price=9.5, observed_at=AT+timedelta(seconds=2),
    )
    values.update(changes)
    return values


@pytest.fixture
def active(monkeypatch):
    monkeypatch.setattr(ingress, "portfolio_active", lambda at: at is not None and at >= AT)
    monkeypatch.setattr(ingress, "portfolio_version", lambda: "portfolio-test")


@pytest.mark.asyncio
async def test_disabled_never_reads_or_writes(monkeypatch):
    db = AsyncMock()
    monkeypatch.setattr(ingress, "portfolio_active", lambda at: False)
    assert await ingress.capture_confirmed_signal(db, **arguments()) is None
    assert not db.mock_calls


@pytest.mark.asyncio
async def test_same_source_transaction_and_no_cash_dependency(memory_session, active):
    db = memory_session
    args = arguments()
    args["account"].current_capital = 0  # The baseline wallet is full, not the shared wallet.
    original = dict(args["candidate"])
    row = await ingress.capture_confirmed_signal(db, **args)
    assert row.signal_key.startswith("ps1:")
    assert row.origin_account_id == 1 and row.source_signal_id == args["source_signal_id"]
    assert row.confirmed_at == AT
    assert json.loads(row.candidate_json) == original
    assert args["candidate"] == original
    assert (await ingress.capture_confirmed_signal(db, **args)).id == row.id
    await db.rollback()
    assert await db.scalar(select(PaperPortfolioSignal.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"price": 10.01}, {"stop_loss_price": 9.6}, {"entry_details": {"score": 999}},
    {"candidate": {"code": "600001", "score": 999}},
    {"confirmed_at": AT+timedelta(seconds=1)},
])
async def test_duplicate_key_cannot_silently_publish_new_evidence(memory_session, active, change):
    db = memory_session
    row = await ingress.capture_confirmed_signal(db, **arguments())
    await db.commit()
    original = row.entry_policy_json
    with pytest.raises(ValueError, match="conflicts"):
        await ingress.capture_confirmed_signal(db, **arguments(**change))
    await db.rollback()
    stored = await db.scalar(select(PaperPortfolioSignal))
    assert stored.entry_policy_json == original and stored.confirmed_at == AT


@pytest.mark.asyncio
async def test_repeat_observation_retains_original_clock(memory_session, active):
    db = memory_session
    first = await ingress.capture_confirmed_signal(db, **arguments())
    later = await ingress.capture_confirmed_signal(db, **arguments(observed_at=AT+timedelta(seconds=10)))
    assert first.id == later.id and later.observed_at == AT+timedelta(seconds=2)


@pytest.mark.asyncio
async def test_new_round_retains_additional_sample_not_overwriting(memory_session, active):
    first = await ingress.capture_confirmed_signal(memory_session, **arguments())
    second = await ingress.capture_confirmed_signal(memory_session, **arguments(
        quote_context={"round_id": "qr-2", "as_of_at": AT, "committed_at": AT+timedelta(seconds=1)},
        confirmed_at=AT+timedelta(seconds=1)))
    assert first.signal_key != second.signal_key
    assert first.confirmed_at == AT


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"confirmed_at": AT-timedelta(seconds=1)},
    {"confirmed_at": AT+timedelta(seconds=10)},
    {"observed_at": AT+timedelta(days=1)},
    {"observed_at": AT+timedelta(seconds=1000)},
    {"quote_context": {"round_id": "qr-1", "as_of_at": AT+timedelta(seconds=1)}},
    {"quote_context": {"round_id": "qr-1", "as_of_at": None}},
])
async def test_causal_and_activation_failures_create_no_signal(memory_session, active, changes):
    assert await ingress.capture_confirmed_signal(memory_session, **arguments(**changes)) is None
    assert await memory_session.scalar(select(PaperPortfolioSignal.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"price": float("nan")}, {"price": True}, {"stop_loss_price": 11.},
    {"source_signal_id": ""}, {"candidate": {"code": "600001", "score": float("inf")}},
])
async def test_invalid_owned_payload_cannot_publish(memory_session, active, changes):
    with pytest.raises((ValueError, TypeError)):
        await ingress.capture_confirmed_signal(memory_session, **arguments(**changes))
    assert await memory_session.scalar(select(PaperPortfolioSignal.id)) is None


@pytest.mark.asyncio
async def test_all_origin_accounts_have_freezable_exit_policy(memory_session, active):
    from app.paper.account_policy import ACCOUNT_NAMES, ROUTE_ACCOUNT_NAMES
    for index, name in enumerate(ACCOUNT_NAMES):
        route = next((r for r, a in ROUTE_ACCOUNT_NAMES.items() if a == name), "")
        row = await ingress.capture_confirmed_signal(memory_session, **arguments(
            account=SimpleNamespace(id=index+1, account_name=name, status="active"),
            source=route or "next_day_plan", shadow_event_key=("event-"+name if route else ""),
        ))
        policy = json.loads(row.entry_policy_json)
        assert policy["exit_parameters"]
        assert policy["parameters"]["account_name"] == name
        assert policy["exit_mode"] in {"short", "midline"}
