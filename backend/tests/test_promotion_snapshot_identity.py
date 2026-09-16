from datetime import datetime

from app.promotion.snapshot_identity import (
    normalize_snapshot_context,
    normalize_snapshot_source,
    parse_snapshot_recorded_at,
    snapshot_batch_key_from_factors,
)


def test_snapshot_source_and_context_are_canonical():
    assert normalize_snapshot_source("cron") == "schedule"
    assert normalize_snapshot_source("UI") == "page"
    assert normalize_snapshot_context("20:00", source="schedule") == "promotion_2000"
    assert normalize_snapshot_context("9:25", source="schedule") == "promotion_0925"
    assert normalize_snapshot_context("", source="page") == "page"


def test_snapshot_time_and_batch_key_are_stable():
    assert parse_snapshot_recorded_at("2026-08-28T20:00:00") == datetime(
        2026, 8, 28, 20, 0
    )
    factors = {
        "prediction_snapshot_source": "scheduler",
        "prediction_snapshot_context": "20:00",
        "prediction_snapshot_recorded_at": "2026-08-28T20:00:00",
    }
    assert (
        snapshot_batch_key_from_factors(factors)
        == "schedule:20:00:2026-08-28T20:00:00"
    )
    factors["prediction_snapshot_batch_key"] = "explicit"
    assert snapshot_batch_key_from_factors(factors) == "explicit"
