"""资金流备用源必须保持供应商和主力指标的真实语义。"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from sqlalchemy import select

from app.config.settings import settings
from app.data import scheduler as scheduler_module
from app.data.scheduler import DataScheduler
from app.data.sources import akshare_source as source_module
from app.data.sources.akshare_source import AkShareSource
from app.models.stock import FundFlow, StockSpot
from test_scheduler_kline_fill import scheduler_db_env


def test_ths_total_net_flow_is_not_published_as_main_flow():
    frame = pd.DataFrame([{
        "股票代码": "000001",
        "股票简称": "平安银行",
        "流入资金": "1.2亿",
        "流出资金": "0.8亿",
        "净额": "4000万",
        "成交额": "5亿",
    }])

    records = DataScheduler._parse_individual_fund_flow_df(
        frame,
        date(2026, 9, 7),
        source="ths_via_akshare",
        source_version="individual_total_fund_flow_ths_v1",
    )

    assert records == []


def test_verified_main_flow_preserves_source_version_and_observed_at():
    observed_at = datetime(2026, 9, 7, 10, 1, 2)
    frame = pd.DataFrame([{
        "代码": "000001",
        "名称": "平安银行",
        "今日主力净流入-净额": 12_345,
        "今日主力净流入-净占比": 1.25,
        "今日大单净流入-净额": 100,
        "今日中单净流入-净额": -20,
        "今日小单净流入-净额": -80,
        "source_quote_at": observed_at - timedelta(seconds=2),
        "received_at": observed_at - timedelta(seconds=1),
    }])

    records = DataScheduler._parse_individual_fund_flow_df(
        frame,
        date(2026, 9, 7),
        source="eastmoney_via_akshare",
        source_version="individual_fund_flow_eastmoney_akshare_v1",
        observed_at=observed_at,
    )

    assert records == [{
        "code": "000001",
        "name": "平安银行",
        "trade_date": date(2026, 9, 7),
        "main_net_inflow": 12_345.0,
        "main_net_inflow_pct": 1.25,
        "super_net_inflow": None,
        "super_net_inflow_pct": None,
        "big_net_inflow": 100.0,
        "big_net_inflow_pct": None,
        "mid_net_inflow": -20.0,
        "mid_net_inflow_pct": None,
        "small_net_inflow": -80.0,
        "small_net_inflow_pct": None,
        "source": "eastmoney_via_akshare",
        "source_version": "individual_fund_flow_eastmoney_akshare_v1",
        "observed_at": observed_at,
        "source_quote_at": observed_at - timedelta(seconds=2),
        "received_at": observed_at - timedelta(seconds=1),
    }]


def test_rows_with_named_but_missing_main_values_do_not_count_as_coverage():
    frame = pd.DataFrame([
        {
            "代码": "000001", "名称": "有效",
            "今日主力净流入-净额": 0, "今日主力净流入-净占比": 0,
        },
        {
            "代码": "000002", "名称": "净额缺失",
            "今日主力净流入-净额": None, "今日主力净流入-净占比": 1.2,
        },
        {
            "代码": "000003", "名称": "占比缺失",
            "今日主力净流入-净额": 100, "今日主力净流入-净占比": float("nan"),
        },
    ])

    observed_at = datetime(2026, 9, 7, 10, 1, 2)
    frame["source_quote_at"] = observed_at - timedelta(seconds=2)
    frame["received_at"] = observed_at - timedelta(seconds=1)
    records = DataScheduler._parse_individual_fund_flow_df(
        frame, date(2026, 9, 7), source="eastmoney", observed_at=observed_at,
    )

    assert [record["code"] for record in records] == ["000001"]
    assert records[0]["main_net_inflow"] == 0
    assert records[0]["main_net_inflow_pct"] == 0


@pytest.mark.parametrize("column", ["今日主力净流入-净额", "今日主力净流入-净占比"])
@pytest.mark.parametrize("value", [True, False])
def test_boolean_main_values_cannot_be_coerced_to_measured_flow(column, value):
    observed_at = datetime(2026, 9, 7, 10, 1, 2)
    good = {"代码": "000001", "今日主力净流入-净额": 0,
            "今日主力净流入-净占比": 0,
            "source_quote_at": observed_at - timedelta(seconds=2),
            "received_at": observed_at - timedelta(seconds=1)}
    bad = {**good, "代码": "000002", column: value}
    records = DataScheduler._parse_individual_fund_flow_df(
        pd.DataFrame([good, bad]), observed_at.date(), observed_at=observed_at,
    )
    assert [record["code"] for record in records] == ["000001"]
    assert records[0]["main_net_inflow"] == records[0]["main_net_inflow_pct"] == 0


@pytest.mark.parametrize("value", [True, False])
def test_boolean_optional_flows_stay_missing_and_explicit_units_stay_valid(value):
    observed_at = datetime(2026, 9, 7, 10, 1, 2)
    frame = pd.DataFrame([{
        "代码": "000001", "主力净流入-净额": "1.2万", "主力净流入-净占比": "1.25%",
        "大单净流入-净额": value, "中单净流入-净额": value, "小单净流入-净额": value,
        "source_quote_at": observed_at - timedelta(seconds=2),
        "received_at": observed_at - timedelta(seconds=1),
    }])
    records = DataScheduler._parse_individual_fund_flow_df(
        frame, observed_at.date(), observed_at=observed_at,
    )
    assert len(records) == 1
    assert records[0]["main_net_inflow"] == 12_000
    assert records[0]["main_net_inflow_pct"] == 1.25
    assert all(records[0][key] is None for key in ("big_net_inflow", "mid_net_inflow", "small_net_inflow"))


@pytest.mark.asyncio
async def test_tencent_partial_coverage_counts_only_rows_with_valid_main_pair(
    scheduler_db_env, monkeypatch,
):
    rows = []
    for number in range(1, 101):
        rows.append({
            "代码": f"{number:06d}",
            "名称": f"测试{number}",
            "今日主力净流入-净额": 100.0 if number == 1 else None,
            "今日主力净流入-净占比": 1.0 if number == 1 else None,
        })
    fallback = pd.DataFrame(rows)
    observed_at = datetime.now()
    fallback["source_quote_at"] = observed_at - timedelta(seconds=2)
    fallback["received_at"] = observed_at - timedelta(seconds=1)
    fallback.attrs.update({
        "fund_flow_source": "tencent",
        "fund_flow_source_version": "tencent_hsfundtab_v1",
        "fund_flow_observed_at": observed_at,
        "fund_flow_expected_count": 100,
    })
    scheduler = DataScheduler()
    fetch = AsyncMock(return_value=fallback)
    forbidden = AsyncMock(side_effect=AssertionError("old source must not be called"))
    scheduler._sources = {
        "tencent": SimpleNamespace(get_individual_fund_flow=fetch),
        "eastmoney": SimpleNamespace(get_individual_fund_flow=forbidden),
        "akshare": SimpleNamespace(
            get_individual_fund_flow=forbidden,
            get_sector_fund_flow=AsyncMock(return_value=pd.DataFrame()),
        ),
    }
    success = AsyncMock()
    monkeypatch.setattr(scheduler_module.data_quality_guard, "record_success", success)
    monkeypatch.setattr(
        scheduler_module.trade_calendar, "is_trading_hours", AsyncMock(return_value=True),
    )
    monkeypatch.setattr(settings, "FUND_FLOW_INDIVIDUAL_FALLBACK_MIN_COVERAGE", 0.95)
    async with scheduler_db_env() as session:
        session.add_all(StockSpot(code=f"{number:06d}") for number in range(1, 101))
        await session.commit()

    await scheduler._intraday_slow()

    async with scheduler_db_env() as session:
        persisted = list((await session.scalars(select(FundFlow))).all())
    assert [row.code for row in persisted] == ["000001"]
    assert persisted[0].source == "tencent"
    fetch.assert_awaited_once_with([f"{number:06d}" for number in range(1, 101)])
    forbidden.assert_not_awaited()
    for dataset in ("individual_fund_flow_round", "individual_fund_flow"):
        call = next(c for c in success.await_args_list if c.args[1:3] == ("tencent", dataset))
        assert call.kwargs["record_count"] == 1
        assert call.kwargs["expected_count"] == 100


@pytest.mark.parametrize("amount,pct", [(float("inf"), 1), (1, float("-inf"))])
def test_infinite_main_values_are_not_valid_coverage(amount, pct):
    frame = pd.DataFrame([{"代码":"000001", "今日主力净流入-净额":amount, "今日主力净流入-净占比":pct}])
    now = datetime(2026, 9, 7, 10)
    frame["source_quote_at"] = now
    frame["received_at"] = now
    assert DataScheduler._parse_individual_fund_flow_df(frame, now.date(), observed_at=now) == []


@pytest.mark.parametrize("observed", ["invalid", datetime(2026, 9, 6, 10), datetime(2099, 9, 7, 10)])
def test_explicit_invalid_clock_is_not_replaced_with_now(observed):
    frame = pd.DataFrame([{"代码":"000001", "今日主力净流入-净额":1, "今日主力净流入-净占比":1}])
    assert DataScheduler._parse_individual_fund_flow_df(frame, date(2026, 9, 7), observed_at=observed) == []


@pytest.mark.asyncio
async def test_akshare_fallback_prefers_today_main_flow_contract(monkeypatch):
    main_loader = AsyncMock()
    total_loader = AsyncMock()
    monkeypatch.setattr(source_module.ak, "stock_individual_fund_flow_rank", main_loader)
    monkeypatch.setattr(source_module.ak, "stock_fund_flow_individual", total_loader)

    source = AkShareSource()
    returned = pd.DataFrame([{
        "代码": "000001",
        "今日主力净流入-净额": 1,
        "今日主力净流入-净占比": 0.1,
    }])
    source._safe_call = AsyncMock(return_value=returned)

    frame = await source.get_individual_fund_flow("即时")

    first_call = source._safe_call.await_args_list[0]
    assert first_call.args[:2] == ("stock_individual_fund_flow_rank", main_loader)
    assert first_call.kwargs == {"indicator": "今日"}
    assert frame.attrs["fund_flow_source"] == "eastmoney_via_akshare"
    assert frame.attrs["fund_flow_semantics"] == "main_order_net_flow"
    assert frame.attrs["fund_flow_source_version"] == "individual_fund_flow_eastmoney_akshare_v1"
    assert frame.attrs["fund_flow_clock_basis"] == "client_received_only"
    assert "fund_flow_observed_at" not in frame.attrs
    assert DataScheduler._parse_individual_fund_flow_df(frame, date.today()) == []


@pytest.mark.parametrize("clock_case", ["missing", "invalid", "nat", "stale", "previous_day", "future"])
def test_fund_source_clock_cannot_be_replaced_by_receipt_time(clock_case):
    received = datetime(2026, 9, 7, 10, 1, 2)
    quote = {
        "missing": None,
        "invalid": "invalid",
        "nat": pd.NaT,
        "stale": datetime(2026, 9, 7, 9, 30),
        "previous_day": datetime(2026, 9, 4, 15),
        "future": datetime(2026, 9, 7, 10, 1, 3),
    }[clock_case]
    frame = pd.DataFrame([{
        "代码": "000001", "今日主力净流入-净额": 100,
        "今日主力净流入-净占比": 1,
        "source_quote_at": quote, "received_at": received,
    }])
    assert DataScheduler._parse_individual_fund_flow_df(
        frame, received.date(), observed_at=received,
    ) == []


@pytest.mark.asyncio
async def test_cached_tencent_attribute_cannot_resurrect_expired_fund_clock(scheduler_db_env, monkeypatch):
    now = datetime.now()
    old = now - timedelta(seconds=601)
    frame = pd.DataFrame([{
        "代码": "000001", "今日主力净流入-净额": 100, "今日主力净流入-净占比": 1,
        "source_quote_at": old, "received_at": old,
    }])
    frame.attrs.update({
        "fund_flow_source": "tencent",
        "fund_flow_source_version": "tencent_hsfundtab_v1",
        "fund_flow_observed_at": old,
    })
    scheduler = DataScheduler()
    scheduler._tradeable_codes = ["000001"]
    fetch = AsyncMock(return_value=frame)
    forbidden = AsyncMock(side_effect=AssertionError("no old-source resurrection"))
    scheduler._sources = {
        "tencent": SimpleNamespace(get_individual_fund_flow=fetch),
        "eastmoney": SimpleNamespace(get_individual_fund_flow=forbidden),
        "akshare": SimpleNamespace(get_individual_fund_flow=forbidden,
                                   get_sector_fund_flow=AsyncMock(return_value=pd.DataFrame())),
    }
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trading_hours", AsyncMock(return_value=True))
    async with scheduler_db_env() as session:
        session.add(FundFlow(code="000001", trade_date=now.date(), main_net_inflow=7, observed_at=old))
        await session.commit()
    await scheduler._intraday_slow()
    async with scheduler_db_env() as session:
        persisted = list((await session.scalars(select(FundFlow))).all())
    assert len(persisted) == 1
    assert persisted[0].main_net_inflow == 7
    assert persisted[0].observed_at == old
    assert persisted[0].source_quote_at is None
    assert persisted[0].received_at is None
    fetch.assert_awaited_once_with(["000001"])
    forbidden.assert_not_awaited()
