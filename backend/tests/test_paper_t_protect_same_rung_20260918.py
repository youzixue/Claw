"""改2：T 减仓不得用同一条信号升级为清仓。

缺陷
----
弱信号 -> `T减仓` 33%~50% -> 同一条弱信号再次触发 -> `T后保护卖出` 卖掉剩余全部。
于是 T 机制"日内高抛低吸降成本"的实际效果 = 弱信号即清仓。
实测历史 14 次 `T后保护卖出`，其中 **8 次（57%）** 触发它的 rung 与当日
T 减仓是同一条 —— 同一份证据被计了两次。

修法
----
`_post_t_protect_escalation_allowed` 要求升级引入**当日尚未用过的 rung**；
同一条 rung 重复出现只维持"已减仓、等回补或保护卖点"。
不同 rung（T减仓来自止盈、保护来自冲高回落）仍照常全退。
回滚点：`PAPER_AUTO_T_PROTECT_REQUIRE_NEW_RUNG=False`。
"""
from __future__ import annotations

import pytest

from app.api.v1 import paper
from app.config.settings import settings
from app.paper.account_policy import account_sell_params


@pytest.mark.parametrize("reason,expected", [
    # 同一 rung 的数值千差万别，必须归一成可比较的身份
    ("跌破分时均价VWAP58.63", "跌破分时均价"),
    ("跌破分时均价VWAP9.10", "跌破分时均价"),
    ("T减仓400股：跌破分时均价VWAP58.63", "跌破分时均价"),
    ("T后保护卖出：盘中冲高回落2.53%", "盘中冲高回落"),
    ("盘中冲高回落1.80%", "盘中冲高回落"),
    ("触发短线止盈3.20%", "触发短线止盈"),
    ("5分钟急跌-0.72%", "5分钟急跌"),
    ("跌破5日线MA5=10.02", "跌破5日线"),
    ("板块退潮：传媒走弱", "板块退潮"),
    ("", ""),
])
def test_exit_reason_rung_key_normalizes_numbers_and_prefixes(reason, expected):
    assert paper._exit_reason_rung_key(reason) == expected


@pytest.mark.parametrize("reason,previous", [
    # 8/14 实测同 rung 案例
    ("盘中冲高回落2.53%", {"盘中冲高回落"}),
    ("跌破开盘价-0.31%", {"跌破开盘价"}),
    ("盘中收弱：收盘价接近日内低点", {"盘中收弱"}),
    ("跌破分时均价VWAP9.10", {"跌破分时均价"}),
])
def test_same_rung_cannot_escalate_to_full_liquidation(reason, previous):
    allowed, why = paper._post_t_protect_escalation_allowed(
        reason=reason, previous_rungs=previous,
    )
    assert allowed is False
    assert "同一份证据" in why and "T后保护卖出需新证据" in why


@pytest.mark.parametrize("reason,previous", [
    # 6/14 实测不同 rung 案例：T 减仓来自止盈，保护来自冲高回落 —— 合法升级
    ("盘中冲高回落2.53%", {"触发短线止盈"}),
    ("回落成本线保护", {"盘中冲高回落"}),
    ("跌破开盘价-0.31%", {"触发短线止盈"}),
])
def test_different_rung_still_escalates(reason, previous):
    allowed, why = paper._post_t_protect_escalation_allowed(
        reason=reason, previous_rungs=previous,
    )
    assert allowed is True and why == ""


def test_first_weak_exit_of_the_day_is_never_blocked_here():
    """当日还没有任何减仓时，本闸门不参与判定（由 T 减仓金额逻辑处理）。"""
    allowed, why = paper._post_t_protect_escalation_allowed(
        reason="跌破分时均价VWAP9.10", previous_rungs=set(),
    )
    assert allowed is True and why == ""


def test_setting_can_roll_back_to_previous_behaviour():
    allowed, _ = paper._post_t_protect_escalation_allowed(
        reason="盘中冲高回落2.53%", previous_rungs={"盘中冲高回落"},
        params={"t_protect_require_new_rung": False},
    )
    assert allowed is True
    assert settings.PAPER_AUTO_T_PROTECT_REQUIRE_NEW_RUNG is True


def test_parameter_is_frozen_into_the_account_sell_profile():
    """隐式退出依赖必须随账户参数冻结，否则无法审计与回放。"""
    for account in ("default", "promotion", "mainline", "challenger_b", "challenger_a"):
        profile = account_sell_params(account)
        assert "t_protect_require_new_rung" in profile, account
        assert profile["t_protect_require_new_rung"] is True


def test_every_post_t_protect_rung_is_recognized():
    """`_is_post_t_protect_exit_reason` 列出的 rung 都必须能被归一化。"""
    for rung in ("回落成本线保护", "盘中冲高回落", "盘中收弱", "跌破分时均价",
                 "跌破开盘价", "5分钟急跌", "盘口卖压增强", "跌破5日线", "板块退潮"):
        assert paper._is_post_t_protect_exit_reason(rung)
        assert paper._exit_reason_rung_key(rung) == rung


@pytest.mark.asyncio
async def test_today_sell_stats_exposes_rungs_for_the_gate(tmp_path):
    """`_today_sell_stats` 必须把当日已用过的 rung 带出来，否则闸门拿不到证据。"""
    from datetime import date, datetime

    import pytest_asyncio
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.db.session import Base
    from app.models.paper import PaperTradeLog

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as db:
            for minute, reason in ((30, "T减仓400股：跌破分时均价VWAP9.10"),
                                   (45, "T后保护卖出：盘中冲高回落2.53%")):
                db.add(PaperTradeLog(
                    account_id=12, code="600000", trade_type="sell",
                    trade_time=datetime(2026, 9, 17, 9, minute),
                    price=10.0, amount=400, reason=reason,
                ))
            await db.commit()
            stats = await paper._today_sell_stats(db, 12, "600000", date(2026, 9, 17))
        assert stats["rungs"] == {"跌破分时均价", "盘中冲高回落"}
        assert len(stats["reasons"]) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_today_sell_stats_empty_case_exposes_empty_rungs(tmp_path):
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.db.session import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'e.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with maker() as db:
            stats = await paper._today_sell_stats(db, 12, "600000", date(2026, 9, 17))
        assert stats["amount"] == 0 and stats["rungs"] == set() and stats["reasons"] == []
    finally:
        await engine.dispose()
