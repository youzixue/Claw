"""Read-time compatibility review: isolated SQLite, never production or MCP."""
from datetime import date, datetime, timedelta

import pytest
from app.api.v1 import promotion
from app.models.governance import TradeCalendarModel
from app.models.signal import PromotionPredictionRecord
from app.models.stock import LimitUpPool, StockKline, StockSpot
from test_promotion_api import promotion_api_env, disable_live_dragon_tiger_lookup


def record(day, code="600001", context="promotion_2000"):
    return PromotionPredictionRecord(
        code=code, target_board=1, prediction_trade_date=day,
        predicted_probability=.5, outcome_status="success",
        factors_json=promotion._json_dumps_safe({
            "prediction_snapshot_source": "schedule",
            "prediction_snapshot_context": context,
            "prediction_snapshot_recorded_at": f"{day}T20:00:00",
            "prediction_ranked_selected": True,
            "prediction_ranked_position": 1,
        }),
    )


async def seed(session, before, after, *, bad=None, pool=True, context="promotion_2000"):
    cursor = before
    while cursor <= after:
        session.add(TradeCalendarModel(trade_date=cursor, is_trade_day=cursor in (before, after)))
        cursor += timedelta(days=1)
    session.add(record(before, context=context))
    session.add(LimitUpPool(code="600099", trade_date=before, consecutive_days=1, source="test"))
    session.add(StockKline(code="600001", trade_date=before, close=10, volume=100, source="ths"))
    if bad != "missing":
        session.add(StockKline(
            code="600001", trade_date=after, close=11,
            prev_close=10, volume=0 if bad == "zero_volume" else 100,
            source="spot" if bad == "source" else "ths",
            change_pct=float("nan") if bad == "nan" else 10,
        ))
    if pool:
        session.add(LimitUpPool(code="600001", trade_date=after, consecutive_days=1, source="test"))
    # Future spot and old success state cannot repair historical evidence.
    session.add(StockSpot(code="600001", name="future", price=12, change_pct=20, updated_at=datetime(2026, 12, 31, 15, 30)))
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["missing", "zero_volume", "source", "nan"])
async def test_invalid_outcome_stays_unknown(promotion_api_env, bad):
    Session, _ = promotion_api_env
    async with Session() as session:
        await seed(session, date(2026, 8, 13), date(2026, 8, 14), bad=bad)
        payload = await promotion._build_promotion_daily_learning_review(session, now=datetime(2026, 8, 15))
    for row in (payload["latest"], payload["aggregate"],
                payload["latest"]["launch_precursor_metrics"]["all_ranked_first_board"]):
        assert row["directional_unknown_count"] == 1
        assert row["directional_evaluable_count"] == 0
        assert row["directional_precision"] is None
        assert row["directional_observed_precision"] is None
        assert row["directional_precision_lower_bound"] == 0
        assert row["directional_precision_upper_bound"] == 1
        assert row["directional_target_met"] is None
    assert payload["aggregate"]["valid_review_days"] == 0
    assert payload["latest"]["predicted_count"] == 1


@pytest.mark.asyncio
async def test_missing_next_pool_never_skips_to_later_pool(promotion_api_env):
    Session, _ = promotion_api_env
    async with Session() as session:
        await seed(session, date(2026, 8, 12), date(2026, 8, 13), pool=False)
        session.add(TradeCalendarModel(trade_date=date(2026, 8, 14), is_trade_day=True))
        session.add(LimitUpPool(code="600001", trade_date=date(2026, 8, 14), consecutive_days=1, source="test"))
        await session.commit()
        payload = await promotion._build_promotion_daily_learning_review(session, now=datetime(2026, 8, 15))
    # The recent calendar window now also retains the newer missing forecast.
    # It must not relabel the recorded 08-12 prediction using the later 08-14 pool.
    assert payload["latest"]["prediction_trade_date"] == "2026-08-13"
    assert payload["latest"]["predicted_count"] == 0
    assert "formal_ranked_predictions_unavailable" in payload["latest"]["evaluation_reasons"]
    row = next(item for item in payload["daily"] if item["prediction_trade_date"] == "2026-08-12")
    assert row["actual_trade_date"] == "2026-08-13"
    assert row["limit_up_precision"] is None
    assert row["brier_score"] is None
    assert row["predicted_limit_up_hit_count"] is None
    assert row["lane_metrics"]["target_1"]["precision"] is None
    assert row["directional_precision"] == 1
    assert "outcome_limit_up_pool_missing" in row["evaluation_reasons"]
    assert payload["aggregate"]["limit_up_precision"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("before,after", [(date(2026, 8, 14), date(2026, 8, 17)),
                                         (date(2026, 4, 3), date(2026, 4, 7))])
async def test_recorded_weekend_and_holiday_align(promotion_api_env, before, after):
    Session, _ = promotion_api_env
    async with Session() as session:
        await seed(session, before, after)
        payload = await promotion._build_promotion_daily_learning_review(session, now=datetime.combine(after + timedelta(days=1), datetime.min.time()))
    assert payload["latest"]["actual_trade_date"] == str(after)
    assert payload["latest"]["directional_precision"] == 1


@pytest.mark.asyncio
async def test_missing_prediction_pool_blocks_cross_gap_board_normalization(promotion_api_env):
    Session, _ = promotion_api_env
    async with Session() as session:
        # T0 and T2 have the same limit-up name, but T1's pool was not collected.
        session.add_all([TradeCalendarModel(trade_date=date(2026, 8, d), is_trade_day=True)
                         for d in (12, 13, 14)])
        session.add_all([LimitUpPool(code="600001", trade_date=date(2026, 8, d),
                                    consecutive_days=1, source="test") for d in (12, 14)])
        session.add(record(date(2026, 8, 13)))
        session.add_all([
            StockKline(code="600001", trade_date=date(2026, 8, 13), close=10, volume=100, source="ths"),
            StockKline(code="600001", trade_date=date(2026, 8, 14), close=11, prev_close=10,
                       volume=100, change_pct=10, source="ths"),
        ])
        await session.commit()
        payload = await promotion._build_promotion_daily_learning_review(session, now=datetime(2026, 8, 15))
    row = payload["latest"]
    assert row["prediction_trade_date"] == "2026-08-13"
    assert row["actual_trade_date"] == "2026-08-14"
    assert row["directional_precision"] == 1
    assert row["limit_up_precision"] is None
    assert row["actual_target_limit_up_count"] is None
    assert row["missed_examples"] == []
    assert row["missed_reason_counts"] == {}
    assert "prediction_limit_up_pool_missing" in row["evaluation_reasons"]
    assert "outcome_limit_up_pool_missing" not in row["evaluation_reasons"]
    for lane in row["lane_metrics"].values():
        assert lane["actual_count"] is None
        assert lane["precision"] is None
        assert "prediction_limit_up_pool_missing" in lane["evaluation_reasons"]
    assert payload["aggregate"]["limit_up_precision"] is None
    assert payload["aggregate"]["valid_review_days"] == 0
    assert payload["recommendation"]["status"] == "data_incomplete"


@pytest.mark.asyncio
async def test_calendar_gap_does_not_infer_next_available_day(promotion_api_env):
    Session, _ = promotion_api_env
    async with Session() as session:
        session.add(record(date(2026, 8, 12)))
        session.add_all([TradeCalendarModel(trade_date=date(2026, 8, d), is_trade_day=True) for d in (12, 14)])
        await session.commit()
        payload = await promotion._build_promotion_daily_learning_review(session, now=datetime(2026, 8, 15))
    assert payload["latest"]["actual_trade_date"] is None
    assert payload["latest"]["predicted_count"] == 1
    assert payload["latest"]["directional_unknown_count"] == 1
    assert "outcome_calendar_gap" in payload["latest"]["evaluation_reasons"]


@pytest.mark.parametrize("context", ["promotion_0925", "promotion_0935", "unknown", ""])
def test_no_formal_close_does_not_fall_back(context):
    assert promotion._latest_learning_batch_records([record(date(2026, 8, 13), context=context)]) == []


@pytest.mark.asyncio
async def test_partial_day_keeps_missing_name_in_all_denominators(promotion_api_env):
    Session, _ = promotion_api_env
    async with Session() as session:
        await seed(session, date(2026, 8, 13), date(2026, 8, 14))
        session.add(record(date(2026, 8, 13), code="600002"))
        await session.commit()
        payload = await promotion._build_promotion_daily_learning_review(session, now=datetime(2026, 8, 15))
    for row in (payload["latest"], payload["aggregate"],
                payload["latest"]["launch_precursor_metrics"]["all_ranked_first_board"],
                payload["aggregate"]["launch_precursor_metrics"]["all_ranked_first_board"],
                payload["latest"]["lane_metrics"]["target_1"]):
        assert row["directional_evaluable_count"] == 1
        assert row["directional_unknown_count"] == 1
        assert row["directional_coverage"] == .5
        assert row["directional_observed_precision"] == 1
        assert row["directional_precision"] is None
        assert row["directional_precision_lower_bound"] == .5
        assert row["directional_precision_upper_bound"] == 1
        assert row["directional_target_met"] is None
    assert payload["latest"]["predicted_count"] == 2
    assert payload["aggregate"]["predicted_count"] == 2
    assert payload["aggregate"]["valid_review_days"] == 0


@pytest.mark.asyncio
async def test_intraday_only_is_not_a_close_score(promotion_api_env):
    Session, _ = promotion_api_env
    async with Session() as session:
        await seed(session, date(2026, 8, 13), date(2026, 8, 14), context="promotion_0925")
        payload = await promotion._build_promotion_daily_learning_review(session, now=datetime(2026, 8, 15))
    assert payload["latest"]["predicted_count"] == 0
    assert payload["latest"]["directional_precision"] is None
    assert payload["aggregate"]["directional_target_met"] is None
    assert payload["latest"]["evaluation_status"] == "unavailable"


@pytest.mark.asyncio
async def test_empty_review_reports_null_aggregate_precision(promotion_api_env):
    Session, _ = promotion_api_env
    async with Session() as session:
        payload = await promotion._build_promotion_daily_learning_review(session, now=datetime(2026, 8, 15))
    assert payload["daily"] == []
    assert payload["aggregate"]["predicted_count"] == 0
    assert payload["aggregate"]["directional_precision"] is None
    assert payload["aggregate"]["directional_target_met"] is None
    assert payload["aggregate"]["evaluation_status"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_count", [0, 1])
async def test_learning_direction_denominator_requires_verified_close(promotion_api_env, valid_count):
    Session, _ = promotion_api_env
    before, after = date(2026, 8, 13), date(2026, 8, 14)
    async with Session() as session:
        session.add_all([TradeCalendarModel(trade_date=day, is_trade_day=True) for day in (before, after)])
        for index in range(4):
            item = record(before, code=f"60000{index + 1}")
            item.model_version = promotion.PROMOTION_MODEL_VERSION
            item.candidate_route = "mainline_spread_start"
            item.learning_bucket = "T1:mainline_spread_start"
            item.outcome_status = "success" if index == 0 else "failed"
            item.actual_close_change_pct = 99 if index else 0
            item.failure_tags_json = '["outcome_close_unavailable"]'
            session.add(item)
            if index < valid_count:
                session.add_all([
                    StockKline(code=item.code, trade_date=before, close=10, volume=100, source="ths"),
                    StockKline(code=item.code, trade_date=after, close=11, prev_close=10,
                               change_pct=10, volume=100, source="ths"),
                ])
        await session.commit()
        stats = await promotion._load_promotion_learning_stats(session)
    row = stats["T1:mainline_spread_start"]
    assert row["sample_count"] == 4
    assert row["success_count"] == 1
    assert row["success_rate"] == .25
    assert row["directional_sample_count"] == valid_count
    assert row["directional_unknown_count"] == 4 - valid_count
    assert row["pool_directional_sample_count"] == valid_count
    assert row["pool_directional_unknown_count"] == 4 - valid_count
    assert row["rising_rate"] == (1 if valid_count else None)
    assert row["empirical_rising_probability"] == round((valid_count + 10) / (valid_count + 20), 4)
    assert row["pool_empirical_rising_probability"] == row["empirical_rising_probability"]
    assert row["avg_close_change_pct"] == (10 if valid_count else None)


def test_fixed_denominator_partial_and_empty_metrics():
    row = promotion._promotion_directional_metrics(10, 5, 4)
    assert row["directional_observed_precision"] == .8
    assert row["directional_precision"] is None
    assert row["directional_coverage"] == .5
    assert row["directional_precision_lower_bound"] == .4
    assert row["directional_precision_upper_bound"] == .9
    assert row["directional_target_met"] is None
    empty = promotion._promotion_directional_metrics(0, 0, 0)
    assert empty["directional_precision"] is None
    assert empty["directional_target_met"] is None
