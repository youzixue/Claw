"""Invalid aggregate values must never certify market recovery or buy readiness."""
import math

import numpy as np
import pytest

from app.data.scheduler import _calculate_market_sentiment_state

BASE = dict(limit_up_count=100, limit_down_count=1, broken_limit_count=1,
            seal_rate=95, board_height=9, main_net_inflow=150,
            advance_decline_ratio=3.0, breadth_sample_count=5000,
            breadth_coverage=1.0, index_avg_change_pct=1.8,
            index_sample_count=3, turnover_total=1.5, fund_flow_coverage=1.0)


@pytest.mark.parametrize("field", tuple(BASE))
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True, np.bool_(True), 10 ** 1000],
                         ids=["nan", "inf", "negative-inf", "bool", "numpy-bool", "huge-int"])
def test_invalid_numeric_input_is_explicitly_degraded(field, value):
    state = _calculate_market_sentiment_state(**{**BASE, field: value})
    assert state["quality_status"] == "degraded"
    assert field in state["quality_reason"]
    assert state["cycle"] not in {"recovery", "climax"}
    assert math.isfinite(state["score"]) and 0 <= state["score"] <= 100
    assert math.isfinite(state["quality_completeness"]) and 0 <= state["quality_completeness"] < 1


@pytest.mark.parametrize("field,value", [
    ("main_net_inflow", None), ("turnover_total", None), ("fund_flow_coverage", None),
    ("breadth_coverage", -0.01), ("breadth_coverage", 1.01),
    ("fund_flow_coverage", -0.01), ("fund_flow_coverage", 1.01),
    ("seal_rate", -0.01), ("seal_rate", 100.01),
    ("advance_decline_ratio", -0.01), ("turnover_total", -0.01),
    ("limit_up_count", -1), ("limit_down_count", -1), ("broken_limit_count", -1),
    ("board_height", -1), ("breadth_sample_count", -1), ("index_sample_count", -1),
    ("limit_up_count", 1.5), ("index_sample_count", 2.5),
])
def test_impossible_units_and_counts_do_not_pass(field, value):
    state = _calculate_market_sentiment_state(**{**BASE, field: value})
    assert state["quality_status"] == "degraded" and field in state["quality_reason"]
    assert state["cycle"] not in {"recovery", "climax"}


@pytest.mark.parametrize("inflow,points", [(-80.000001, 9), (-80, 9), (-79.999999, 10),
                                           (0, 10), (79.999999, 10), (80, 11), (80.000001, 11)])
def test_real_zero_sign_and_original_eighty_billion_threshold_unchanged(inflow, points):
    result = _calculate_market_sentiment_state(**{**BASE, "main_net_inflow": inflow})
    assert result["quality_status"] == "ok"
    assert result["cycle_points"] == points


def test_unavailable_index_remains_degraded_without_invented_value():
    result = _calculate_market_sentiment_state(**{**BASE, "index_avg_change_pct": None})
    assert result["quality_status"] == "degraded"
    assert result["cycle"] == "divergence"


@pytest.mark.parametrize("field,value", [
    ("seal_rate", 0), ("seal_rate", 100), ("breadth_coverage", 0), ("breadth_coverage", 1),
    ("fund_flow_coverage", 0), ("fund_flow_coverage", 1), ("advance_decline_ratio", 0),
    ("turnover_total", 0), ("limit_up_count", 0), ("main_net_inflow", np.float64(0)),
])
def test_valid_unit_endpoints_not_reported_as_invalid_number(field, value):
    result = _calculate_market_sentiment_state(**{**BASE, field: value})
    assert "情绪数值无效" not in result["quality_reason"]
    assert math.isfinite(result["score"]) and math.isfinite(result["quality_completeness"])


from test_scheduler_kline_fill import scheduler_db_env


@pytest.mark.asyncio
@pytest.mark.parametrize("case,null_field", [
    ("fund_overflow", "main_net_inflow"),
    ("turnover_overflow", "turnover_total"),
    ("index_overflow", "index_avg_change_pct"),
    ("normal_zero", None),
    ("missing", "main_net_inflow"),
    ("stale", "main_net_inflow"),
])
async def test_real_snapshot_persists_unknown_and_risk_blocks_invalid_aggregate(
    scheduler_db_env, monkeypatch, case, null_field,
):
    from datetime import datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import pandas as pd
    from sqlalchemy import select
    from app.data import scheduler as module
    from app.data.scheduler import DataScheduler
    from app.models.stock import FundFlow, MarketSentiment, StockSpot, StockTag
    from app.risk.circuit_breaker import SentimentCircuitBreaker

    now = datetime.now().replace(hour=10, minute=5, second=0, microsecond=0)
    class DateTimeMeta(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)
    class FixedDateTime(datetime, metaclass=DateTimeMeta):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(module, "datetime", FixedDateTime)
    source = "eastmoney"
    async with scheduler_db_env() as db:
        for i in range(500):
            code = f"{600000 + i:06}"
            db.add(StockTag(code=code, board_type="main_sh", board_tag="tradeable",
                            is_st=False, is_suspended=False, is_delisting=False))
            db.add(StockSpot(code=code, price=10, volume=1000, change_pct=1, updated_at=now))
            if case != "missing":
                db.add(FundFlow(code=code, trade_date=now.date(),
                    main_net_inflow=1e308 if case == "fund_overflow" and i < 2 else 0,
                    main_net_inflow_pct=0, source=source,
                    source_version="individual_fund_flow_v3_f124",
                    source_quote_at=now - timedelta(seconds=3600 if case == "stale" else 2),
                    received_at=now - timedelta(seconds=3599 if case == "stale" else 1),
                    observed_at=now - timedelta(seconds=3598 if case == "stale" else 0)))
        await db.commit()
    rows = [{"代码": code, "最新价": 10, "今开": 10, "最高": 10, "最低": 9,
             "成交量": 1000, "昨收": 9.9,
             "成交额": 1e308 if case == "turnover_overflow" else 1e12,
             "涨跌幅": 1e308 if case == "index_overflow" else 1.0}
            for code in ("000001", "399001", "399006")]
    scheduler = DataScheduler()
    fetch = AsyncMock(return_value=pd.DataFrame(rows))
    scheduler._sources = {"index": SimpleNamespace(get_index_spot=fetch,
        get_index_daily=AsyncMock(side_effect=AssertionError("unexpected daily fallback")))}
    monkeypatch.setattr(module.trade_calendar, "is_trade_day", AsyncMock(return_value=True))
    monkeypatch.setattr(module.trade_calendar, "get_trade_session", lambda: "morning")
    monkeypatch.setattr(module.data_quality_guard, "record_success", AsyncMock())
    monkeypatch.setattr(module.data_quality_guard, "record_failure", AsyncMock())
    result = await scheduler._intraday_indices_and_sentiment()
    assert result["status"] == ("degraded" if null_field else "ok")
    health = scheduler.get_pipeline_runtime_status()["sentiment_funds"]
    assert health["snapshot_persisted"] is True
    if case in {"missing", "stale"}:
        assert health["status"] == "unavailable"
        assert health["qualified_count"] == 0 and health["main_net_inflow_billion"] is None
        assert health["status_counts"] == ({"stale": 500} if case == "stale" else {})
        assert health["missing_row_count"] == (500 if case == "missing" else 0)
    fetch.assert_awaited_once()
    async with scheduler_db_env() as db:
        record = (await db.scalars(select(MarketSentiment))).one()
        risk = await SentimentCircuitBreaker().get_current_state(db, trade_date=now.date())
        if null_field:
            assert getattr(record, null_field) is None  # SQL NULL, never inf or synthetic zero
            assert record.quality_status == "degraded"
            assert risk.should_block_buy is True and risk.max_position_pct == 0
        else:
            assert record.main_net_inflow == 0
            assert record.quality_status == "ok"
        for field in ("main_net_inflow", "turnover_total", "index_avg_change_pct"):
            value = getattr(record, field)
            assert value is None or math.isfinite(value)
        funds = list((await db.scalars(select(FundFlow).order_by(FundFlow.code))).all())
        assert len(funds) == (0 if case == "missing" else 500)
        if funds:
            assert funds[0].main_net_inflow == (1e308 if case == "fund_overflow" else 0)
            assert funds[0].source_quote_at == now - timedelta(seconds=3600 if case == "stale" else 2)
        if case in {"missing", "stale", "fund_overflow"}:
            from app.api.v1.sentiment import sentiment_stats
            assert risk.main_net_inflow is None
            payload = await sentiment_stats(trade_date=now.date().isoformat(), db=db)
            assert payload["stats"]["main_net_inflow"] is None
            assert record.calculation_version == "breadth_index_quality_v4_nullable_funds"
