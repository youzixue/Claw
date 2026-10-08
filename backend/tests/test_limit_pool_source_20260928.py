"""Offline pure-function fixtures: no production database or source access."""
from datetime import date, datetime, timedelta, timezone
import json
import socket
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.data.limit_pool_source import (
    TENCENT_LIMIT_VERSION, parse_wencai_limit_details, project_tencent_limits,
)

DAY = date(2026, 9, 28)
NOW = datetime(2026, 9, 28, 10, 0)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden in pure adapter tests")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def quote(**changes):
    row = dict(code="600001", name="fixture", price=11, prev_close=10,
               limit_up=11, limit_down=9, high=11, low=10,
               volume=100, turnover=1.5, source_quote_at=NOW,
               quote_round_id="round-test")
    row.update(changes)
    row.setdefault("received_at", row["source_quote_at"])
    return row


def project(rows, observed_at=NOW, max_age_sec=60):
    return project_tencent_limits(rows, observed_at=observed_at, max_age_sec=max_age_sec)


def test_up_state_has_unknown_metadata_and_real_evidence():
    row = project([quote()])["up"][0]
    assert row["limit_up_price"] == 11 and row["turnover"] == 1.5
    assert row["source_version"] == TENCENT_LIMIT_VERSION == "tencent_limit_state_v1"
    assert row["source"] == "tencent" and row["quarantined"] is False
    for key in ("break_count", "consecutive_days", "limit_up_time", "seal_amount"):
        assert row[key] is None
    evidence = json.loads(row["evidence_json"])
    assert evidence["quote_round_id"] == "round-test"
    assert evidence["scope"] == "verified_codes_only_not_full_market"
    assert evidence["source_quote_at"] == NOW.isoformat()


def test_partial_frame_regular_codes_and_empty_scope():
    rows = [quote(), quote(code="000001", price=10.5, high=10.8),
            quote(code="300001", price=10.5)]
    result = project(rows)
    assert result["valid_codes"] == {"600001", "000001", "300001"}
    assert len(result["up"]) == len(result["broken"]) == 1
    broken = result["broken"][0]
    assert broken["close_price"] == 10.5 and broken["close_at_limit"] is False
    assert broken["final_state"] == "broken"
    assert all(broken[k] is None for k in ("break_time", "limit_up_time", "seal_amount", "seal_duration"))
    assert project([]) == dict(valid_codes=set(), up=[], down=[], broken=[], rejected_count=0)


def test_down_and_intraday_touch_can_coexist():
    result = project([quote(price=9, low=9)])
    assert not result["up"]
    assert len(result["down"]) == len(result["broken"]) == 1
    assert result["down"][0]["break_count"] is None
    assert result["down"][0]["consecutive_days"] is None


@pytest.mark.parametrize("changes", [
    {"source_quote_at": NOW - timedelta(seconds=61)},
    {"source_quote_at": NOW + timedelta(seconds=1)},
    {"source_quote_at": NOW - timedelta(days=1)},
    {"source_quote_at": NOW.replace(hour=9, minute=29)},
    {"source_quote_at": None},
    {"received_at": None},
    {"received_at": NOW - timedelta(seconds=1)},
    {"received_at": NOW + timedelta(seconds=1)},
    {"source_quote_at": "20260928100000"},
    {"price": 0}, {"volume": 0}, {"limit_up": 0},
    {"limit_up": 10}, {"limit_down": 10}, {"prev_close": 11},
    {"low": 11.01}, {"high": 11.01}, {"low": 8.99},
    {"price": 11.005}, {"high": 11.005},
    {"code": "000000"}, {"code": "123456"}, {"code": "600001.SH"},
    {"code": "60001"}, {"code": 600001},
])
def test_rejected_quote_boundaries(changes):
    result = project([quote(**changes)])
    assert result["rejected_count"] == 1 and not result["valid_codes"]


@pytest.mark.parametrize("field", ["price", "limit_up", "limit_down", "prev_close", "high", "low", "volume"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), None, "1e10000", True])
def test_all_required_numbers_are_finite_positive(field, value):
    assert project([quote(**{field: value})])["rejected_count"] == 1


def test_sub_half_cent_tolerance_and_not_ten_percent_guess():
    assert project([quote(price=10.9999)])["up"]
    assert project([quote(price=12, high=12, limit_up=12, limit_down=8)])["up"]
    assert project([quote(price=10.9949)])["broken"]
    assert project([quote(source_quote_at=NOW-timedelta(seconds=60))])["up"]


def test_duplicate_code_rejects_every_occurrence_without_hiding_valid_other():
    result = project([quote(), quote(price=10.5), quote(code="000001")])
    assert result["rejected_count"] == 2
    assert result["valid_codes"] == {"000001"}


def test_close_lunch_and_source_clock_preservation():
    close = NOW.replace(hour=15)
    for hour in (15, 18, 23):
        observed = NOW.replace(hour=hour, minute=30)
        result = project([quote(source_quote_at=close)], observed_at=observed)
        assert result["up"][0]["source_quote_at"] == close
    late = close + timedelta(seconds=3)
    assert project([quote(source_quote_at=late)], observed_at=close+timedelta(minutes=1))["up"]
    assert not project([quote(source_quote_at=close-timedelta(seconds=1))],
                       observed_at=close+timedelta(hours=2))["valid_codes"]
    assert not project([quote(source_quote_at=close)],
                       observed_at=close+timedelta(days=1))["valid_codes"]
    lunch = NOW.replace(hour=12)
    assert project([quote(source_quote_at=lunch)], observed_at=lunch)["up"]
    assert not project([quote(source_quote_at=lunch)],
                       observed_at=lunch+timedelta(minutes=2))["valid_codes"]


def test_aware_clock_compares_shanghai_and_preserves_original():
    source = NOW.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
    row = project([quote(source_quote_at=source)])["up"][0]
    assert row["source_quote_at"] == source


@pytest.mark.parametrize("age", [-1, float("nan"), float("inf"), None])
def test_invalid_max_age_is_contract_error(age):
    with pytest.raises(ValueError):
        project([], max_age_sec=age)


def frame(**changes):
    row = {"股票代码": "600001.SH", "连续涨停天数[20260928]": 2,
           "涨停开板次数[20260928]": 0,
           "首次涨停时间[20260928]": "2026-09-28 09:35:00",
           "涨停原因类别[20260928]": "测试原因", "涨停封单额[20260928]": 0}
    row.update(changes)
    return pd.DataFrame([row])


def details(df):
    return parse_wencai_limit_details(df, DAY)


def test_wencai_exact_date_normal_zero_and_metadata_only():
    result = details(frame())["600001"]
    assert result == dict(consecutive_days=2, break_count=0, limit_up_time="09:35:00",
                          limit_up_reason="测试原因", seal_amount=0)
    assert "source" not in result and "price" not in result


def test_wencai_missing_not_invented_and_wrong_day_not_used():
    df = pd.DataFrame([{"股票代码": "000001.SZ", "涨停原因[20260928]": "原因",
                        "连续涨停天数[20260927]": 9, "涨停开板次数": 3}])
    result = details(df)["000001"]
    assert result["consecutive_days"] is result["break_count"] is None
    assert result["seal_amount"] is result["limit_up_time"] is None
    assert result["limit_up_reason"] == "原因"


@pytest.mark.parametrize("df", [pd.DataFrame(), pd.DataFrame([{"股票代码": "600001",
    "连续涨停天数[20260927]": 3}]), pd.DataFrame([{"股票代码": "600001", "连续涨停天数": 3}])])
def test_wencai_empty_wrong_day_undated_rejected(df):
    with pytest.raises(ValueError):
        details(df)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, "bad", True])
def test_wencai_invalid_numeric_missing(value):
    result = details(frame(**{"连续涨停天数[20260928]": value,
                              "涨停开板次数[20260928]": value,
                              "涨停封单额[20260928]": value}))["600001"]
    assert result["consecutive_days"] is result["break_count"] is result["seal_amount"] is None


@pytest.mark.parametrize("value", ["2026-09-27 09:35:00", "2026-09-29 09:35:00",
                                  "24:00:00", "09:60:00", "093500", "nan"])
def test_wencai_bad_time_is_unknown(value):
    assert details(frame(**{"首次涨停时间[20260928]": value}))["600001"]["limit_up_time"] is None


@pytest.mark.parametrize("value", ["nan", float("nan"), None, " ", "null"])
def test_wencai_reason_not_nan(value):
    assert details(frame(**{"涨停原因类别[20260928]": value}))["600001"]["limit_up_reason"] is None


@pytest.mark.parametrize("unit,scale", [("元", 1), ("万", 10000), ("亿", 100000000)])
def test_wencai_explicit_amount_units(unit, scale):
    df = frame().drop(columns=["涨停封单额[20260928]"])
    df[f"涨停封单额({unit})[20260928]"] = 1.5
    assert details(df)["600001"]["seal_amount"] == 1.5 * scale
    df[f"涨停封单额({unit})[20260928]"] = "1.5亿"
    assert details(df)["600001"]["seal_amount"] is None


def test_wencai_duplicate_normalized_code_and_truncation():
    df = pd.concat([frame(), frame(**{"股票代码": "600001"})], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate"):
        details(df)
    for key in ("code_count", "row_count"):
        df = frame()
        df.attrs[key] = 2
        with pytest.raises(ValueError, match="truncated"):
            details(df)
        df.attrs[key] = 1
        assert len(details(df)) == 1


def test_wencai_invalid_codes_cannot_satisfy_declared_coverage():
    df = frame(**{"股票代码": "123456"})
    df.attrs["code_count"] = 1
    with pytest.raises(ValueError, match="truncated"):
        details(df)


def test_wencai_reason_alias_precedence_and_integer_validation():
    result = details(frame(**{"涨停原因[20260928]": "alias",
                              "连续涨停天数[20260928]": 0,
                              "涨停开板次数[20260928]": 1.5}))["600001"]
    assert result["limit_up_reason"] == "测试原因"
    assert result["break_count"] is result["consecutive_days"] is None


def test_wencai_duplicate_columns_and_invalid_attrs_fail_closed():
    df = frame()
    df.columns = ["股票代码"] + ["连续涨停天数[20260928]"] * 5
    with pytest.raises(ValueError, match="duplicate"):
        details(df)
    for invalid in (-1, 1.5, float("inf"), "nan"):
        df = frame()
        df.attrs["row_count"] = invalid
        with pytest.raises(ValueError, match="invalid"):
            details(df)


def test_wencai_bj_identity_and_time_only():
    result = details(frame(**{"股票代码": "920001.BJ",
                              "首次涨停时间[20260928]": "09:35:00"}))
    assert result["920001"]["limit_up_time"] == "09:35:00"


def test_bad_observation_clock_and_undated_metric_units():
    with pytest.raises(ValueError):
        project([], observed_at=None)
    df = pd.DataFrame([{"股票代码": "600001", "连续涨停天数(万)[20260928]": 1}])
    with pytest.raises(ValueError, match="no exact"):
        details(df)


def test_inputs_not_mutated():
    row = quote()
    original = dict(row)
    project([row])
    assert row == original
    df = frame()
    original_df = df.copy(deep=True)
    details(df)
    pd.testing.assert_frame_equal(df, original_df)
