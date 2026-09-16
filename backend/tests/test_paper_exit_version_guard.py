"""Old-entry exits require a fresh decision bound to the original sellable position.

Only isolated in-memory tables and fixture broker responses; no live orders.
"""
import copy
import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.v1 import paper
from app.db.session import Base
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog
from app.models.trading import TradeFill, TradeOrder
from app.trading import service
from paper_pending_fixture import accepted_frame

AT = datetime(2026, 9, 11, 10)


@pytest_asyncio.fixture
async def exit_case():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            account = PaperAccount(account_name="challenger_c", status="active",
                                   initial_capital=50000, current_capital=50000)
            db.add(account)
            await db.flush()
            position = PaperPosition(account_id=account.id, code="600001",
                buy_price=10, buy_amount=300, buy_time=AT-timedelta(days=1),
                strategy_version="entry-old", is_closed=False)
            db.add(position)
            await db.flush()
            metadata = {
                "decision_round_id": "decision",
                "position_id": position.id, "block_warn": False,
                "exit_decision_strategy_version": paper._strategy_version("challenger_c"),
                "candidate": {
                    "exit_trigger_reason": "原持仓硬止损",
                    "exit_parameters": {"stop_loss_pct": 6, "max_hold_days": 3},
                    "exit_policy": {"basis": "frozen_entry_order",
                                    "position_strategy_version": "entry-old"},
                },
            }
            order = TradeOrder(order_id="old-position-exit", broker="paper",
                account_id="challenger_c", code="600001", side="sell",
                strategy_id="paper-auto-short", strategy_version="entry-old",
                source="position", signal_id="auto-sell-decision",
                price=9, quantity=300, filled_quantity=0, status="submitted",
                order_type="limit", reason="原持仓硬止损",
                decision_round_id="decision", decision_at=AT-timedelta(seconds=30),
                trade_date=AT.date(), created_at=AT-timedelta(seconds=30),
                risk_json=json.dumps({"paper_deferred_order": metadata}))
            db.add(order)
            await db.commit()
            yield db, account, position, order, metadata
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_fresh_exit_retains_entry_attribution_and_frozen_parameters(exit_case, legacy):
    db, _, position, order, metadata = exit_case
    if legacy:
        position.strategy_version = None
        order.strategy_version = "legacy_unversioned"
        metadata["candidate"]["exit_policy"] = {
            "basis": "legacy_or_missing_entry_evidence", "position_strategy_version": ""}
    original = copy.deepcopy(metadata["candidate"])
    assert await service._pending_order_version_reason(db, order, metadata, now=AT) == ""
    assert metadata["candidate"] == original
    assert metadata["exit_version_validation"]["entry_strategy_version"] == order.strategy_version
    assert position.strategy_version == (None if legacy else "entry-old")


@pytest.mark.asyncio
@pytest.mark.parametrize("violation", [
    "buy", "missing_order_version", "wrong_source", "wrong_strategy",
    "missing_exit_version", "stale_exit_version", "missing_position", "bool_position",
    "wrong_account", "closed_account", "closed_position", "wrong_code",
    "changed_entry_version", "missing_candidate", "missing_parameters",
    "missing_policy", "wrong_policy_version", "no_trigger",
    "no_decision_clock", "future_decision", "new_position", "today_position",
    "oversell", "today_added", "no_remaining",
])
async def test_exception_fails_closed_for_unbound_stale_or_unsellable_orders(exit_case, violation):
    db, account, position, order, metadata = exit_case
    if violation == "buy":
        order.side = "buy"
    elif violation == "missing_order_version":
        order.strategy_version = None
    elif violation == "wrong_source":
        order.source = "radar"
    elif violation == "wrong_strategy":
        order.strategy_id = "manual"
    elif violation == "missing_exit_version":
        metadata.pop("exit_decision_strategy_version")
    elif violation == "stale_exit_version":
        metadata["exit_decision_strategy_version"] = "another-old-decision"
    elif violation == "missing_position":
        metadata.pop("position_id")
    elif violation == "bool_position":
        metadata["position_id"] = True
    elif violation == "wrong_account":
        order.account_id = "default"
        metadata["exit_decision_strategy_version"] = paper._strategy_version("default")
    elif violation == "closed_account":
        account.status = "closed"
    elif violation == "closed_position":
        position.is_closed = True
    elif violation == "wrong_code":
        order.code = "600002"
    elif violation == "changed_entry_version":
        position.strategy_version = "different-entry"
    elif violation == "missing_candidate":
        metadata.pop("candidate")
    elif violation == "missing_parameters":
        metadata["candidate"].pop("exit_parameters")
    elif violation == "missing_policy":
        metadata["candidate"].pop("exit_policy")
    elif violation == "wrong_policy_version":
        metadata["candidate"]["exit_policy"]["position_strategy_version"] = "other"
    elif violation == "no_trigger":
        metadata["candidate"]["exit_trigger_reason"] = ""
    elif violation == "no_decision_clock":
        order.decision_at = None
    elif violation == "future_decision":
        order.decision_at = AT+timedelta(seconds=1)
    elif violation == "new_position":
        position.buy_time = AT-timedelta(seconds=1)
    elif violation == "today_position":
        position.buy_time = AT-timedelta(minutes=30)
    elif violation == "oversell":
        order.quantity = 400
    elif violation == "today_added":
        db.add(PaperTradeLog(account_id=account.id, code=position.code,
            trade_type="buy", amount=100, price=9, trade_time=AT-timedelta(minutes=10)))
    elif violation == "no_remaining":
        order.filled_quantity = order.quantity
    assert await service._pending_order_version_reason(db, order, metadata, now=AT)
    assert "exit_version_validation" not in metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [
    ("fill", "partial"), ("quote", "waiting"), ("depth", "waiting"),
    ("risk", "risk_blocked"), ("no_new_round", None), ("database", "abort"),
])
async def test_real_reconciliation_preserves_quote_depth_risk_and_round_guards(
    exit_case, monkeypatch, mode, expected,
):
    db, _, position, order, metadata = exit_case
    original = copy.deepcopy(metadata["candidate"])
    calls = []
    async def spot(*_args):
        return SimpleNamespace(code="600001", price=9, name="fixture",
            bid1_price=9, bid1_volume=1,
            source_quote_at=AT, received_at=AT, updated_at=AT, quote_round_id="next")
    async def risk(_db, command):
        calls.append("risk")
        return {"final_level": "block" if mode == "risk" else "pass",
                "block_reasons": [], "warnings": []}
    class Broker:
        async def place_order(self, _db, request):
            calls.append("broker")
            assert request.strategy_version == "entry-old"
            if mode == "database":
                raise OperationalError("fixture", {}, sqlite3.OperationalError("database is locked"))
            position.buy_amount -= request.quantity
            fill = SimpleNamespace(fill_id="fixture-fill", price=9, quantity=100,
                commission=5, tax=.45, realized_pnl=-105.45, broker_trade_id="999",
                raw={}, filled_at=AT)
            return SimpleNamespace(accepted=True, external_order_id="fixture-external", fills=[fill])
    monkeypatch.setattr(service, "_paper_execution_spot", spot)
    monkeypatch.setattr(paper, "_execution_quote_status",
                        lambda *_a, **_kw: (mode != "quote", "fixture stale"))
    monkeypatch.setattr(service, "_depth_fill_plan",
                        lambda *_a, **_kw: (0, None, []) if mode == "depth" else
                            (100, 9, [{"level": 1, "price": 9, "quantity": 100}]))
    monkeypatch.setattr(service, "_pre_trade_risk_check", risk)
    monkeypatch.setattr(service, "get_broker_adapter", lambda _name: Broker())
    round_id = "decision" if mode == "no_new_round" else "next"
    payload = {"round_id": round_id, "quality_status": "ok", "as_of_at": AT, "committed_at": AT}
    if mode in {"fill", "database"}:
        payload.update(config_version="exit-version-fixture", code_version="exit-version-fixture",
                       records=[{"code": "600001", "name": "fixture", "price": 9}])
        await accepted_frame(db, payload)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: AT)
    token = paper._QUOTE_ROUND_CONTEXT.set(payload)
    try:
        if expected == "abort":
            with pytest.raises(OperationalError):
                await service.reconcile_paper_deferred_orders(db, account_id=order.account_id, now=AT)
            assert calls == ["risk", "risk", "broker"]
            await db.refresh(order)  # atomic rollback expires ORM state; verify persisted state
            assert order.status == "submitted" and order.filled_quantity == 0
            assert list((await db.scalars(select(TradeFill))).all()) == []
            await db.rollback()
            return
        result = await service.reconcile_paper_deferred_orders(db, account_id=order.account_id, now=AT)
        if expected is None:
            assert result == []
        else:
            assert result[0]["event"] == expected
            assert result[0]["deferred"]["candidate"] == original
        assert await service.reconcile_paper_deferred_orders(db, account_id=order.account_id, now=AT) == []
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)
    assert calls == (["risk", "risk", "broker"] if mode == "fill" else ["risk"] if mode == "risk" else [])
    assert order.strategy_version == position.strategy_version == "entry-old"
    assert order.filled_quantity == (100 if mode == "fill" else 0)
    fills = list((await db.scalars(select(TradeFill))).all())
    assert len(fills) == (1 if mode == "fill" else 0)
    if fills:
        assert fills[0].decision_round_id == "decision"
        assert fills[0].fill_round_id == "next"
        # Remaining 200 shares still match the original position after a partial fill.
        remaining_metadata = json.loads(order.risk_json)["paper_deferred_order"]
        assert await service._pending_order_version_reason(
            db, order, remaining_metadata, now=AT+timedelta(seconds=30)) == ""
