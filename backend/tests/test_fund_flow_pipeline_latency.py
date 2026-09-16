"""Tencent-only live funds; slow concept metadata must not delay persistence."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from sqlalchemy import select

from app.config.settings import settings
from app.data import scheduler as scheduler_module
from app.data.scheduler import DataScheduler
from app.models.stock import FundFlow, StockSpot
from test_scheduler_kline_fill import scheduler_db_env


def individual_frame():
    received_at = datetime.now()
    return pd.DataFrame([{
        "source_quote_at": received_at - timedelta(seconds=1),
        "received_at": received_at,
        "代码": "000001", "名称": "测试",
        "今日主力净流入-净额": 123456, "今日主力净流入-净占比": 2.5,
        "今日大单净流入-净额": 10, "今日中单净流入-净额": -20,
        "今日小单净流入-净额": -30,
    }])


def ths_individual_frame():
    return pd.DataFrame([{
        "股票代码": "000002", "股票简称": "备用源测试",
        "最新价": 12.3, "涨跌幅": 1.2, "换手率": 3.4,
        "流入资金": "1.2亿", "流出资金": "0.8亿",
        "净额": "4000万", "成交额": "5亿",
    }])


def test_ranked_fallback_pages_are_deduplicated_by_code():
    frame = pd.concat([individual_frame(), individual_frame()], ignore_index=True)
    frame.loc[1, "今日主力净流入-净额"] = 50_000_000
    records = DataScheduler._parse_individual_fund_flow_df(
        frame,
        scheduler_module.date.today(),
        source="eastmoney_via_akshare",
        source_version="individual_fund_flow_eastmoney_akshare_v1",
    )
    assert len(records) == 1
    assert records[0]["code"] == "000001"
    assert records[0]["main_net_inflow"] == 50_000_000


def tencent_frame(frame=None):
    frame = individual_frame() if frame is None else frame.copy()
    frame.attrs.update(fund_flow_source="tencent",
                       fund_flow_source_version="tencent_hsfundtab_v1")
    return frame


def live_scheduler(fetch, concept=None):
    scheduler = DataScheduler()
    scheduler._tradeable_codes = ["000001", "000002"]
    forbidden = AsyncMock(side_effect=AssertionError("no old individual-fund fallback"))
    scheduler._sources = {
        "tencent": SimpleNamespace(get_individual_fund_flow=fetch),
        "eastmoney": SimpleNamespace(get_individual_fund_flow=forbidden),
        "akshare": SimpleNamespace(
            get_individual_fund_flow=forbidden,
            get_sector_fund_flow=concept or AsyncMock(return_value=pd.DataFrame()),
        ),
        "ths": SimpleNamespace(get_individual_fund_flow=forbidden),
    }
    return scheduler, forbidden


@pytest.mark.asyncio
async def test_individual_funds_commit_while_concept_call_is_still_running(scheduler_db_env, monkeypatch):
    release = asyncio.Event()
    calls = 0

    async def slow_concept(*args):
        nonlocal calls
        calls += 1
        await release.wait()
        return pd.DataFrame()

    fetch = AsyncMock(return_value=tencent_frame())
    scheduler, forbidden = live_scheduler(fetch, slow_concept)
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trading_hours", AsyncMock(return_value=True))
    monkeypatch.setattr(settings, "FUND_FLOW_CONCEPT_WAIT_TIMEOUT_SEC", 0.01)
    try:
        for _ in range(2):
            await asyncio.wait_for(scheduler._intraday_slow(), 2)
        assert calls == 1
        assert fetch.await_count == 2
        forbidden.assert_not_awaited()
        assert scheduler.get_pipeline_runtime_status()["concept_fund_flow_pending"] is True
        async with scheduler_db_env() as db:
            rows = list((await db.scalars(select(FundFlow))).all())
            assert len(rows) == 1
            assert rows[0].main_net_inflow == 123456
            assert rows[0].source == "tencent"
            assert rows[0].source_version == "tencent_hsfundtab_v1"
            assert rows[0].source_quote_at < rows[0].received_at <= rows[0].observed_at
        release.set()
        task = scheduler._concept_fund_flow_task
        await task
        result = await scheduler._collect_concept_fund_flow_bounded()
        assert isinstance(result, pd.DataFrame) and result.empty
        assert scheduler._concept_fund_flow_task is None
        assert calls == 1
    finally:
        release.set()
        task = scheduler._concept_fund_flow_task
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_individual_funds_do_not_publish_total_flow_as_main_flow(scheduler_db_env, monkeypatch):
    frame = tencent_frame(ths_individual_frame())
    now = datetime.now()
    frame["source_quote_at"] = now - timedelta(seconds=2)
    frame["received_at"] = now - timedelta(seconds=1)
    fetch = AsyncMock(return_value=frame)
    scheduler, forbidden = live_scheduler(fetch)
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trading_hours", AsyncMock(return_value=True))
    await scheduler._intraday_slow()
    fetch.assert_awaited_once_with(["000001", "000002"])
    forbidden.assert_not_awaited()
    async with scheduler_db_env() as db:
        assert list((await db.scalars(select(FundFlow))).all()) == []


@pytest.mark.asyncio
async def test_partial_tencent_coverage_is_published_without_old_fallback(scheduler_db_env, monkeypatch):
    frame = tencent_frame()
    frame.attrs["fund_flow_expected_count"] = 2
    fetch = AsyncMock(return_value=frame)
    scheduler, forbidden = live_scheduler(fetch)
    scheduler._tradeable_codes = ["000004", "000001"]
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trading_hours", AsyncMock(return_value=True))
    monkeypatch.setattr(settings, "FUND_FLOW_INDIVIDUAL_FALLBACK_MIN_COVERAGE", 0.95)
    success = AsyncMock()
    monkeypatch.setattr(scheduler_module.data_quality_guard, "record_success", success)
    async with scheduler_db_env() as db:
        db.add_all(StockSpot(code=f"00000{i}", name=f"行情{i}") for i in range(1, 4))
        await db.commit()

    await scheduler._intraday_slow()
    fetch.assert_awaited_once_with(["000001", "000002", "000003", "000004"])
    forbidden.assert_not_awaited()
    async with scheduler_db_env() as db:
        rows = list((await db.scalars(select(FundFlow))).all())
        assert len(rows) == 1 and rows[0].code == "000001"
        assert rows[0].name == "行情1"
    slice_call = next(c for c in success.await_args_list
                      if c.args[1:3] == ("tencent", "individual_fund_flow_round"))
    universe = next(c for c in success.await_args_list
                    if c.args[1:3] == ("tencent", "individual_fund_flow"))
    assert slice_call.kwargs["record_count"] == 1
    assert slice_call.kwargs["expected_count"] == 2
    assert universe.kwargs["record_count"] == 1
    assert universe.kwargs["expected_count"] == 4


@pytest.mark.asyncio
async def test_main_fund_health_counts_rolling_fresh_tencent_universe_not_full_slice(scheduler_db_env, monkeypatch):
    now = datetime.now()
    first, second = tencent_frame(), tencent_frame()
    second.loc[0, "代码"] = "000003"
    for item in (first, second):
        item.attrs["fund_flow_expected_count"] = 1
    fetch = AsyncMock(side_effect=[first, second])
    scheduler, forbidden = live_scheduler(fetch)
    scheduler._tradeable_codes = [f"{number:06d}" for number in range(1, 6)]
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trading_hours", AsyncMock(return_value=True))
    success = AsyncMock()
    monkeypatch.setattr(scheduler_module.data_quality_guard, "record_success", success)

    def stored(code, *, source="tencent", version="tencent_hsfundtab_v1", age=2):
        return FundFlow(
            code=code, trade_date=now.date(), main_net_inflow=100, main_net_inflow_pct=1,
            source=source, source_version=version,
            source_quote_at=now - timedelta(seconds=age),
            received_at=now - timedelta(seconds=age - 1),
            observed_at=now - timedelta(seconds=age - 1),
        )

    async with scheduler_db_env() as db:
        db.add_all([
            stored("000002"),                    # still fresh from a preceding round
            stored("000003", age=601),           # prior Tencent evidence has expired
            stored("000004", source="eastmoney", version="individual_fund_flow_v3_f124"),
            stored("000005", version="unknown"), # source name alone is not qualification
            stored("600519"),                    # not in this requested universe
        ])
        await db.commit()

    for cumulative_count in (2, 3):
        success.reset_mock()
        await scheduler._intraday_slow()
        rounds = [c for c in success.await_args_list
                  if c.args[1:3] == ("tencent", "individual_fund_flow_round")]
        main = [c for c in success.await_args_list
                if c.args[1:3] == ("tencent", "individual_fund_flow")]
        assert len(rounds) == len(main) == 1
        assert rounds[0].kwargs["record_count"] == rounds[0].kwargs["expected_count"] == 1
        assert main[0].kwargs["record_count"] == cumulative_count
        assert main[0].kwargs["expected_count"] == 5
        assert not any(c.args[2] == "individual_fund_flow_universe"
                       for c in success.await_args_list)
    assert fetch.await_count == 2
    forbidden.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["empty", "exception", "missing_source", "wrong_version",
                                  "missing_clock", "unrequested_identity"])
async def test_bad_tencent_funds_never_call_old_sources_or_fill_zero(scheduler_db_env, monkeypatch, case):
    frame = tencent_frame()
    if case == "empty":
        frame = pd.DataFrame()
    elif case == "missing_source":
        frame.attrs.pop("fund_flow_source")
    elif case == "wrong_version":
        frame.attrs["fund_flow_source_version"] = "field50"
    elif case == "missing_clock":
        frame = frame.drop(columns=["source_quote_at"])
    elif case == "unrequested_identity":
        frame.loc[0, "代码"] = "600519"
    fetch = (AsyncMock(side_effect=RuntimeError("source unavailable")) if case == "exception"
             else AsyncMock(return_value=frame))
    scheduler, forbidden = live_scheduler(fetch)
    monkeypatch.setattr(scheduler_module.trade_calendar, "is_trading_hours", AsyncMock(return_value=True))
    await scheduler._intraday_slow()
    fetch.assert_awaited_once_with(["000001", "000002"])
    forbidden.assert_not_awaited()
    async with scheduler_db_env() as db:
        assert list((await db.scalars(select(FundFlow))).all()) == []
    assert scheduler._concept_fund_flow_task is None


@pytest.mark.asyncio
async def test_stop_cleans_up_retained_concept_task():
    scheduler = DataScheduler()
    task = asyncio.create_task(asyncio.Event().wait())
    scheduler._concept_fund_flow_task = task
    scheduler.stop()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
