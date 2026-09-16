from datetime import date, timedelta
import sqlite3

from scripts.analyze_limit_up_precursors import (
    _binary_auc,
    _strict_first_board_codes,
    _temporal_holdout_backtest,
)
from scripts.backtest_first_board_rank_v26 import probability_metrics


def test_binary_auc_handles_perfect_order_and_ties():
    assert _binary_auc([(0.0, False), (1.0, True)]) == 1.0
    assert _binary_auc([(0.0, False), (0.0, True)]) == 0.5
    assert _binary_auc([(1.0, True)]) is None


def test_v26_probability_metrics_reports_brier_and_auc():
    metrics = probability_metrics([(0.1, False), (0.9, True)])

    assert metrics["sample_count"] == 2
    assert metrics["average_probability"] == 0.5
    assert metrics["observed_rate"] == 0.5
    assert metrics["brier_score"] == 0.01
    assert metrics["roc_auc"] == 1.0


def test_temporal_holdout_prefers_confirmed_precursors_over_raw_low_position():
    samples = []
    start = date(2026, 1, 5)
    for index in range(10):
        event_date = str(start + timedelta(days=index))
        samples.extend(
            [
                {
                    "code": f"600{index:03d}",
                    "event_date": event_date,
                    "first_board": True,
                    "event_rise": True,
                    "event_strong_rise": True,
                    "low_position_42": False,
                    "launch_active_volume_turnover": True,
                    "funding_preheat_ready": True,
                    "primary_industry_ignition_ready": True,
                },
                {
                    "code": f"601{index:03d}",
                    "event_date": event_date,
                    "first_board": False,
                    "event_rise": False,
                    "event_strong_rise": False,
                    "low_position_42": True,
                    "launch_active_volume_turnover": False,
                    "funding_preheat_ready": False,
                    "primary_industry_ignition_ready": False,
                },
            ]
        )

    result = _temporal_holdout_backtest(samples)

    assert result["status"] == "ok"
    assert result["training_date_count"] == 7
    assert result["holdout_date_count"] == 3
    methods = {item["score"]: item for item in result["methods"]}
    assert methods["raw_low_position"]["roc_auc"] == 0.0
    assert methods["previous_close_launch_precursors"]["roc_auc"] == 1.0
    assert methods["previous_close_launch_precursors"]["top_quintile_first_board_rate"] == 1.0
    assert result["previous_close_feature_weights"]["low_position_42"] < 0
    assert result["previous_close_feature_weights"]["funding_preheat_ready"] > 0


def test_strict_first_board_truth_uses_authoritative_consecutive_days():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE limit_up_pool ("
        "trade_date TEXT, code TEXT, name TEXT, consecutive_days INTEGER)"
    )
    previous_date = date(2026, 8, 27)
    event_date = date(2026, 8, 28)
    connection.executemany(
        "INSERT INTO limit_up_pool VALUES (?, ?, ?, ?)",
        [
            (str(previous_date), "600001", "昨日涨停甲", 1),
            (str(previous_date), "600002", "昨日涨停乙", 1),
            # 600001 即使昨日也涨停，权威字段为1时仍按首板；600003 即使
            # 昨日不在池，权威字段为2时也绝不能近似成首板。
            (str(event_date), "600001", "权威首板", 1),
            (str(event_date), "600002", "权威二板", 2),
            (str(event_date), "600003", "断档二板", 2),
            (str(event_date), "600004", "普通首板", 1),
            (str(event_date), "600005", "ST风险", 1),
        ],
    )

    first_board, all_limit_up = _strict_first_board_codes(
        connection,
        event_date,
        previous_date,
        st_codes=set(),
    )

    assert first_board == {"600001", "600004"}
    assert all_limit_up == {"600001", "600002", "600003", "600004"}
