"""Execution-loop regressions: existing isolated fixtures, fake transport only.

F pending-data behavior remains owned by the parent paper.py task.
"""
import json

import pytest

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.api.v1 import paper
from app.trading import service
from app.models.paper import PaperAutoTradeLog
from app.push import paper_buy_points as points
from test_paper_buy_points import setup, record
from test_paper_deferred_exit_provenance import memory_session
from test_pending_buy_validity import AT, quote
from app.models.stock import LimitUpPool


@pytest.mark.asyncio
async def test_submit_risk_block_must_not_be_reported_as_signal_expiry(setup):
    maker, now, send = setup
    signal = await record(maker, now, account="challenger_b")
    async with maker() as db:
        db.add(PaperAutoTradeLog(
            account_id=signal.account_id, run_id="real-decision",
            trade_date=now.date(), created_at=now, source="test_route",
            code=signal.code, strategy_version=signal.strategy_version,
            action="skip_terminal", decision="skipped",
            reason="isolated submit risk block; original order terminal",
            candidate_json=json.dumps({
                "submission_order_status": "risk_blocked",
                "submission_recoverable": False,
            }),
        ))
        await db.commit()
    result = await points.dispatch_buy_points(now=now, session_factory=maker)
    assert result["status"] == "sent"
    audit = send.call_args.args[0].extra["signal_audits"][0]
    assert audit["execution_state"] == "blocked", audit


@pytest.mark.asyncio
async def test_canceled_next_round_must_not_remain_pending_in_buy_point_push(setup, monkeypatch):
    maker, now, send = setup
    signal = await record(maker, now)
    order_id = "isolated-pending-order"
    monkeypatch.setattr(service, "reconcile_paper_deferred_orders", AsyncMock(return_value=[{
        "event": "canceled", "reason": "buy_signal_expired: original confirmation expired",
        "order": {"order_id": order_id, "broker": "paper", "account_id": "default",
                  "code": signal.code, "side": "buy",
                  "status": "canceled", "source": "test_route", "price": 10.5,
                  "quantity": 100, "strategy_version": signal.strategy_version},
        "fills": [], "risk": {},
        "deferred": {"candidate": {"pending_order_id": order_id}},
    }]))
    monkeypatch.setattr(paper, "_stock_info", AsyncMock(return_value=("隔离测试", 10.5)))
    async with maker() as db:
        db.add(PaperAutoTradeLog(
            account_id=signal.account_id, run_id="real-decision",
            trade_date=now.date(), created_at=now, source="test_route",
            code=signal.code, strategy_version=signal.strategy_version,
            action="deferred_buy", decision="wait", reason="waiting for next round",
            candidate_json=json.dumps({"pending_order_id": order_id}),
        ))
        await db.commit()
        outcomes = await paper._reconcile_deferred_order_logs(
            db, account=SimpleNamespace(id=signal.account_id, account_name="default"),
            run_id="paper-risk-next-round", trade_date=now.date(),
            trigger="quote-round-risk", observed_at=now,
        )
        assert outcomes[0].action == "skip_buy"
        assert outcomes[0].reason_code == "deferred_canceled"
        await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=maker)
    audit = send.call_args.args[0].extra["signal_audits"][0]
    assert audit["execution_state"] == "canceled", audit


@pytest.mark.asyncio
async def test_highboard_queue_buy_must_be_visible_as_pending(setup):
    maker, now, send = setup
    signal = await record(maker, now, account="tenbagger", queue_order=True)
    async with maker() as db:
        db.add(PaperAutoTradeLog(
            account_id=signal.account_id, run_id="real-decision",
            trade_date=now.date(), created_at=now, source="test_route",
            code=signal.code, strategy_version=signal.strategy_version,
            action="queue_buy", decision="wait", reason="submitted limit-up queue",
            candidate_json=json.dumps({"pending_order_id": "isolated-queue-order"}),
        ))
        await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=maker)
    audit = send.call_args.args[0].extra["signal_audits"][0]
    assert audit["execution_state"] == "pending", audit


@pytest.mark.asyncio
async def test_f_missing_history_is_unknown_wait_not_terminal_cancellation(memory_session, monkeypatch):
    from datetime import timedelta
    db = memory_session
    prior = AT.date() - timedelta(days=1)
    monkeypatch.setattr(paper.settings, "PAPER_REVERSAL_ENABLED", True)
    monkeypatch.setattr(paper.trade_calendar, "previous_trade_day", AsyncMock(return_value=prior))
    db.add(LimitUpPool(code="002988", name="isolated", trade_date=prior, quarantined=False))
    await db.commit()
    diagnostics = []
    rows, _ = await paper._reversal_pullback_candidates(
        db, limit=1, trade_date=AT.date(), only_code="002988", diagnostics=diagnostics)
    assert not rows and diagnostics and diagnostics[0]["stage_code"] == "data_gate"
    status, reason = await paper._pending_primary_buy_confirmation(
        db, account_name="reversal", source="reversal_pullback",
        candidate={"code": "002988", "_source": "reversal_pullback", "signal_date": prior.isoformat()},
        spot=quote(AT), limit_price=30.88, now=AT)
    assert status == "waiting", (status, reason, diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [
    None, "account", "code", "source", "version", "date", "future", "order", "missing",
])
async def test_cross_round_receipt_requires_exact_original_identity(setup, mismatch):
    from datetime import timedelta
    maker, now, send = setup
    signal = await record(maker, now)
    original = dict(
        account_id=signal.account_id, run_id="real-decision", trade_date=now.date(),
        created_at=now, source="test_route", code=signal.code,
        strategy_version=signal.strategy_version, action="deferred_buy", decision="wait",
        candidate_json=json.dumps({"pending_order_id": "original-order"}))
    followup = dict(original, run_id="later-round", action="skip_buy", decision="skipped",
                    reason_code="deferred_canceled", reason="original remainder canceled",
                    candidate_json=json.dumps({"deferred_order_observation": {
                        "order_response": {"order_id": "original-order", "status": "canceled"}}}))
    changes = {
        "account": {"account_id": signal.account_id + 1}, "code": {"code": "600002"},
        "source": {"source": "another"}, "version": {"strategy_version": "another"},
        "date": {"trade_date": now.date() - timedelta(days=1)},
        "future": {"created_at": now + timedelta(seconds=1)},
        "order": {"candidate_json": json.dumps({"pending_order_id": "another-order"})},
        "missing": {"candidate_json": "{}"},
    }
    followup.update(changes.get(mismatch, {}))
    async with maker() as db:
        db.add(PaperAutoTradeLog(**original))
        await db.flush()
        db.add(PaperAutoTradeLog(**followup))
        await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=maker)
    state = send.call_args.args[0].extra["signal_audits"][0]["execution_state"]
    assert state == ("canceled" if mismatch is None else "pending")


@pytest.mark.parametrize("action,decision,status,trade_id,expected", [
    ("skip_terminal", "skipped", "risk_blocked", None, "blocked"),
    ("skip_terminal", "skipped", "rejected", None, "blocked"),
    ("skip_terminal", "skipped", "canceled", None, "canceled"),
    ("skip_terminal", "skipped", "filled", None, "unconfirmed"),
    ("skip_terminal", "skipped", None, None, "expired"),
    ("queue_buy", "wait", None, None, "pending"),
    ("buy", "executed", "partial", 123, "partial"),
    ("buy", "executed", "filled", 123, "filled"),
    ("buy", "executed", "filled", None, "unconfirmed"),
])
def test_execution_state_does_not_infer_expiry_or_full_fill(action, decision, status, trade_id, expected):
    row = SimpleNamespace(
        action=action, decision=decision, executed_trade_id=trade_id,
        candidate_json=json.dumps({"submission_order_status": status}),
        reason_code="", reason="isolated evidence")
    item = {}
    points._apply_execution_log(item, row)
    assert item["execution_state"] == expected


@pytest.mark.asyncio
async def test_highboard_later_receipt_uses_queue_order_identity(setup):
    maker, now, send = setup
    signal = await record(maker, now, account="challenger_e", queue_order=True)
    common = dict(account_id=signal.account_id, trade_date=now.date(), created_at=now,
                  source="test_route", code=signal.code, strategy_version=signal.strategy_version)
    async with maker() as db:
        db.add(PaperAutoTradeLog(**common, run_id="real-decision", action="queue_buy", decision="wait",
                                candidate_json=json.dumps({"pending_order_id": "queue-original"})))
        await db.flush()
        db.add(PaperAutoTradeLog(**common, run_id="later-queue", action="skip_buy", decision="blocked",
                                candidate_json=json.dumps({"queue_order_id": "queue-original"}),
                                reason="queue risk blocked"))
        await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=maker)
    assert send.call_args.args[0].extra["signal_audits"][0]["execution_state"] == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "filled", "partial", "partial_then_canceled", "missing_trade", "missing_fill",
    "dangling_trade_id", "other_order", "other_version", "future_log", "future_fill",
    "future_order", "future_trade", "ledger_account", "ledger_version", "ledger_quantity",
    "order_account", "order_version", "order_round",
])
async def test_cross_round_fill_uses_real_receipts_and_never_borrows(setup, case):
    from datetime import timedelta
    from app.models.paper import PaperTradeLog
    from app.models.trading import TradeOrder, TradeFill
    maker, now, send = setup
    async with maker.kw["bind"].begin() as conn:
        for model in (PaperTradeLog, TradeOrder, TradeFill):
            await conn.run_sync(model.__table__.create, checkfirst=True)
    signal = await record(maker, now)
    partial = case in {"partial", "partial_then_canceled"}
    quantity = 100 if partial else 200
    order_status = "partial" if partial else "filled"
    common = dict(account_id=signal.account_id, trade_date=now.date(), created_at=now,
                  source="test_route", code=signal.code, strategy_version=signal.strategy_version)
    async with maker() as db:
        db.add(PaperAutoTradeLog(**common, run_id="real-decision", action="deferred_buy", decision="wait",
                                candidate_json=json.dumps({"pending_order_id": "original-order"})))
        await db.flush()
        order_id = "other-order" if case == "other_order" else "original-order"
        fill_at = now + timedelta(seconds=1) if case == "future_fill" else now
        trade = PaperTradeLog(id=123, account_id=signal.account_id, code=signal.code,
            trade_type="buy", price=10.5, amount=quantity, trade_time=fill_at,
            strategy_version=signal.strategy_version, decision_round_id="round-current",
            fill_round_id="next-round")
        if case == "future_trade":
            trade.trade_time = now + timedelta(seconds=1)
        if case == "ledger_account":
            trade.account_id += 1
        if case == "ledger_version":
            trade.strategy_version = "another-version"
        if case == "ledger_quantity":
            trade.amount += 100
        if case != "missing_trade":
            db.add(trade)
        order = TradeOrder(order_id=order_id, broker="paper", account_id="default",
            code=signal.code, source="test_route", side="buy", order_type="limit", price=10.5,
            quantity=200, filled_quantity=quantity, status=order_status,
            strategy_version=signal.strategy_version, trade_date=now.date(), created_at=now,
            decision_at=now, decision_round_id="round-current")
        if case == "future_order":
            order.created_at = now + timedelta(seconds=1)
        if case == "order_account":
            order.account_id = "challenger_a"
        if case == "order_version":
            order.strategy_version = "another-version"
        if case == "order_round":
            order.decision_round_id = "another-round"
        db.add(order)
        if case != "missing_fill":
            db.add(TradeFill(fill_id="fill-original", order_id=order_id, broker="paper",
                broker_trade_id="123", code=signal.code, side="buy", price=10.5, quantity=quantity,
                trade_date=now.date(), filled_at=fill_at,
                decision_round_id="round-current", fill_round_id="next-round"))
        payload = {"deferred_order_observation": {"order_response": {
            "order_id": "original-order", "status": order_status}}}
        receipt = dict(common, run_id="next-round", action="buy", decision="executed",
            executed_trade_id=999 if case == "dangling_trade_id" else 123,
            candidate_json=json.dumps(payload))
        if case == "other_version":
            receipt["strategy_version"] = "different"
        if case == "future_log":
            receipt["created_at"] = now + timedelta(seconds=1)
        db.add(PaperAutoTradeLog(**receipt))
        await db.flush()
        if case == "partial_then_canceled":
            order.status = "canceled"
            db.add(PaperAutoTradeLog(**common, run_id="third-round", action="skip_buy",
                decision="skipped", reason_code="deferred_canceled",
                candidate_json=json.dumps({"deferred_order_observation": {"order_response": {
                    "order_id": "original-order", "status": "canceled"}}}),
                reason="remaining quantity canceled after partial fill"))
        await db.commit()
    await points.dispatch_buy_points(now=now, session_factory=maker)
    message = send.call_args.args[0]
    actual = message.extra["signal_audits"][0]["execution_state"]
    expected = {"filled": "filled", "partial": "partial", "partial_then_canceled": "canceled",
                "other_version": "pending", "future_log": "pending"}.get(case, "unconfirmed")
    assert actual == expected
    if case == "partial_then_canceled":
        assert "余量" in message.content and "不否认此前部分成交" in message.content
        assert "本轮未下单" not in message.content
        async with maker() as db:
            preserved = await db.get(PaperTradeLog, 123)
            assert preserved.amount == 100 and preserved.price == 10.5


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "legacy_missing", "consistent", "conflict_pending_queue", "conflict_response",
    "invalid_scalar", "invalid_empty", "invalid_container", "invalid_json",
    "later_conflict_filled", "later_conflict_canceled", "later_conflict_blocked",
    "later_invalid", "infinite_price",
])
async def test_conflicting_order_evidence_cannot_choose_a_valid_ledger(setup, case):
    """A valid three-ledger receipt must not rehabilitate conflicting audit IDs."""
    from app.models.paper import PaperTradeLog
    from app.models.trading import TradeOrder, TradeFill

    maker, now, send = setup
    signal = await record(maker, now)
    price = float("inf") if case == "infinite_price" else 10.5
    common = dict(account_id=signal.account_id, trade_date=now.date(), created_at=now,
                  source="test_route", code=signal.code, strategy_version=signal.strategy_version)
    payloads = {
        "legacy_missing": {},
        "consistent": {"pending_order_id": "B", "queue_order_id": "B",
                       "deferred_order_observation": {"order_response": {"order_id": "B"}}},
        "conflict_pending_queue": {"pending_order_id": "A", "queue_order_id": "B"},
        "conflict_response": {"pending_order_id": "A",
                             "deferred_order_observation": {"order_response": {"order_id": "B"}}},
        "invalid_scalar": {"pending_order_id": 123, "queue_order_id": "B"},
        "invalid_empty": {"pending_order_id": " ", "queue_order_id": "B"},
        "invalid_container": {"pending_order_id": "B", "deferred_order_observation": ["bad"]},
    }
    payload = payloads.get(case, {"pending_order_id": "B"})
    if case.startswith("later_"):
        status = {"later_conflict_canceled": "canceled", "later_conflict_blocked": "risk_blocked"}.get(case, "filled")
        payload = {"pending_order_id": 123 if case == "later_invalid" else "A",
                   "deferred_order_observation": {"order_response": {"order_id": "B", "status": status}}}
    async with maker() as db:
        db.add(PaperTradeLog(id=123, account_id=signal.account_id, code=signal.code,
            trade_type="buy", price=price, amount=200, trade_time=now,
            strategy_version=signal.strategy_version, decision_round_id="round-current",
            fill_round_id="next-round"))
        db.add(TradeOrder(order_id="B", broker="paper", account_id="default", code=signal.code,
            source="test_route", side="buy", order_type="limit", price=10.5, quantity=200,
            filled_quantity=200, status="filled", strategy_version=signal.strategy_version,
            trade_date=now.date(), created_at=now, decision_at=now, decision_round_id="round-current"))
        db.add(TradeFill(fill_id="fill-B", order_id="B", broker="paper", broker_trade_id="123",
            code=signal.code, side="buy", price=price, quantity=200, trade_date=now.date(),
            filled_at=now, decision_round_id="round-current", fill_round_id="next-round"))
        if case.startswith("later_"):
            db.add(PaperAutoTradeLog(**common, run_id="real-decision", action="deferred_buy",
                decision="wait", candidate_json=json.dumps({"pending_order_id": "B"})))
            await db.flush()
        decision = PaperAutoTradeLog(**common,
            run_id="later-round" if case.startswith("later_") else "real-decision",
            action="buy", decision="executed", executed_trade_id=123,
            candidate_json="{" if case == "invalid_json" else json.dumps(payload))
        db.add(decision)
        await db.commit()
        # Direct verifier must also fail closed, not rely only on its caller.
        verified = await points._verified_buy_fill_state(db, points._item(signal), decision, now=now)
        assert verified == ("filled" if case in {"legacy_missing", "consistent"} else None)
    result = await points.dispatch_buy_points(now=now, session_factory=maker)
    assert result["status"] == "sent"
    state = send.call_args.args[0].extra["signal_audits"][0]["execution_state"]
    assert state == ("filled" if case in {"legacy_missing", "consistent"} else "unconfirmed")
    if state == "unconfirmed":
        content = send.call_args.args[0].content
        assert "已有模拟成交记录" not in content and "已撤销余量" not in content and "已拦截" not in content


@pytest.mark.parametrize("status", ["filled", "partial", "canceled", "risk_blocked"])
def test_presentation_does_not_assign_a_terminal_state_to_conflicting_links(status):
    row = SimpleNamespace(action="buy", decision="executed", executed_trade_id=123,
        candidate_json=json.dumps({"pending_order_id": "A", "queue_order_id": "B",
                                  "submission_order_status": status}),
        reason_code="", reason="conflicting isolated evidence")
    item = {}
    points._apply_execution_log(item, row)
    assert item["execution_state"] == "unconfirmed"
