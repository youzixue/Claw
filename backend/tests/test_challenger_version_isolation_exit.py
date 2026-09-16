"""G6：空版本持仓不再被强制清仓（2026-09-17 用户授权「避免踏空」）。

原判据自相矛盾：`_legacy_challenger_exit_reasons` 对「已知版本不同」的持仓
明确保留（注释：执行器对已持有代码没有加仓路径、共享 broker 另会拒绝陈旧排队
成交，故清仓既非必要也非版本隔离的有效替代），却对「版本未知」强平。
「未知」并不比「已知不同」更该清仓 —— 两者都不是期望版本。

实测 2026-09-16 有 8 笔持仓因此被强制清仓。

已知不合格单帧版本（`_UNSAFE_POSITION_VERSION_PREFIXES`）**仍然清仓** ——
那是有依据的安全性判断，不在本次放宽范围。
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.session import Base
from app.models import paper as paper_models  # noqa: F401
from app.paper.strategy_iteration_challenger import (
    _UNSAFE_POSITION_VERSION_PREFIXES,
    _legacy_challenger_exit_reasons,
)

pytestmark = pytest.mark.asyncio

CURRENT = "abcdef_shape_v4_fill:0c71c4a060ed"


@pytest_asyncio.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'v.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


async def _reasons(session, versions: dict[str, str]) -> dict[str, str]:
    return await _legacy_challenger_exit_reasons(
        session,
        account_id=1,
        position_versions=versions,
        expected_strategy_version=CURRENT,
    )


async def test_unversioned_position_is_kept(session):
    """空版本 ⇒ 保留，不再强制清仓（本次放宽的核心）。"""
    assert await _reasons(session, {"600001": ""}) == {}


async def test_superseded_version_is_still_kept(session):
    """已知版本不同 ⇒ 保留（原有行为，不得回归）。"""
    assert await _reasons(session, {"600001": "abcdef_shape_v3_old:deadbeef"}) == {}


async def test_current_version_is_kept(session):
    assert await _reasons(session, {"600001": CURRENT}) == {}


@pytest.mark.parametrize("prefix", _UNSAFE_POSITION_VERSION_PREFIXES)
async def test_explicitly_unsafe_version_is_still_liquidated(session, prefix):
    """已知不合格单帧版本 ⇒ **仍然清仓**（安全性判断，未放宽）。"""
    reasons = await _reasons(session, {"600001": f"{prefix}abc"})
    assert "600001" in reasons
    assert "已知不合格单帧版本" in reasons["600001"]


async def test_mixed_positions_only_liquidate_the_unsafe_one(session):
    """混合场景：只清不合格的那个，其余保留。"""
    unsafe = f"{_UNSAFE_POSITION_VERSION_PREFIXES[0]}xyz"
    reasons = await _reasons(session, {
        "600001": "",                       # 空版本 -> 保留
        "600002": "abcdef_shape_v3_old:x",  # 旧版本 -> 保留
        "600003": CURRENT,                  # 当前版本 -> 保留
        "600004": unsafe,                   # 不合格 -> 清仓
    })
    assert set(reasons) == {"600004"}


async def test_empty_position_versions_returns_no_reasons(session):
    assert await _reasons(session, {}) == {}


async def test_ledger_detects_both_historical_and_current_exit_markers():
    """台账检测必须**同时**认历史文案与当前文案。

    库里已存的强平卖出用的是 2026-09-17 之前的文案。若只认新文案，
    这些历史强平会掉出版本隔离台账、被错记到新版本的 PnL。
    """
    from app.paper.strategy_iteration_challenger import _LEGACY_VERSION_EXIT_MARKERS

    assert "旧版或未标版本仓位隔离退出" in _LEGACY_VERSION_EXIT_MARKERS
    assert "已知不合格单帧版本仓位隔离退出" in _LEGACY_VERSION_EXIT_MARKERS
    for marker in _LEGACY_VERSION_EXIT_MARKERS:
        assert marker in f"{marker}：持仓版本=x，当前版本=y"
