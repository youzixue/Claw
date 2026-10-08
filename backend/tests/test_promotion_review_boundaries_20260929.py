"""Research read-view boundaries; isolated SQLite only, with network denied."""
from datetime import date, datetime, timedelta
import json
import socket

import pytest
from sqlalchemy import delete, select

from app.api.v1 import promotion
from app.data.limit_pool import supplement_wencai_limit_details
from app.models.governance import TradeCalendarModel
from app.models.stock import LimitUpPool, QuoteRound, StockKline
from test_promotion_api import promotion_api_env, disable_live_dragon_tiger_lookup
from test_promotion_review_evidence import record
from test_limit_pool_pipeline_20260928 import save, spot, DETAIL

BEFORE = date(2026, 9, 28)
AFTER = date(2026, 9, 29)
CLOSE = datetime(2026, 9, 29, 15, 0, 1)
AS_OF = datetime(2026, 9, 29, 16)


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network is forbidden in review boundary tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


async def calendar(session, days=(BEFORE, AFTER)):
    session.add_all(TradeCalendarModel(trade_date=day, is_trade_day=day.weekday() < 5) for day in days)
    await session.commit()


async def formal_prediction(session, *, day=BEFORE, actionable=False):
    prediction = record(day)
    factors = promotion._json_loads_safe(prediction.factors_json)
    factors["prediction_actionable"] = actionable
    prediction.factors_json = promotion._json_dumps_safe(factors)
    session.add(prediction)
    session.add_all([
        StockKline(code="600001", trade_date=day, close=10, volume=100, source="ths"),
        StockKline(code="600001", trade_date=day + timedelta(days=1), close=11,
                   prev_close=10, change_pct=10, volume=100, source="ths"),
    ])
    await session.commit()


async def new_pool(session, *, kind="complete", at=CLOSE):
    rows = [spot("600001", source_at=at)]
    if kind == "empty":
        rows = [spot("600001", price=10.5, high=10.6, source_at=at)]
    await save(session, rows, at=at)
    if kind == "complete":
        await supplement_wencai_limit_details(
            session, {"600001": DETAIL}, requested_at=at, observed_at=at + timedelta(seconds=10),
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("predicted", [False, True])
async def test_verified_direct_height_does_not_require_previous_pool(promotion_api_env, predicted):
    Session, _ = promotion_api_env
    async with Session() as session:
        await calendar(session)
        if predicted:
            await formal_prediction(session)
        await new_pool(session)
        result = await promotion._build_promotion_daily_learning_review(session, now=AS_OF)
    assert result["outcome_universe_scope"] == "current_risk_filtered_main_board"
    assert any("非PIT" in note for note in result["notes"])
    assert any("market_target_limit_up_count" in note and "非主板观察范围" in note for note in result["notes"])
    assert any("actual_rising_count" in note and "不经过当前数据库StockTag过滤" in note for note in result["notes"])
    for row in (result["latest"], result["aggregate"]):
        assert row["outcome_universe_scope"] == "current_risk_filtered_main_board"
        assert row["actual_target_limit_up_count"] == 1
        assert row["market_target_limit_up_count"] == 1
        assert row["predicted_count"] == int(predicted)
        assert row["predicted_limit_up_hit_count"] == (1 if predicted else None)
        assert row["limit_up_precision"] == (1 if predicted else None)
        assert "prediction_limit_up_pool_missing" not in row["evaluation_reasons"]
        if not predicted:
            assert row["directional_coverage"] is None
            assert row["predicted_rising_hit_count"] is None
            assert row["average_predicted_probability"] is None
    lane = result["latest"]["lane_metrics"]["target_1"]
    assert lane["actual_count"] == 1
    assert lane["hit_count"] == (1 if predicted else None)
    assert lane["precision"] == (1 if predicted else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["incomplete", "orphan", "missing_members"])
async def test_partial_new_pool_is_unknown_even_with_raw_rows(promotion_api_env, kind):
    Session, _ = promotion_api_env
    async with Session() as session:
        await calendar(session)
        await formal_prediction(session, actionable=True)
        session.add(LimitUpPool(code="600099", trade_date=BEFORE, consecutive_days=1, source="test"))
        await session.commit()
        await new_pool(session, kind="incomplete" if kind == "incomplete" else "complete")
        if kind == "orphan":
            await session.execute(delete(QuoteRound))
        elif kind == "missing_members":
            await session.execute(delete(LimitUpPool).where(LimitUpPool.trade_date == AFTER))
        await session.commit()
        result = await promotion._build_promotion_daily_learning_review(session, now=AS_OF)
    for row in (result["latest"], result["aggregate"]):
        assert row["actual_target_limit_up_count"] is None
        assert row["predicted_limit_up_hit_count"] is None
        assert row["limit_up_precision"] is None
        assert row["brier_score"] is None
        assert row["actionable_limit_up_hit_count"] is None
        assert row["underestimated_limit_up_hit_count"] is None
        assert row["average_predicted_probability"] == .5
        assert row["directional_precision"] == 1
        assert "outcome_limit_up_pool_incomplete" in row["evaluation_reasons"]
    assert result["latest"]["lane_metrics"]["target_1"]["hit_count"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [True, 1.0, "1", None, -1])
async def test_verified_pool_member_count_requires_nonnegative_integer(promotion_api_env, count):
    Session, _ = promotion_api_env
    async with Session() as session:
        await calendar(session)
        await formal_prediction(session)
        await new_pool(session)
        quote = await session.scalar(select(QuoteRound))
        evidence = json.loads(quote.component_watermarks_json)
        evidence["limit_pool"]["up_count"] = count
        quote.component_watermarks_json = json.dumps(evidence)
        await session.commit()
        result = await promotion._build_promotion_daily_learning_review(session, now=AS_OF)
    assert result["latest"]["actual_target_limit_up_count"] is None
    assert "outcome_limit_up_pool_incomplete" in result["latest"]["evaluation_reasons"]


@pytest.mark.asyncio
async def test_certified_empty_pool_is_zero_but_zero_denominator_rates_are_unknown(promotion_api_env):
    Session, _ = promotion_api_env
    async with Session() as session:
        await calendar(session)
        await formal_prediction(session)
        await new_pool(session, kind="empty")
        result = await promotion._build_promotion_daily_learning_review(session, now=AS_OF)
    for row in (result["latest"], result["aggregate"]):
        assert row["actual_target_limit_up_count"] == 0
        assert row["predicted_limit_up_hit_count"] == 0
        assert row["limit_up_precision"] == 0
        assert row["limit_up_recall"] is None
        assert row["actionable_limit_up_precision"] is None
        assert row["actionable_limit_up_hit_count"] is None
        assert row["brier_score"] == .25
    for lane in result["latest"]["lane_metrics"].values():
        assert lane["actual_count"] == 0
        assert lane["recall"] is None
        assert lane["pool_recall"] is None
        assert lane["recall_precision"] is None
        assert lane["recall_recall"] is None
        assert lane["actionable_precision"] is None
    empty_lane = result["latest"]["lane_metrics"]["target_2"]
    assert empty_lane["precision"] is None
    assert empty_lane["hit_count"] is None


@pytest.mark.asyncio
async def test_current_intraday_round_cannot_certify_close(promotion_api_env):
    Session, _ = promotion_api_env
    async with Session() as session:
        await calendar(session)
        await formal_prediction(session)
        await new_pool(session, at=CLOSE.replace(hour=14))
        result = await promotion._build_promotion_daily_learning_review(session, now=AS_OF)
    assert result["latest"]["actual_target_limit_up_count"] is None
    assert "outcome_limit_up_pool_incomplete" in result["latest"]["evaluation_reasons"]


@pytest.mark.asyncio
async def test_recent_calendar_window_keeps_missing_prediction_days(promotion_api_env):
    Session, _ = promotion_api_env
    days = [date(2026, 9, day) for day in (21, 22, 23, 24)]
    async with Session() as session:
        await calendar(session, days)
        session.add_all([record(days[0]), record(days[2])])
        await session.commit()
        result = await promotion._build_promotion_daily_learning_review(
            session, lookback_days=3, now=datetime(2026, 9, 24, 16),
        )
    assert [row["prediction_trade_date"] for row in result["daily"]] == [str(day) for day in reversed(days[:3])]
    missing = result["daily"][1]
    assert missing["predicted_count"] == 0
    assert missing["predicted_limit_up_hit_count"] is None
    assert "formal_ranked_predictions_unavailable" in missing["evaluation_reasons"]
    assert result["aggregate"]["review_days"] == 3


@pytest.mark.asyncio
async def test_recent_calendar_expansion_is_bounded_not_all_history(promotion_api_env):
    Session, _ = promotion_api_env
    start = date(2026, 8, 3)
    days = [start + timedelta(days=index) for index in range(40)]
    async with Session() as session:
        await calendar(session, days)
        session.add(record(start))
        await session.commit()
        result = await promotion._build_promotion_daily_learning_review(
            session, lookback_days=3, now=datetime(2026, 9, 11, 16),
        )
    assert len(result["daily"]) == 3
    assert result["latest"]["prediction_trade_date"] == "2026-09-10"
    assert all(row["predicted_count"] == 0 for row in result["daily"])


@pytest.mark.asyncio
async def test_missing_weekend_calendar_is_not_inferred(promotion_api_env):
    Session, _ = promotion_api_env
    friday, monday = date(2026, 8, 14), date(2026, 8, 17)
    async with Session() as session:
        await calendar(session, (friday, monday))
        session.add(record(friday))
        await session.commit()
        result = await promotion._build_promotion_daily_learning_review(session, now=datetime(2026, 8, 17, 16))
    assert result["latest"]["actual_trade_date"] is None
    assert "outcome_calendar_gap" in result["latest"]["evaluation_reasons"]
    assert result["latest"]["directional_unknown_count"] == 1


@pytest.mark.asyncio
async def test_known_pending_weekend_prediction_does_not_replace_latest_completed(promotion_api_env):
    Session, _ = promotion_api_env
    thursday, friday, saturday = (date(2026, 8, day) for day in (13, 14, 15))
    async with Session() as session:
        await calendar(session, (thursday, friday, saturday))
        session.add_all([record(thursday), record(friday)])
        await session.commit()
        result = await promotion._build_promotion_daily_learning_review(
            session, now=datetime(2026, 8, 15, 16),
        )
    assert len(result["daily"]) == 1
    assert result["latest"]["actual_trade_date"] == str(friday)


@pytest.mark.asyncio
async def test_mixed_window_does_not_zero_unknown_event_counts(promotion_api_env):
    Session, _ = promotion_api_env
    monday, tuesday, wednesday = (date(2026, 9, day) for day in (21, 22, 23))
    async with Session() as session:
        await calendar(session, (monday, tuesday, wednesday))
        for day in (monday, tuesday):
            prediction = record(day)
            factors = promotion._json_loads_safe(prediction.factors_json)
            factors["prediction_actionable"] = True
            prediction.factors_json = promotion._json_dumps_safe(factors)
            session.add(prediction)
        await session.commit()
        await new_pool(session, at=datetime(2026, 9, 22, 15, 0, 1))
        result = await promotion._build_promotion_daily_learning_review(
            session, now=datetime(2026, 9, 23, 16),
        )
    assert result["daily"][1]["predicted_limit_up_hit_count"] == 1
    aggregate = result["aggregate"]
    for field in ("predicted_limit_up_hit_count", "actionable_limit_up_hit_count",
                  "underestimated_limit_up_hit_count", "limit_up_precision", "brier_score"):
        assert aggregate[field] is None
    assert aggregate["average_predicted_probability"] == .5
