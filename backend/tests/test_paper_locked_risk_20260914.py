"""Real ten-rule risk, current ORM projections and real books in isolated SQLite.
The only signal-policy override disables route selection (tested elsewhere), never
the risk chain, stock-status loader, quote/depth validation or atomic ledger.
"""
import asyncio
import json
from datetime import timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update, delete

from app.api.v1 import paper
from app.core.trade_calendar import TradeCalendar
from app.models.paper import PaperAccount, PaperPosition, PaperTradeLog, PaperNav
from app.models.stock import StockTag, StockBlacklist, StockSpot, MarketSentiment
from app.models.trading import TradeFill, TradeOrder
from app.risk.engine import risk_engine
from app.risk.rules import register_all_rules
from app.trading import service, paper_authorization
from test_quote_round_execution import quote_execution_env
from test_paper_account_round_capacity_20260914 import prepare, fill_count, PATHS as DEPTH_PATHS, FILL_AT
from test_paper_orphan_fill_guard import AT
from test_paper_atomic_execution_20260914 import snapshot

PATHS = [*DEPTH_PATHS, "queue-sealed"]


@pytest.fixture(autouse=True)
def real_risk_calendar(monkeypatch):
    monkeypatch.setattr(paper, "_TRADE_LOCK", asyncio.Lock())
    monkeypatch.setattr(service.settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", False)
    monkeypatch.setattr(service, "_requires_pending_buy_validity", lambda _: False)
    original_now = paper._paper_now
    monkeypatch.setattr(paper, "_paper_now", lambda:
        original_now() if paper._PAPER_FILL_CONTEXT.get() or paper._QUOTE_ROUND_CONTEXT.get() else AT)
    monkeypatch.setattr(paper, "_public_order_clock", lambda: AT)
    monkeypatch.setattr(paper.trade_calendar, "_cache", {
        AT.date()-timedelta(days=i): (AT.date()-timedelta(days=i)).weekday()<5 for i in range(40)})
    async def loaded(self, year):
        assert self is paper.trade_calendar and year == 2026
    monkeypatch.setattr(TradeCalendar, "_ensure_loaded", loaded)
    network = AsyncMock(side_effect=AssertionError("no network in risk fixture"))
    monkeypatch.setattr(TradeCalendar, "_sync_from_source", network)
    old_rules = risk_engine._rules
    risk_engine._rules = []
    register_all_rules()
    checks = []
    real_check = risk_engine.check
    def checked(context):
        result = real_check(context)
        checks.append((paper_authorization._PAPER_TRANSACTION.get() is not None, context, result))
        return result
    monkeypatch.setattr(risk_engine, "check", checked)
    yield checks
    risk_engine._rules = old_rules
    network.assert_not_awaited()
    assert checks and all(r["checked_rules"] == 10 and r["evaluation_status"] == "complete" for _, _, r in checks)


async def qualified(factory, monkeypatch, path):
    async with factory() as db:
        db.add(StockTag(code="600001", name="轮次测试", board_type="main_sh", board_tag="tradeable",
            is_st=False, is_suspended=False, is_delisting=False, is_ipo_recent=False))
        # Expired row deliberately retained in the caller identity map.
        db.add(StockBlacklist(code="600001", reason="manual", start_date=AT.date()-timedelta(days=2),
            end_date=AT.date()-timedelta(days=1)))
        db.add(MarketSentiment(trade_date=AT.date(), sentiment_cycle="recovery", sentiment_score=60,
            limit_up_count=40, limit_down_count=5, broken_limit_count=8, seal_rate=70, board_height=3,
            advance_decline_ratio=1.2, turnover_total=1.2, main_net_inflow=20, quality_status="ok",
            calculation_version="isolated-real-risk", observed_at=AT-timedelta(seconds=2)))
        await db.commit()
    invoke, payload, commands = await prepare(factory, monkeypatch,
        ["queue-buy" if path=="queue-sealed" else path], hands=10)
    if path == "queue-sealed":
        payload["records"][0].update(price=11, ask1_price=0, bid1_price=11, bid1_volume=10, volume=1020)
    real_precheck = service._pre_trade_risk_check
    retained = []
    async def precheck(db, command):
        # SQLAlchemy identity maps use weak references; hold all old projections
        # so a second SELECT without populate_existing demonstrably stays stale.
        retained.extend([await db.get(StockTag, "600001"), await db.get(StockBlacklist, "600001"),
            await db.scalar(select(MarketSentiment)), await db.scalar(select(PaperAccount))])
        return await real_precheck(db, command)
    monkeypatch.setattr(service, "_pre_trade_risk_check", precheck)
    return invoke, payload, commands, retained


class MutationBeforeAcquisition:
    def __init__(self, mutate):
        self.lock = asyncio.Lock()
        self.mutate = mutate
        self.called = False
    async def __aenter__(self):
        if not self.called:
            self.called = True
            await self.mutate()
        return await self.lock.__aenter__()
    async def __aexit__(self, *args):
        return await self.lock.__aexit__(*args)


async def mutate(factory, kind):
    async with factory() as db:
        if kind == "suspend":
            await db.execute(update(StockTag).values(is_suspended=True))
        elif kind == "warn":
            await db.execute(update(StockTag).values(is_ipo_recent=True))
        elif kind == "identity_missing":
            await db.execute(delete(StockTag))
        elif kind == "observe":
            await db.execute(update(StockTag).values(board_tag="observe_only"))
        elif kind == "blacklist":
            await db.execute(update(StockBlacklist).values(end_date=None))
        elif kind == "sentiment":
            await db.execute(update(MarketSentiment).values(sentiment_cycle="freezing", sentiment_score=0))
        elif kind == "sentiment_missing":
            await db.execute(delete(MarketSentiment))
        elif kind == "account_closed":
            await db.execute(update(PaperAccount).values(status="closed"))
        elif kind == "account_replaced":
            await db.execute(update(PaperAccount).values(status="closed"))
            db.add(PaperAccount(account_name="default", initial_capital=50000, current_capital=50000,
                total_assets=50000, status="active"))
        elif kind == "position_limit":
            a = await db.scalar(select(PaperAccount))
            amount = int(a.initial_capital * 0.75 / 1000) * 100
            db.add(PaperPosition(account_id=a.id, code="600001", buy_price=10, current_price=10,
                buy_amount=amount, buy_time=AT-timedelta(days=1), is_closed=False,
                strategy_version=paper._strategy_version("default")))
            db.add(PaperTradeLog(account_id=a.id, code="600001", trade_type="buy", price=10, amount=amount,
                trade_time=AT-timedelta(days=1), commission=5, tax=0, signal_id="isolated-other-fill",
                strategy_version=paper._strategy_version("default")))
        elif kind == "drawdown":
            a = await db.scalar(select(PaperAccount))
            db.add(PaperNav(account_id=a.id, trade_date=AT.date()-timedelta(days=1), nav=1.5))
        elif kind != "unchanged":
            raise AssertionError(kind)
        await db.commit()


CASES = ([(p, "suspend") for p in PATHS] +
    [(p, k) for p in PATHS if not p.endswith("sell") for k in
     ("identity_missing", "observe", "blacklist", "sentiment", "sentiment_missing",
      "account_closed", "account_replaced", "position_limit", "drawdown")] +
    [(p, k) for p in PATHS if p.endswith("sell") for k in ("account_closed", "account_replaced")])


@pytest.mark.asyncio
@pytest.mark.parametrize("path,kind", CASES)
async def test_changed_risk_after_checkpoint_cannot_fill(
    quote_execution_env, monkeypatch, real_risk_calendar, path, kind,
):
    factory = quote_execution_env
    invoke, _, _, retained = await qualified(factory, monkeypatch, path)
    after_mutation = []
    async def mutation():
        # A separate session commits during actual checkpoint-to-lock window.
        await mutate(factory, kind)
        after_mutation.append(await snapshot(factory))
    gate = MutationBeforeAcquisition(mutation)
    monkeypatch.setattr(paper, "_TRADE_LOCK", gate)
    result = await invoke(0)
    assert gate.called and retained, "must reach the actual post-preflight lock boundary"
    assert fill_count(result) == 0, (path, kind, result)
    async with factory() as db:
        order = (await db.scalars(select(TradeOrder))).one()
        assert order.status == "risk_blocked", order.error_message
        proof = json.loads(order.risk_json)["paper_locked_risk"]
        assert proof["status"] == "blocked" and proof["evaluated_at"] == FILL_AT.isoformat()
        assert proof["scope"] == "current_projection_under_process_fill_lock"
        assert len((await db.scalars(select(PaperAccount))).all()) == (2 if kind=="account_replaced" else 1)
    # Account valuation may refresh, but no new trade/receipt/inventory mutation.
    after = await snapshot(factory)
    for key in ("paper_trade_log", "paper_sale_accounting", "trade_fill", "positions"):
        assert after[key] == after_mutation[0][key]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_unchanged_risk_rechecks_ten_rules_and_freezes_receipt(
    quote_execution_env, monkeypatch, real_risk_calendar, path,
):
    invoke, _, _, _ = await qualified(quote_execution_env, monkeypatch, path)
    assert fill_count(await invoke(0)) == 1
    assert any(locked for locked, _, _ in real_risk_calendar)
    async with quote_execution_env() as db:
        fill = (await db.scalars(select(TradeFill))).one()
        raw = json.loads(fill.raw_json)
        execution = raw.get("immediate_execution_evidence") or raw["pending_execution_timing"]
        proof = execution["locked_risk"]
        assert proof["status"] == "validated"
        assert proof["result"]["checked_rules"] == 10
        assert proof["price"] == fill.price and proof["quantity"] == fill.quantity
        assert proof["account_name"] == "default"
        assert proof["quote_round_id"] == fill.fill_round_id
        assert json.loads((await db.scalar(select(TradeOrder))).risk_json)["paper_locked_risk"] == proof


@pytest.mark.asyncio
@pytest.mark.parametrize("path,warn_setting,blocked", [
    ("immediate-buy", True, False), ("deferred-buy", True, True),
    ("queue-buy", True, True), ("queue-buy", False, False),
    ("queue-sealed", True, True), ("queue-sealed", False, False),
])
async def test_locked_warning_preserves_original_route_policy(
    quote_execution_env, monkeypatch, path, warn_setting, blocked,
):
    monkeypatch.setattr(service.settings, "PAPER_AUTO_WARN_RISK_BLOCK_BUY", warn_setting)
    invoke, _, _, _ = await qualified(quote_execution_env, monkeypatch, path)
    monkeypatch.setattr(paper, "_TRADE_LOCK", MutationBeforeAcquisition(
        lambda: mutate(quote_execution_env, "warn")))
    result = await invoke(0)
    assert fill_count(result) == (0 if blocked else 1)
    async with quote_execution_env() as db:
        order = (await db.scalars(select(TradeOrder))).one()
        proof = json.loads(order.risk_json)["paper_locked_risk"]
        assert proof["result"]["final_level"] == "warn"
        assert order.risk_level == "warn"
        assert proof["status"] == ("blocked" if blocked else "validated")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("seconds", [5, 240])
async def test_real_risk_await_does_not_refresh_quote_or_bypass_final_clock(
    quote_execution_env, monkeypatch, path, seconds,
):
    from fastapi import HTTPException
    invoke, _, _, _ = await qualified(quote_execution_env, monkeypatch, path)
    clock = [FILL_AT]
    monkeypatch.setattr(paper, "_public_order_clock", lambda: clock[0])
    real = service.sentiment_circuit_breaker.get_current_state
    async def delayed(db, day, **kwargs):
        result = await real(db, day, **kwargs)
        if kwargs.get("fresh"):
            clock[0] = FILL_AT + timedelta(seconds=seconds)
        return result
    monkeypatch.setattr(service.sentiment_circuit_breaker, "get_current_state", delayed)
    before = await snapshot(quote_execution_env)
    if seconds == 240 and path.startswith("queue"):
        with pytest.raises(HTTPException):
            await invoke(0)
    else:
        assert fill_count(await invoke(0)) == (1 if seconds == 5 else 0)
    async with quote_execution_env() as db:
        fills = (await db.scalars(select(TradeFill))).all()
        if seconds == 5:
            assert len(fills) == 1 and fills[0].filled_at == clock[0]
            raw = json.loads(fills[0].raw_json)
            p = raw.get("immediate_execution_evidence") or raw["pending_execution_timing"]
            assert p["dispatch_validated_at"] == FILL_AT.isoformat()
            assert p["locked_risk"]["completed_at"] == clock[0].isoformat()
            assert p["locked_risk"]["evaluated_at"] == FILL_AT.isoformat()
        else:
            assert fills == []
    if seconds == 240:
        after = await snapshot(quote_execution_env)
        for key in ("paper_trade_log", "paper_sale_accounting", "trade_fill", "positions"):
            assert after[key] == before[key]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("invalid", [None, FILL_AT-timedelta(microseconds=1),
    FILL_AT+timedelta(days=1), FILL_AT.replace(tzinfo=timezone.utc)])
async def test_risk_completion_clock_must_remain_valid_without_now_fallback(
    quote_execution_env, monkeypatch, path, invalid,
):
    invoke, _, _, _ = await qualified(quote_execution_env, monkeypatch, path)
    clock = [FILL_AT]
    monkeypatch.setattr(paper, "_public_order_clock", lambda: clock[0])
    real = service.sentiment_circuit_breaker.get_current_state
    async def delayed(db, day, **kwargs):
        result = await real(db, day, **kwargs)
        if kwargs.get("fresh"):
            clock[0] = invalid
        return result
    monkeypatch.setattr(service.sentiment_circuit_breaker, "get_current_state", delayed)
    before = await snapshot(quote_execution_env)
    result = await invoke(0)
    assert fill_count(result) == 0
    async with quote_execution_env() as db:
        order = (await db.scalars(select(TradeOrder))).one()
        proof = json.loads(order.risk_json)["paper_locked_risk"]
        assert order.status == "risk_blocked"
        assert proof["reason_code"] == "invalid_risk_evaluation_clock"
    after = await snapshot(quote_execution_env)
    for key in ("paper_trade_log", "paper_sale_accounting", "trade_fill", "positions"):
        assert after[key] == before[key]
