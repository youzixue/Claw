"""竞价证据必须使用真实观测时钟，调度目标时间不能倒填。"""
from datetime import date, datetime
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from sqlalchemy import select

from app.data.scheduler import DataScheduler
from app.models.stock import AuctionData
from app.strategy.auction import AuctionCollector
from test_scheduler_kline_fill import scheduler_db_env


def _cron_field(job, name: str) -> str:
    return str(next(field for field in job.trigger.fields if field.name == name))


def _frame(observed_at: datetime) -> pd.DataFrame:
    frame = pd.DataFrame([{
        "code": "000001",
        "open": 10.2,
        "prev_close": 10.0,
        "volume": 1000,
        "amount": 1020000,
        "volume_ratio": 1.5,
    }])
    frame.attrs["auction_observed_at"] = observed_at
    frame.attrs["auction_clock_basis"] = "client_received_at"
    return frame


def test_scheduler_registers_distinct_early_and_terminal_auction_jobs():
    scheduler = DataScheduler()
    scheduler.setup_jobs()

    early = scheduler.scheduler.get_job("auction_collect_0920")
    terminal = scheduler.scheduler.get_job("auction_collect_0925")
    assert early is not None
    assert terminal is not None
    assert (_cron_field(early, "hour"), _cron_field(early, "minute"), _cron_field(early, "second")) == (
        "9", "20", "6",
    )
    assert (
        _cron_field(terminal, "hour"),
        _cron_field(terminal, "minute"),
        _cron_field(terminal, "second"),
    ) == ("9", "25", "6")
    assert scheduler.scheduler.get_job("auction_collect_0926") is None


@pytest.mark.asyncio
async def test_fixed_jobs_do_not_pass_target_time_as_observation(monkeypatch):
    scheduler = DataScheduler()
    collect = AsyncMock()
    monkeypatch.setattr(scheduler, "_auction_collect", collect)

    await scheduler._auction_collect_force_0920()
    await scheduler._auction_collect_force_0924()
    await scheduler._auction_collect_force_0925()

    assert [call.kwargs for call in collect.await_args_list] == [
        {"force": True}, {"force": True}, {"force": True},
    ]


@pytest.mark.asyncio
async def test_save_uses_response_observed_at_not_requested_label(scheduler_db_env):
    collector = AuctionCollector()
    observed_at = datetime(2026, 9, 7, 9, 20, 11)
    async with scheduler_db_env() as session:
        saved = await collector.save_auction_data(
            session,
            _frame(observed_at),
            date(2026, 9, 7),
            auction_time="09:20:06",
        )
        row = await session.scalar(select(AuctionData))

    assert saved == 1
    assert row.auction_time == "09:20:11"


@pytest.mark.asyncio
async def test_response_delayed_to_0930_cannot_be_saved_as_0925(scheduler_db_env):
    collector = AuctionCollector()
    async with scheduler_db_env() as session:
        saved = await collector.save_auction_data(
            session,
            _frame(datetime(2026, 9, 7, 9, 30, 0)),
            date(2026, 9, 7),
            auction_time="09:25:00",
        )
        rows = list((await session.scalars(select(AuctionData))).all())

    assert saved == 0
    assert rows == []


@pytest.mark.asyncio
async def test_stock_spot_fallback_is_disabled_even_if_called_directly(scheduler_db_env):
    collector = AuctionCollector()
    async with scheduler_db_env() as session:
        frame = await collector._collect_from_stock_spot(session, date(2026, 9, 7))
    assert frame.empty
