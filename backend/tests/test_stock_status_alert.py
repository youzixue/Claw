"""风险标签流水线的静默失效告警。

背景：`_update_stock_status`（每交易日 08:25）自 2026-08 下旬起连续失败，
`stock_tags` 的 ST/停牌/退市标记冻结在 08-31 整整 **16 天无人发现** ——
因为失败只留了一条 WARNING，被行情日志淹没。

本用例锁定告警的两个关键性质：
1. 失败次数取自既有 `data_source_health.fail_streak`，不另起计数器；
2. 取数失败**不得**影响主流程（告警自身的异常必须被吞掉）。
"""
from __future__ import annotations

from datetime import date, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.data.scheduler import (
    STOCK_STATUS_ALERT_MARKER,
    STOCK_STATUS_ALERT_THRESHOLD,
    _stock_status_fail_streak,
)
from app.db.session import Base
from app.models.risk import DataSourceHealth

# 项目未开启 asyncio_mode=auto，异步用例需显式标记
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def session(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'alert.db'}", future=True
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with maker() as db:
        yield db
    await engine.dispose()


def test_alert_marker_is_stable_and_greppable():
    """标记必须稳定且不含易变内容，否则无法被检索/订阅。"""
    assert STOCK_STATUS_ALERT_MARKER
    assert "stock_tags" in STOCK_STATUS_ALERT_MARKER
    assert STOCK_STATUS_ALERT_THRESHOLD >= 2, "阈值 1 会把偶发抖动当故障"


async def test_streak_zero_when_no_rows(session: AsyncSession):
    assert await _stock_status_fail_streak(session) == 0


async def test_streak_reads_max_across_rows(session: AsyncSession):
    """同一 api_name 可能有多行（历史 upsert 遗留），取最大值。"""
    session.add_all([
        DataSourceHealth(source="pywencai", api_name="stock_status", status="down",
                         fail_streak=3, updated_at=datetime.now()),
        DataSourceHealth(source="pywencai", api_name="stock_status", status="down",
                         fail_streak=48, updated_at=datetime.now()),
        DataSourceHealth(source="pywencai", api_name="other", status="down",
                         fail_streak=999, updated_at=datetime.now()),
    ])
    await session.flush()
    assert await _stock_status_fail_streak(session) == 48


async def test_streak_helper_never_raises(session: AsyncSession, monkeypatch):
    """告警辅助函数本身出错时返回 0，绝不能反向影响主流程。"""
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(session, "scalar", boom)
    assert await _stock_status_fail_streak(session) == 0


@pytest.mark.parametrize("streak", [0, 1, 2, 16, 48])
async def test_streak_returns_exact_value(session: AsyncSession, streak: int):
    session.add(DataSourceHealth(source="pywencai", api_name="stock_status",
                                 status="down", fail_streak=streak,
                                 updated_at=datetime.now()))
    await session.flush()
    assert await _stock_status_fail_streak(session) == streak
