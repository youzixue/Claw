import pytest

from app.api.v1 import commodity_linkage
from app.api.v1.commodity_linkage import _attach_signal_fields, _spot_counterpart


def test_spot_signal_prefers_daily_change_over_half_year_direction():
    item = {
        "daily_change_pct": 0.62,
        "half_year_change_pct": -7.63,
        "producer_stocks": [{"code": "601857", "name": "中国石油", "change_pct": -0.24}],
        "downstream_stocks": [{"code": "000001", "name": "下游公司", "change_pct": 1.2}],
    }

    _attach_signal_fields(item)

    assert item["effective_change_pct"] == 0.62
    assert item["signal_horizon"] == "日频"
    assert item["impact_direction"] == "上游受益 / 下游承压"
    assert item["benefit_stocks"][0]["code"] == "601857"
    assert item["pressure_stocks"][0]["code"] == "000001"


def test_missing_daily_uses_half_year_without_treating_null_as_up():
    item = {
        "daily_change_pct": None,
        "half_year_change_pct": -6.86,
        "producer_stocks": [{"code": "600309", "name": "万华化学", "change_pct": -0.9}],
        "downstream_stocks": [{"code": "002001", "name": "下游应用", "change_pct": 0.5}],
    }

    _attach_signal_fields(item)

    assert item["effective_change_pct"] == -6.86
    assert item["signal_horizon"] == "近半年"
    assert item["impact_direction"] == "上游承压 / 下游受益"
    assert item["pressure_stocks"][0]["code"] == "600309"
    assert item["benefit_stocks"][0]["code"] == "002001"


def test_no_price_signal_keeps_direction_neutral():
    item = {
        "daily_change_pct": None,
        "half_year_change_pct": None,
        "producer_stocks": [{"code": "600000", "name": "测试上游", "change_pct": 0.1}],
        "downstream_stocks": [],
    }

    _attach_signal_fields(item)

    assert item["effective_change_pct"] is None
    assert item["impact_direction"] == "方向待确认"
    assert item["benefit_stocks"] == []
    assert item["pressure_stocks"] == []
    assert item["neutral_stocks"][0]["code"] == "600000"


def test_futures_can_reuse_spot_stock_mapping_by_symbol():
    spot_rows = [{
        "symbol": "CU",
        "indicator_name": "铜",
        "producer_stocks": [{"code": "000630", "name": "铜陵有色", "change_pct": -2.84}],
        "downstream_stocks": [{"code": "002203", "name": "海亮股份", "change_pct": 0.8}],
    }]

    counterpart = _spot_counterpart(spot_rows, "CU0", "铜")

    assert counterpart is spot_rows[0]


def test_stock_risk_labels_mark_non_tradeable_flags():
    stock = {
        "board_tag": "observe_only",
        "is_st": True,
        "is_suspended": False,
        "is_delisting": False,
        "is_ipo_recent": True,
    }

    labels = commodity_linkage._stock_risk_labels(stock)

    assert labels == ["观察", "ST", "次新"]
    assert commodity_linkage._stock_tradeable(stock) is False


@pytest.mark.asyncio
async def test_spot_price_history_source_failure_degrades(monkeypatch):
    async def fail_call(*args, **kwargs):
        raise RuntimeError("timeout")

    monkeypatch.setattr(commodity_linkage, "_ak_call", fail_call)
    warnings = []
    source_status = {}

    history = await commodity_linkage._load_spot_price_history(warnings, source_status)

    assert history == {}
    assert source_status[commodity_linkage.SOURCE_SPOT_PRICE_DAILY] == "error"
    assert warnings[0]["source"] == commodity_linkage.SOURCE_SPOT_PRICE_DAILY


@pytest.mark.asyncio
async def test_futures_realtime_source_failure_degrades(monkeypatch):
    async def fail_call(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(commodity_linkage, "_ak_call", fail_call)
    warnings = []
    source_status = {}

    rows = await commodity_linkage._build_futures_rows(
        db=None,
        spot_rows=[],
        warnings=warnings,
        source_status=source_status,
    )

    assert rows == []
    assert source_status[commodity_linkage.SOURCE_FUTURES_SPOT] == "error"
    assert warnings[0]["source"] == commodity_linkage.SOURCE_FUTURES_SPOT
