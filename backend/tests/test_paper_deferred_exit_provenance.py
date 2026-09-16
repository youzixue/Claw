"""Real deferred-outcome producer -> API audit regression, isolated from brokers.

Never prefill a mocked producer's deferred field: that hid the original bug.
Risk, quote/depth and broker responses are fixture-only; no business DB or profit
validation. Original T+1/order policies are not changed by this audit patch.
"""
import copy
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.v1 import paper
from app.db.session import Base
from app.models.paper import PaperAccount, PaperAutoTradeLog, PaperTradeLog, PaperPosition, PaperShadowEvent
from app.paper import position_observation as observation
from app.models.trading import TradeFill, TradeOrder
from app.trading import service
from paper_pending_fixture import accepted_frame

AT = datetime(2026, 9, 9, 10)
MODES = (
    "canceled", "version_changed", "version_missing", "quote_wait", "depth_wait",
    "risk_blocked", "broker_rejected", "partial", "filled",
)
EXPECTED = {
    "canceled": "canceled", "version_changed": "risk_blocked",
    "version_missing": "risk_blocked", "quote_wait": "waiting",
    "depth_wait": "waiting", "risk_blocked": "risk_blocked",
    "broker_rejected": "rejected", "partial": "partial", "filled": "filled",
}


@pytest_asyncio.fixture
async def memory_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as db:
            yield db
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("side", ["sell", "buy"])
async def test_real_outcome_retains_sell_provenance_without_changing_execution(
    memory_session, monkeypatch, mode, legacy, side,
):
    db = memory_session
    monkeypatch.setattr(observation, "_now", lambda: AT + timedelta(seconds=2))
    monkeypatch.setattr("app.paper.confirmation_evidence._observation_now", lambda: AT + timedelta(seconds=2))
    # Audit producer matrix, not an entry-policy fixture; the actual buy guard is
    # exercised without mocking in test_pending_buy_validity.py (sells stay exempt).
    monkeypatch.setattr(service, "_requires_pending_buy_validity", lambda _order: False)
    quote_calls, risk_calls, broker_calls, captured = [], [], [], []

    async def spot(*_args):
        return SimpleNamespace(code="600001", price=10, name="isolated",
            ask1_price=10, ask1_volume=1, bid1_price=10, bid1_volume=1,
            source_quote_at=AT, received_at=AT, updated_at=AT, quote_round_id="fixture-next")

    def quote_check(*_args, **_kwargs):
        quote_calls.append(True)
        return (False, "fixture_stale") if mode == "quote_wait" else (True, "")

    async def risk(_db, command):
        risk_calls.append(command)
        return {"final_level": "block" if mode == "risk_blocked" else "pass",
                "warnings": [], "block_reasons": []}

    class FixtureBroker:
        async def place_order(self, _db, request):
            broker_calls.append(request)
            if mode == "broker_rejected":
                raise RuntimeError("isolated fixture rejected")
            if side == "sell":
                # Fixture-only position mutation before the real producer returns.
                position.buy_amount -= 100
                position.is_closed = position.buy_amount == 0
            fill = SimpleNamespace(
                fill_id="fixture-fill", price=10.0, quantity=100, commission=5.0,
                tax=0.5, realized_pnl=0.0, broker_trade_id="999", raw={},
                filled_at=AT,
            )
            return SimpleNamespace(accepted=True, external_order_id="fixture-external", fills=[fill])

    original_reconcile = service.reconcile_paper_deferred_orders

    async def capture_real_outcomes(*args, **kwargs):
        rows = await original_reconcile(*args, **kwargs)
        captured.extend(copy.deepcopy(rows))
        return rows

    monkeypatch.setattr(service, "reconcile_paper_deferred_orders", capture_real_outcomes)
    monkeypatch.setattr(service, "_paper_execution_spot", spot)
    monkeypatch.setattr(paper, "_execution_quote_status", quote_check)
    monkeypatch.setattr(
        service, "_depth_fill_plan",
        lambda *_a, **_kw: (0, None, []) if mode == "depth_wait" else
            (100, 10.0, [{"level": 1, "price": 10.0, "quantity": 100}]),
    )
    monkeypatch.setattr(service, "_pre_trade_risk_check", risk)
    monkeypatch.setattr(service, "get_broker_adapter", lambda _name: FixtureBroker())

    account = PaperAccount(
        account_name="default", initial_capital=100000,
        current_capital=100000, total_assets=100000,
    )
    db.add(account)
    await db.flush()
    current_version = paper._strategy_version("default")
    version = (None if mode == "version_missing" else
               "frozen-old-fixture" if mode == "version_changed" else current_version)
    metadata = {"decision_round_id": "fixture-decision", "block_warn": False,
                "position_id": 42}
    if not legacy:
        metadata["candidate"] = {
            "exit_trigger_reason": "原始止损", "exit_execution_status": "submitting",
            "exit_policy": {"basis": "frozen_entry_order"},
        }
    frozen_candidate = copy.deepcopy(metadata.get("candidate"))
    order = TradeOrder(
        order_id="fixture-order", broker="paper", account_id="default",
        code="600001", side=side, order_type="limit", price=9.9,
        quantity=300 if mode == "partial" else 100, filled_quantity=0,
        status="submitted", strategy_id="paper-auto-short", strategy_version=version,
        source="position", reason="original order reason",
        decision_round_id="fixture-decision",
        trade_date=AT.date() - timedelta(days=1) if mode == "canceled" else AT.date(),
        created_at=AT - timedelta(seconds=30), decision_at=AT - timedelta(seconds=30),
        as_of_at=AT - timedelta(seconds=30),
        risk_json=json.dumps({"paper_deferred_order": metadata}),
    )
    db.add(order)
    position = PaperPosition(
        id=42, account_id=account.id, code="600001", name="isolated",
        buy_time=AT - timedelta(days=1), buy_price=10, buy_amount=order.quantity,
        strategy_version=version, is_closed=False,
    )
    if side == "sell":
        db.add(position)
    await db.commit()
    payload = {"round_id": "fixture-next", "quality_status": "ok",
        "committed_at": AT, "as_of_at": AT, "records": [{"code": "600001", "name": "isolated", "price": 10}],
        "config_version": "provenance-fixture", "code_version": "provenance-fixture"}
    if mode in {"broker_rejected", "partial", "filled"}:
        await accepted_frame(db, payload)
        monkeypatch.setattr(paper, "_public_order_clock", lambda: AT)
    token = paper._QUOTE_ROUND_CONTEXT.set(payload)
    try:
        kwargs = dict(account=account, trade_date=AT.date(), trigger="fixture", observed_at=AT)
        logs = await paper._reconcile_deferred_order_logs(db, run_id="fixture", **kwargs)
        repeated = await paper._reconcile_deferred_order_logs(db, run_id="repeat", **kwargs)
        if side == "sell" and mode == "filled":
            assert position.buy_amount == 0 and position.is_closed is True
            # The later real scan has no open position: execution evidence must
            # originate in reconciliation, never a fabricated pre-position frame.
            assert await paper._run_auto_sells(
                db, account=account, run_id="later-scan", trade_date=AT.date(),
                trigger="fixture", execute=True, quote_now=AT, log_holds=False,
            ) == []
        await db.commit()
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)

    assert repeated == []
    assert len(logs) == len(captured) == 1
    assert captured[0]["event"] == EXPECTED[mode]
    assert order.status == ({"quote_wait": "submitted", "depth_wait": "submitted"}
                            .get(mode, EXPECTED[mode]))
    assert order.strategy_version == version
    assert order.filled_quantity == (100 if mode in {"partial", "filled"} else 0)
    assert json.loads(order.risk_json)["paper_deferred_order"].get("candidate") == frozen_candidate
    payload = json.loads(logs[0].candidate_json)
    if side == "sell":
        assert captured[0]["deferred"].get("candidate") == frozen_candidate
        assert payload["exit_trigger_reason"] == ("" if legacy else "原始止损")
        assert payload["exit_trigger_basis"] == ("legacy_missing" if legacy else "original_decision")
        assert logs[0].strategy_version == (version or "legacy_unversioned")
        assert payload["exit_order_status"] == order.status
        assert payload["exit_filled_quantity_this_round"] == order.filled_quantity
    else:
        assert captured[0]["deferred"].get("candidate") == frozen_candidate
        assert "exit_trigger_basis" not in payload
        assert logs[0].strategy_version == (version or "legacy_unversioned")
        assert payload["confirmation_evidence"]["order_result"] == order.status
        assert payload["confirmation_evidence"]["historical_quote_path_confirmed"] == "unknown"
        assert payload["confirmation_evidence"]["current_setup_valid"] == "unknown"

    fills = list((await db.scalars(select(TradeFill))).all())
    assert len(fills) == (1 if mode in {"partial", "filled"} else 0)
    if fills:
        assert (fills[0].quantity, fills[0].price, fills[0].commission, fills[0].tax) == (100, 10, 5, 0.5)
        assert fills[0].decision_round_id == "fixture-decision"
        assert fills[0].fill_round_id == "fixture-next"
    # Original preflight remains; accepted matches now recheck after risk awaits.
    assert len(quote_calls) == (int(mode not in {"canceled", "version_changed", "version_missing"})
                                + int(mode in {"broker_rejected", "partial", "filled"}))
    assert len(risk_calls) == (int(mode in {"risk_blocked", "broker_rejected", "partial", "filled"})
                               + int(mode in {"broker_rejected", "partial", "filled"}))
    assert len(broker_calls) == int(mode in {"broker_rejected", "partial", "filled"})
    assert await db.scalar(select(func.count()).select_from(PaperAutoTradeLog)) == 1
    assert await db.scalar(select(func.count()).select_from(PaperTradeLog)) == 0
    events = list((await db.scalars(select(PaperShadowEvent))).all())
    assert len(events) == int(side == "sell")
    if events:
        row = events[0]
        assert row.route_id == observation.EXECUTION_ROUTE_ID
        assert row.event_type == "execution_frame" and row.price is None
        evidence = json.loads(row.snapshot_json)
        assert evidence["account"] == {"id": account.id, "name": "default"}
        assert evidence["position_id_from_original_metadata"] == 42
        assert evidence["order_response"]["strategy_version"] == version
        assert evidence["original_exit_trigger"] == (None if legacy else "原始止损")
        assert evidence["original_exit_trigger_basis"] == ("legacy_missing" if legacy else "original_decision")
        assert evidence["outcome_event"] == EXPECTED[mode]
        assert evidence["response_observed_at"] == (AT + timedelta(seconds=2)).isoformat()
        assert evidence["strategy_evaluated_at"] == AT.isoformat()
        assert evidence["position_fully_closed"] is None and evidence["replay_ready"] is False
        assert len(evidence["fill_responses"]) == len(fills)
        if fills:
            # Preserve the producer's source clock string, not a rewritten
            # physical observation clock or a different timestamp spelling.
            assert evidence["fill_responses"][0]["filled_at"] == captured[0]["fills"][0]["filled_at"]
            assert datetime.fromisoformat(evidence["fill_responses"][0]["filled_at"]) == AT
            assert evidence["fill_responses"][0]["commission"] == 5
            assert evidence["fill_responses"][0]["tax"] == 0.5
        assert evidence["commit_known_at"] is None
        assert await db.scalar(select(func.count()).select_from(PaperShadowEvent).where(
            PaperShadowEvent.route_id == observation.ROUTE_ID)) == 0
