"""真实交易函数挂钩测试：隔离SQLite、时点行情与假投递器，绝不真实发消息。"""
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.api.v1 import paper
from app.config.settings import settings
from app.models.paper import PaperAutoTradeLog, PaperPosition, PaperTradeLog
from app.models.stock import StockSpot, StockTag
from app.push import paper_buy_points as points
from app.paper import strategy_iteration_challenger as challenger
from test_paper_api import paper_client
from test_strategy_iteration_challenger import challenger_env, _seed_confirmed, _snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", [False, True, "zero_vwap"])
async def test_t_buyback_alert_before_cash_but_not_with_incomplete_evidence(paper_client, monkeypatch, incomplete):
    _, maker = paper_client
    at, send, submit = clock_and_guards(monkeypatch)
    monkeypatch.setattr(settings, "PAPER_AUTO_TRADE_T_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_AUTO_T_BUYBACK_MAX_PER_DAY", 1)
    monkeypatch.setattr(paper, "_build_short_sell_context", AsyncMock(return_value={
        "avg_price":0 if incomplete == "zero_vwap" else 9.99,
        "min5_change":None if incomplete is True else .2,
        "orderbook_imbalance":.1, "sector_retreat_reason":"",
    }))
    quote = main_quote(at)
    quote.update(quote_round_id="t-round", source_quote_at=at, received_at=at)
    token = paper._QUOTE_ROUND_CONTEXT.set({"round_id":"t-round", "as_of_at":at,
        "committed_at":at, "quality_status":"ok", "records":[quote]})
    try:
        async with maker() as db:
            account = await paper._get_or_create_account(db, "default")
            account.current_capital = 1
            db.add_all([
                StockTag(code="600888", board_type="main_sh", board_tag="tradeable"),
                PaperPosition(account_id=account.id, code="600888", name="隔离T回补",
                    buy_price=10.5, buy_amount=100, buy_time=at-timedelta(days=1),
                    strategy_version=paper._strategy_version("default"), is_closed=False),
                PaperTradeLog(account_id=account.id, code="600888", trade_type="sell",
                    price=10.5, amount=100, trade_time=at-timedelta(minutes=5)),
            ])
            await db.commit()
            decision_logs = await paper._run_auto_t_buybacks(db, account=account,
                run_id="t-decision", trade_date=at.date(), trigger="test", execute=True)
            await db.commit()
            rows = list((await db.scalars(select(PaperAutoTradeLog).where(
                PaperAutoTradeLog.action == points.SIGNAL))).all())
        assert len(rows) == int(not incomplete)
        assert not submit.called and not send.called
        assert any("资金不足" in row.reason for row in decision_logs)
        if rows:
            await points.dispatch_buy_points(now=at, session_factory=maker)
            content = send.call_args.args[0].content
            assert "做T回补" in content and "资金不足" in content and "已有模拟成交" not in content
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


MAIN = [
    ("default", "next_day_plan"), ("promotion", "promotion_promotion"),
    ("mainline", "promotion_mainline"), ("auction", "promotion_auction"),
    ("tenbagger", "tenbagger_midline"), ("reversal", "reversal_pullback"),
    ("challenger_e", "tenbagger_midline"),
]
EVENT = [
    ("challenger_a", "momentum_first_retest"),
    ("challenger_b", "b_weak_open_second_board"),
    ("challenger_c", "c_recent_limit_relaunch"),
    ("challenger_d", "d_auction_recovery"),
    ("challenger_f2", "f2_highboard_break_reclaim"),
]


def clock_and_guards(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 8, 10, 0)
    at = Clock.now()
    monkeypatch.setattr(points, "datetime", Clock)
    monkeypatch.setattr(paper, "_paper_now", lambda: at)
    monkeypatch.setattr(settings, "PAPER_BUY_POINT_PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_CONTINUOUS_EXPERIMENT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_ACTIVATION_AT", "2026-09-08T00:00:00")
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_START_DATE", "2026-09-08")
    monkeypatch.setattr(settings, "PAPER_AUTO_TRADE_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_CHALLENGER_ACCOUNT_ENABLED", True)
    monkeypatch.setattr(paper, "_paper_order_window_status", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(paper, "_should_run_intraday_auto_trade", AsyncMock(return_value=(True, "")))
    send = AsyncMock(return_value={"sent":True, "channels":{"feishu":True}, "status":"sent"})
    monkeypatch.setattr(points.push_scheduler, "push_to_channels", send)
    # 已替换成假投递器；显式开启测试分支，不继承部署/离线运行器的开关。
    monkeypatch.setattr(settings, "PUSH_ENABLED", True)
    # 所有用例都故意被预算/持仓拦截，不应走到下单。
    import app.trading.service as trading
    submit = AsyncMock(side_effect=AssertionError("测试只验证买点，不允许下单或联网"))
    monkeypatch.setattr(trading, "submit_order", submit)
    return at, send, submit


def main_quote(at):
    return dict(code="600888", name="隔离买点", price=10.0, prev_close=9.95,
        open=9.95, high=10.03, low=9.94, avg_price=9.99, change_pct=.5,
        volume_ratio=1.2, ask1_price=10.01, ask1_volume=500,
        bid1_price=9.99, bid1_volume=500, orderbook_imbalance=.1,
        limit_up=10.95, limit_down=8.95, updated_at=at)


async def run_main(maker, monkeypatch, account_name, source, *, gate="", block="daily", execute=True):
    at, send, submit = clock_and_guards(monkeypatch)
    candidate = dict(code="600888", name="隔离买点", _source=source,
        signal_source=source, total_score=95, change_pct=.5, stop_loss_price=9.2,
        trade_gate_passed=True, actionable=True, watch_only=False,
        strategy_label="真实形态测试", entry_condition="已完成策略条件验证",
        buy_point_type="支撑确认", buy_point_reasons=["分时支撑收复"])
    if account_name in {"tenbagger", "challenger_e"}:
        from app.paper.experiment import HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION
        candidate.update(
            entry_mode_contract=HIGHBOARD_ENTRY_MODE_CONTRACT_VERSION,
            entry_variant="e2_strong_entry" if account_name == "challenger_e" else "e_low_entry",
        )
    factory = AsyncMock(side_effect=lambda *args, **kwargs: ([dict(candidate)], []))
    for name in ("_paper_auto_buy_candidates", "_promotion_route_buy_candidates",
                 "_tenbagger_midline_candidates", "_reversal_pullback_candidates"):
        monkeypatch.setattr(paper, name, factory)
    monkeypatch.setattr(paper, "_champion_intraday_confirmation_status",
                        AsyncMock(return_value=(gate != "quote", 3, 60)))
    monkeypatch.setattr(paper, "_candidate_execution_value_reject_reason",
                        lambda *args, **kwargs: "性价比未达标" if gate == "value" else "")
    monkeypatch.setattr(paper, "_continuation_risk_reject_reason",
                        AsyncMock(return_value="延续性不达标" if gate == "continuation" else ""))
    monkeypatch.setattr(paper, "_strategy_buy_limits", lambda name: (0, 10) if block == "daily" else (10, 0) if block == "position" else (10, 10))
    if block == "risk":
        monkeypatch.setattr(paper, "_risk_check_for_buy", AsyncMock(return_value={
            "final_level":"block", "block_reasons":[{"message":"测试风险限制"}]}))
    quote = main_quote(at)
    quote.update(quote_round_id="main-round", source_quote_at=at, received_at=at)
    if gate == "stale":
        quote["updated_at"] = at - timedelta(minutes=10)
    if gate == "vwap":
        quote["avg_price"] = 10.10
    token = paper._QUOTE_ROUND_CONTEXT.set({"round_id":"main-round", "as_of_at":at,
        "committed_at":at, "quality_status":"ok", "records":[quote], "code_version":"test-code"})
    try:
        async with maker() as db:
            db.add(StockTag(code="600888", name="隔离买点", board_type="main_sh", board_tag="tradeable"))
            db.add(StockSpot(**quote))
            await db.commit()
            account = await paper._get_or_create_account(db, account_name)
            if block == "cash":
                account.initial_capital = account.current_capital = account.total_assets = 1
                await db.commit()
            result = await paper.run_paper_auto_trade(db, account_name=account_name, now=at,
                execute=execute, execution_mode="intraday", include_position_risk=False)
            rows = list((await db.scalars(select(PaperAutoTradeLog).where(
                PaperAutoTradeLog.action == points.SIGNAL))).all())
        assert not send.called
        assert not submit.called
        return at, rows, result, send
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("account_name,source", MAIN)
async def test_seven_account_hooks_record_full_signal_before_daily_cap(paper_client, monkeypatch, account_name, source):
    _, maker = paper_client
    at, rows, result, send = await run_main(maker, monkeypatch, account_name, source)
    assert len(rows) == 1, json.dumps(result["logs"], ensure_ascii=False, indent=2)
    assert rows[0].strategy_version == paper._strategy_version(account_name)
    payload = json.loads(rows[0].candidate_json)
    assert payload["account_name"] == account_name
    assert payload["account_role"] == ("次账户" if account_name == "challenger_e" else "主账户")
    assert any("策略日限" in row["reason"] for row in result["logs"])
    await points.dispatch_buy_points(now=at, session_factory=maker)
    assert send.call_count == 1
    assert paper._strategy_display_meta(type("A", (), {"account_name":account_name})())["label"] in send.call_args.args[0].content
    assert "策略日限" in send.call_args.args[0].content
    assert "已有模拟成交" not in send.call_args.args[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["quote", "value", "continuation", "stale", "vwap"])
async def test_quote_only_or_failed_strategy_gate_never_queues_buy_point(paper_client, monkeypatch, gate):
    _, maker = paper_client
    _, rows, result, _ = await run_main(maker, monkeypatch, "default", "next_day_plan", gate=gate)
    assert rows == [], result["logs"]
    if gate in {"value", "continuation"}:
        assert any(row["decision"] == "quote_confirmed" for row in result["logs"])


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["cash", "position", "risk"])
async def test_main_buy_point_survives_portfolio_and_risk_blocks(paper_client, monkeypatch, block):
    _, maker = paper_client
    at, rows, result, send = await run_main(maker, monkeypatch, "default", "next_day_plan", block=block)
    assert len(rows) == 1, json.dumps(result["logs"], ensure_ascii=False, indent=2)
    await points.dispatch_buy_points(now=at, session_factory=maker)
    text = send.call_args.args[0].content
    assert {"cash":"资金不足", "position":"最大持仓数", "risk":"测试风险限制"}[block] in text
    assert "已有模拟成交" not in text


@pytest.mark.asyncio
async def test_manual_dry_run_does_not_create_real_notification(paper_client, monkeypatch):
    _, maker = paper_client
    _, rows, _, send = await run_main(maker, monkeypatch, "default", "next_day_plan", execute=False)
    assert rows == [] and not send.called


@pytest.mark.asyncio
@pytest.mark.parametrize("account_name,route", EVENT)
@pytest.mark.parametrize("invalid", [False, True])
async def test_five_event_accounts_revalidate_before_alert_and_alert_before_budget(
        challenger_env, monkeypatch, account_name, route, invalid):
    at, send, submit = clock_and_guards(monkeypatch)
    # 完整入场条件由真实_live_route_confirmation_valid执行，只有账户日限置零。
    monkeypatch.setattr(paper, "_strategy_buy_limits", lambda name: (0, 10))
    price, ask = (10.4, 10.41) if account_name == "challenger_a" else (10.05, 10.06)
    quote = json.loads(_snapshot("600101", at, route_id=route, price=price, ask=ask))["quote"]
    quote.update(updated_at=at, quote_round_id="event-round", source_quote_at=at, received_at=at)
    if invalid:
        quote["avg_price"] = price + .2
    token = paper._QUOTE_ROUND_CONTEXT.set({"round_id":"event-round", "as_of_at":at,
        "committed_at":at, "quality_status":"ok", "records":[quote]})
    try:
        async with challenger_env() as db:
            await _seed_confirmed(db, code="600101", route_id=route, now=at, price=price, ask=ask)
            result = await challenger.run_strategy_iteration_challenger_accounts(db, now=at)
            await db.commit()
            rows = list((await db.scalars(select(PaperAutoTradeLog).where(
                PaperAutoTradeLog.action == points.SIGNAL))).all())
            reasons = list((await db.scalars(select(PaperAutoTradeLog.reason).where(PaperAutoTradeLog.code == "600101"))).all())
        assert len(rows) == (0 if invalid else 1), (result, reasons)
        assert not submit.called and not send.called
        if rows:
            assert json.loads(rows[0].candidate_json)["account_name"] == account_name
            assert "再次校验通过" in rows[0].reason
            await points.dispatch_buy_points(now=at, session_factory=challenger_env)
            assert send.call_count == 1
            assert "最大持仓数" in send.call_args.args[0].content
            assert "已有模拟成交" not in send.call_args.args[0].content
    finally:
        paper._QUOTE_ROUND_CONTEXT.reset(token)
