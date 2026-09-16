"""并列报告明确隔离样本分母，测试只使用本地隔离SQLite。"""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select, func

from app.api.v1 import paper
from app.config.settings import settings
from app.models.paper import PaperAccount, PaperTradeLog
from app.models.trading import TradeFill, TradeOrder
from app.paper import signal_research as research
from test_paper_signal_research import db, log_row, DAY, AT


@pytest.mark.asyncio
async def test_empty_parallel_report_preserves_null_metrics_and_missing_market_control(db):
    result = await research.build_parallel_research_report(db, start_date=DAY, end_date=DAY, as_of=AT)
    assert len(result["paired_accounts"]) == 12
    assert result["comparison_contract"]["same_sample_denominator"] is False
    assert result["comparison_contract"]["capacity_policy_evaluated"] is False
    assert result["comparison_contract"]["full_market_same_shape"]["sample_count"] is None
    assert all(r["winner"] is None and r["actual_win_rate"] is None for r in result["paired_accounts"])
    assert await db.scalar(select(func.count(PaperAccount.id))) == 0
    assert not db.new and not db.dirty


@pytest.mark.asyncio
async def test_current_instance_version_is_not_merged_with_old_signals_or_realized_returns(db, monkeypatch):
    monkeypatch.setattr(paper, "_strategy_version", lambda name: "v1")
    monkeypatch.setattr(settings, "PAPER_EXPERIMENT_START_DATE", DAY.isoformat())
    db.add_all([PaperAccount(id=1, account_name="default", status="closed", initial_capital=50000),
                PaperAccount(id=2, account_name="default", status="active", initial_capital=50000),
                PaperAccount(id=3, account_name="default", status="active", initial_capital=50000)])
    db.add_all([log_row(id=1, account_id=1), log_row(id=2, account_id=3, version="old"),
                log_row(id=3, account_id=3), log_row(id=4, account_id=2)])
    exit_at = AT + timedelta(days=1)
    db.add_all([PaperTradeLog(id=101, account_id=3, code="600001", trade_type="buy", amount=100,
                    price=10, commission=5, tax=0, trade_time=AT, strategy_version="v1"),
                PaperTradeLog(id=102, account_id=3, code="600001", trade_type="sell", amount=100,
                    price=11, commission=5, tax=1, trade_time=exit_at, strategy_version="v1")])
    entry = {"active": True, "strategy_version": "v1", "entry_regime": {"label": "unknown"}}
    db.add(TradeOrder(order_id="entry", account_id="default", broker="paper", code="600001",
        side="buy", source="test", strategy_version="v1", decision_round_id="round-1",
        decision_at=AT, as_of_at=AT, created_at=AT, trade_date=DAY,
        order_type="limit", quantity=100, price=10, status="filled",
        risk_json=json.dumps({"experiment_entry": entry})))
    db.add(TradeFill(fill_id="entry-fill", order_id="entry", broker="paper", broker_trade_id="101",
        code="600001", side="buy", quantity=100, price=10, commission=5, tax=0, filled_at=AT))
    await db.flush()
    before_exit = await research.build_parallel_research_report(db, start_date=DAY, end_date=DAY, as_of=AT)
    before_row = next(r for r in before_exit["paired_accounts"] if r["account_name"] == "default")
    assert before_row["actual_closed_round_trips"] == 0 and before_row["actual_open_round_trips"] == 1
    kwargs = dict(start_date=DAY, end_date=DAY, as_of=exit_at+timedelta(hours=6))
    result = await research.build_parallel_research_report(db, **kwargs)
    row = next(r for r in result["paired_accounts"] if r["account_name"] == "default")
    assert row["all_recorded_signals"]["confirmation_events"] == 4
    assert row["current_instance_version_signals"]["confirmation_events"] == 1
    assert row["actual_account_id"] == 3
    assert row["actual_closed_round_trips"] == 1 and row["actual_realized_net_pnl"] == 89
    assert row["actual_win_rate"] == 1 and row["performance_difference"] is None
    assert row["winner"] is None
    assert result["comparison_contract"]["portfolio_scope_start"] == DAY.isoformat()
    assert result["comparison_contract"]["historical_configuration_reconstructed"] is False
    assert not db.new and not db.dirty
    # Signal evidence is unchanged but actual realized fees change the combined content hash.
    fill = await db.get(PaperTradeLog, 102)
    fill.commission = 6
    await db.flush()
    changed = await research.build_parallel_research_report(db, **kwargs)
    assert changed["dataset_sha256"] == result["dataset_sha256"]
    assert changed["parallel_dataset_sha256"] != result["parallel_dataset_sha256"]


@pytest.mark.asyncio
async def test_bad_range_fails_before_portfolio(db, monkeypatch):
    from app.paper import experiment_report
    from unittest.mock import AsyncMock
    spy = AsyncMock(side_effect=AssertionError("must not read portfolio for invalid input"))
    monkeypatch.setattr(experiment_report, "build_experiment_report", spy)
    with pytest.raises(ValueError, match="interval"):
        await research.build_parallel_research_report(db, start_date=DAY, end_date=DAY+timedelta(days=1), as_of=AT)
    assert not spy.called
