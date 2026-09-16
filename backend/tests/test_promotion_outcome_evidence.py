from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from app.promotion.outcome_evidence import next_recorded_trade_day, formal_outcome_bar_error

DAY = date(2026, 8, 28)


def test_recorded_calendar_closed_days_are_not_outcomes_and_unknown_is_never_skipped():
    calendar = {DAY: True, DAY+timedelta(days=1): False, DAY+timedelta(days=2): False,
                DAY+timedelta(days=3): True}
    assert next_recorded_trade_day(DAY, calendar, through=DAY+timedelta(days=4)) == (DAY+timedelta(days=3), "")
    del calendar[DAY+timedelta(days=1)]
    assert next_recorded_trade_day(DAY, calendar, through=DAY+timedelta(days=4)) == (None, "outcome_calendar_gap")
    assert next_recorded_trade_day(DAY, {}, through=DAY) == (None, "prediction_calendar_unknown")
    assert next_recorded_trade_day(DAY, {DAY: True}, through=DAY) == (None, "outcome_session_not_available")
    assert next_recorded_trade_day(DAY, {DAY: True, DAY+timedelta(days=1): True}, through=DAY+timedelta(days=1)) == (None, "calendar_conflict")


def bar(**kwargs):
    return SimpleNamespace(**{"close": 10.0, "prev_close": 10., "volume": 100., "source": "tencent_close", **kwargs})


@pytest.mark.parametrize("before,after,reason", [
    (None, bar(), "candidate_outcome_bar_missing"),
    (bar(), bar(source="spot_fallback"), "candidate_outcome_source_unverified"),
    (bar(), bar(close=float("nan")), "candidate_outcome_suspended_or_invalid"),
    (bar(), bar(volume=0), "candidate_outcome_suspended_or_invalid"),
    (bar(), bar(close=True), "candidate_outcome_suspended_or_invalid"),
    (bar(), bar(prev_close=9), "candidate_outcome_price_chain_discontinuity"),
    (bar(), bar(prev_close=None), "candidate_outcome_price_chain_discontinuity"),
    (bar(), bar(prev_close=10.01, source="ths"), ""),
])
def test_formal_next_day_price_and_volume_truth(before, after, reason):
    assert formal_outcome_bar_error(before, after) == reason
