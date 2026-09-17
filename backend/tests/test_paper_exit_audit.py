"""退出审计不会变更卖点、绕过T+1，且不把买前日高当持仓浮盈。"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.api.v1 import paper
from app.models.paper import PaperAutoTradeLog, PaperPosition
from app.paper import exit_audit
from test_paper_api import paper_client

AT = datetime(2026, 9, 8, 10, 0)


def position(**kw):
    return SimpleNamespace(**({"id": 1, "account_id": 8, "code": "600791", "name": "测试股",
                              "buy_time": AT - timedelta(minutes=25), "buy_price": 8.13,
                              "strategy_version": "entry-v1"} | kw))


def spot(**kw):
    return SimpleNamespace(**({"price": 8.07, "high": 8.30,
                              "source_quote_at": AT - timedelta(seconds=4),
                              "received_at": AT - timedelta(seconds=1),
                              "quote_round_id": "qr-test"} | kw))


def step(previous=None, *, pos=None, quote=None, now=AT, ok=True):
    return exit_audit.advance_extrema(previous, position=pos or position(),
        spot=quote or spot(), observed_at=now, quote_ok=ok, max_age_sec=90)


def test_session_high_is_not_post_entry_high_and_rule_is_unchanged():
    pos = position(stop_loss_price=7.78)
    state, changed = step(pos=pos)
    assert changed and state["post_entry_high"] == 8.07
    assert state["post_entry_high"] != 8.30
    # 2026-09-17 改3：弱信号 rung 现要求 ≥2 项独立走弱证据；断言的是
    # "session_high 不是 post_entry_high" 与触发文案，故补证据而不改断言。
    ctx = {"price": 8.07, "high": 8.30, "open": 7.98, "change_pct": .75,
           "limit_down": 7.21, "stop_loss_price": 7.78, "close_position": .5,
           "ma5": 8.20, "orderbook_imbalance": -0.5}
    exit_audit.attach_exit_audit(ctx, position=pos, extrema=state, quote_ok=True)
    reason = paper._short_sell_reason(pos, ctx, -.738, 0, AT.date(), AT,
                                      params={"pullback_from_high_pct": 2.5})
    assert reason.startswith("盘中冲高回落")
    assert "当日日高相对成本2.09%" in reason
    assert "非持仓最高浮盈" in reason
    assert ctx["session_high"] == 8.30 and ctx["post_entry_high"] == 8.07
    assert ctx["exit_high_basis"] == "session_high_frozen_rule_unchanged"


@pytest.mark.parametrize("bad", [
    {"source_quote_at": AT - timedelta(hours=1)},  # 买入前
    {"source_quote_at": AT + timedelta(seconds=1)},
    {"received_at": AT + timedelta(seconds=1)},
    {"source_quote_at": AT.replace(tzinfo=timezone.utc)},
    {"source_quote_at": None}, {"received_at": None}, {"quote_round_id": ""},
    {"price": float("nan")}, {"price": float("inf")}, {"price": -1},
    {"source_quote_at": AT - timedelta(seconds=100)},
])
def test_invalid_observation_never_updates_peak(bad):
    state, changed = step(quote=spot(**bad))
    assert not changed and state["post_entry_high"] is None


def test_duplicate_source_and_bad_quote_do_not_advance_extrema():
    initial, _ = step()
    duplicate, changed = step(initial, quote=spot(price=9))
    assert not changed and duplicate["post_entry_high"] == 8.07
    bad, changed = step(initial, quote=spot(price=9), ok=False)
    assert not changed and bad["post_entry_high"] == 8.07


@pytest.mark.parametrize("identity", [{"id": 2}, {"account_id": 9}, {"code": "600001"},
    {"strategy_version": "entry-v2"}, {"buy_time": AT - timedelta(minutes=5)}])
def test_other_account_position_version_never_supplies_peak(identity):
    initial, _ = step(quote=spot(price=12))
    state, _ = step(initial, pos=position(**identity))
    assert state["post_entry_high"] == 8.07


def test_future_state_not_restored_and_cost_change_does_not_rewrite_old_profit():
    initial, _ = step(quote=spot(price=9))
    future = dict(initial, last_observed_at=(AT + timedelta(hours=1)).isoformat())
    state, _ = step(future)
    assert state["post_entry_high"] == 8.07
    after, changed = step(initial, pos=position(buy_price=7),
        quote=spot(price=8, source_quote_at=AT+timedelta(seconds=26),
                   received_at=AT+timedelta(seconds=29)), now=AT+timedelta(seconds=30))
    assert changed and after["post_entry_high"] == 9
    assert after["observed_max_profit_pct"] == pytest.approx((8/7-1)*100)
    assert after["max_profit_cost_basis"] == 7


@pytest.mark.asyncio
async def test_valid_iso_source_clock_is_normalized_before_persistence(paper_client):
    _, maker = paper_client
    async with maker() as db:
        source = (AT - timedelta(seconds=4)).isoformat()
        state = await exit_audit.observe_position_extrema(db, position=position(),
            spot=spot(source_quote_at=source), observed_at=AT, quote_ok=True,
            max_age_sec=90, persist=True)
        await db.commit()
        row = await db.scalar(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.stage_code == "position_extrema"))
        assert row.as_of_at == AT - timedelta(seconds=4)
        assert state["post_entry_high"] == 8.07


@pytest.mark.asyncio
async def test_append_only_restore_dry_run_and_late_start_are_explicit(paper_client):
    _, maker = paper_client
    pos = position()
    async with maker() as db:
        first = await exit_audit.observe_position_extrema(db, position=pos, spot=spot(),
            observed_at=AT, quote_ok=True, max_age_sec=90, persist=True)
        await db.commit()
    async with maker() as db:
        second = await exit_audit.observe_position_extrema(db, position=pos,
            spot=spot(price=8.16, source_quote_at=AT+timedelta(seconds=26),
                      received_at=AT+timedelta(seconds=29)),
            observed_at=AT+timedelta(seconds=30), quote_ok=True, max_age_sec=90, persist=True)
        await db.commit()
        raw = list((await db.scalars(select(PaperAutoTradeLog).where(
            PaperAutoTradeLog.stage_code == "position_extrema").order_by(PaperAutoTradeLog.id))).all())
        assert len(raw) == 2 and json.loads(raw[0].candidate_json)["post_entry_high"] == 8.07
        assert second["post_entry_high"] == 8.16
        assert first["history_before_first_observation"] == "unknown_not_backfilled"
        await exit_audit.observe_position_extrema(db, position=pos,
            spot=spot(price=9, source_quote_at=AT+timedelta(seconds=56),
                      received_at=AT+timedelta(seconds=59)),
            observed_at=AT+timedelta(seconds=60), quote_ok=True, max_age_sec=90, persist=False)
        await db.commit()
    async with maker() as db:
        restored = await exit_audit.observe_position_extrema(db, position=pos, spot=spot(),
            observed_at=AT+timedelta(seconds=90), quote_ok=False, max_age_sec=90, persist=True)
        assert restored["post_entry_high"] == 8.16


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected", [("t1", "t_plus_one"), ("quote", "quote_invalid"),
    ("depth", "orderbook_unfillable"), ("reject", "order_not_accepted"),
    ("pending", ""), ("error", "execution_error"), ("audit_error", "t_plus_one"),
    ("database", "transaction_abort"), ("audit_database", "transaction_abort")])
async def test_trigger_survives_all_execution_blocks(paper_client, monkeypatch, mode, expected):
    from app.trading import service
    _, maker = paper_client
    async def build(*_args):
        # 2026-09-17 改3：弱信号 rung 现要求 ≥2 项独立走弱证据
        return {"price": 8.07, "high": 8.30, "open": 7.98, "change_pct": .75,
                "stop_loss_price": 7.78, "close_position": .5,
                "ma5": 8.20, "orderbook_imbalance": -0.5}
    async def fetch(*_args): return spot()
    async def available(*_args): return 0 if mode in {"t1", "audit_error"} else 100
    async def stats(*_args): return {"amount": 0}
    async def active(*_args, **_kw): return False
    async def submit(*_args, **_kw):
        command = _args[1]
        assert command.strategy_version == "v1"
        assert command.deferred_metadata["exit_decision_strategy_version"] == paper._strategy_version("challenger_b")
        assert command.deferred_metadata["position_id"] == pos.id
        if mode == "database":
            raise OperationalError("fixture", {}, sqlite3.OperationalError("database is locked"))
        if mode == "error": raise RuntimeError("test")
        return {"order": {"status": "submitted" if mode == "pending" else "risk_blocked",
                          "order_id": "test-order", "error_message": "test risk"}, "fills": []}
    monkeypatch.setattr(paper, "_build_short_sell_context", build)
    monkeypatch.setattr(paper, "_spot_by_code", fetch)
    monkeypatch.setattr(paper, "_available_sell_amount", available)
    monkeypatch.setattr(paper, "_today_sell_stats", stats)
    monkeypatch.setattr(paper, "_has_active_paper_order", active)
    monkeypatch.setattr(paper, "_execution_quote_status", lambda *_a, **_k: (mode != "quote", "stale"))
    monkeypatch.setattr(paper, "_conservative_execution_price", lambda *_a: None if mode == "depth" else 8.06)
    monkeypatch.setattr(service, "submit_order", submit)
    if mode in {"audit_error", "audit_database"}:
        async def broken(*_args, **_kw):
            if mode == "audit_database":
                raise OperationalError("fixture", {}, sqlite3.OperationalError("database is locked"))
            raise ValueError("audit only")
        monkeypatch.setattr(exit_audit, "observe_position_extrema", broken)
    async with maker() as db:
        acct = await paper._get_or_create_account(db, "challenger_b")
        pos = PaperPosition(account_id=acct.id, code="600791", name="测试", buy_price=8.13,
            buy_amount=100, buy_time=AT-timedelta(minutes=25), current_price=8.07,
            profit_pct=-.738, hold_days=0, stop_loss_price=7.78, strategy_version="v1", is_closed=False)
        db.add(pos)
        await db.flush()
        if expected == "transaction_abort":
            failure_log = AsyncMock(side_effect=AssertionError("must not log through failed transaction"))
            monkeypatch.setattr(paper, "_add_auto_log", failure_log)
            with pytest.raises(OperationalError):
                await paper._run_auto_sells(db, account=acct, run_id="audit-test", trade_date=AT.date(),
                    trigger="test", execute=True, quote_now=AT)
            assert failure_log.await_count == 0
            await db.rollback()
            return
        logs = await paper._run_auto_sells(db, account=acct, run_id="audit-test", trade_date=AT.date(),
            trigger="test", execute=True, quote_now=AT)
        await db.commit()
        assert len(logs) == 1
        payload = json.loads(logs[0].candidate_json)
        assert payload["exit_trigger_reason"].startswith("盘中冲高回落")
        assert payload["execution_block_code"] == expected
        assert payload["post_entry_high"] != 8.30
        assert logs[0].executed_trade_id is None
        if mode in {"t1", "audit_error"}:
            assert "T+1" in logs[0].reason
            assert "T+1" in payload["execution_block_reason"]
        if mode == "pending":
            assert payload["exit_execution_status"] == "order_pending"
            assert logs[0].action == "deferred_sell"
        if mode == "audit_error":
            assert payload["position_extrema"]["coverage"] == "audit_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("event,expected", [("filled", "filled"), ("partial", "partially_filled"),
                                           ("waiting", "order_pending"), ("rejected", "blocked")])
async def test_next_round_fill_keeps_original_exit_trigger(paper_client, monkeypatch, event, expected):
    from app.trading import service
    _, maker = paper_client
    original = {"exit_trigger_reason": "原始冲高回落", "exit_execution_status": "submitting"}
    async def reconcile(*_args, **_kw):
        return [{"event": event, "order": {"side": "sell", "code": "600791", "order_id": "test",
                    "status": event, "price": 8.06, "reason": "卖出"},
                 "fills": [{"quantity": 100, "price": 8.06, "broker_trade_id": "123"}]
                          if event in {"filled", "partial"} else [],
                 "deferred": {"candidate": original}, "reason": "本轮撮合状态"}]
    monkeypatch.setattr(service, "reconcile_paper_deferred_orders", reconcile)
    async with maker() as db:
        account = await paper._get_or_create_account(db, "challenger_b")
        rows = await paper._reconcile_deferred_order_logs(db, account=account, run_id="fill-test",
                    trade_date=AT.date(), trigger="test", observed_at=AT)
        assert len(rows) == 1
        payload = json.loads(rows[0].candidate_json)
        assert payload["exit_trigger_reason"] == "原始冲高回落"
        assert payload["exit_execution_status"] == expected
        assert original["exit_execution_status"] == "submitting"
        assert (rows[0].executed_trade_id is not None) == (event in {"filled", "partial"})
