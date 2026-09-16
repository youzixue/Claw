from datetime import date
from unittest.mock import AsyncMock

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.data.scheduler as scheduler_module
from app.data.scheduler import DataScheduler
from app.db.session import Base
from app.models.stock import StockBlacklist, StockTag


@pytest.mark.asyncio
async def test_stock_status_all_empty_fails_closed(monkeypatch):
    monkeypatch.setattr(
        scheduler_module.trade_calendar,
        "is_trade_day",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "pywencai.get",
        lambda **_kwargs: pd.DataFrame(),
    )

    class EmptySessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    failure = AsyncMock()
    monkeypatch.setattr(scheduler_module, "async_session", EmptySessionContext)
    monkeypatch.setattr(
        scheduler_module.data_quality_guard,
        "record_failure",
        failure,
    )

    result = await DataScheduler()._update_stock_status()

    assert result["status"] == "failed"
    assert "四个查询均为空" in result["reason"]
    failure.assert_awaited_once()
    assert failure.await_args.args[1:3] == ("pywencai", "stock_status")


@pytest.mark.asyncio
async def test_stock_status_positive_merge_preserves_existing_blacklist(
    monkeypatch,
    tmp_path,
):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'stock_status.db'}",
        future=True,
    )
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        session.add(
            StockTag(
                code="000001",
                name="旧名称",
                board_type="main_sz",
                board_tag="blocked",
                is_st=True,
                is_suspended=False,
                is_delisting=False,
            )
        )
        session.add(
            StockBlacklist(
                code="000001",
                reason="st",
                start_date=date(2026, 9, 1),
                auto_expire=True,
                source="auto",
            )
        )
        await session.commit()

    monkeypatch.setattr(
        scheduler_module.trade_calendar,
        "is_trade_day",
        AsyncMock(return_value=True),
    )

    def fake_get(*, query, loop):
        assert loop is True
        if query == "ST股":
            return pd.DataFrame(
                [{"股票代码": "000001", "股票简称": "测试ST"}]
            )
        return pd.DataFrame(columns=["股票代码", "股票简称"])

    monkeypatch.setattr("pywencai.get", fake_get)
    monkeypatch.setattr(scheduler_module, "async_session", session_factory)

    await DataScheduler()._update_stock_status()

    async with session_factory() as session:
        tag = await session.scalar(
            select(StockTag).where(StockTag.code == "000001")
        )
        blacklist = await session.scalar(
            select(StockBlacklist).where(StockBlacklist.code == "000001")
        )

    assert tag is not None
    assert tag.name == "测试ST"
    assert tag.is_st is True
    assert tag.board_tag == "blocked"
    assert blacklist is not None
    assert blacklist.reason == "st"
    assert blacklist.start_date == date(2026, 9, 1)
    assert blacklist.auto_expire is True  # 保留旧值，不重建名单。

    await engine.dispose()
