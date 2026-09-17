"""⑤ H1：A 策略必要价格区间的隐含振幅上限 = 5.102%（可执行文档化）。

背景
----
`_a_entry_price_band` 把 A 原入口的两条约束取交集：
    下界 = high × (1 − dd)   dd = PAPER_INTRADAY_CONFIRM_MAX_PULLBACK_FROM_HIGH_PCT
    上界 = low  × (1 + rb)   rb = PAPER_AUTO_VALUE_ENTRY_MAX_REBOUND_FROM_LOW_PCT
交集非空 <=> high/low − 1 <= rb/(1−dd) + dd/(1−dd)
取 dd=2%、rb=3% → 5.102%。

本文件的作用是让这条"隐含约束"成为可执行事实：
1) 把 5.102% 由真实配置推导出来，配置一改测试立刻失败，文档不会悄悄过期；
2) 锁住下界/上界的**参数来源**，防止把 `PAPER_AUTO_PULLBACK_FROM_HIGH_PCT`
   （=2.5，与入口区间无关）误当成下界参数；
3) 用真实行情形态验证"振幅 > 5.102% 时区间必为空"这一结论。
"""
from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.api.v1 import paper as paper_module
from app.config.settings import settings
from app.paper.account_policy import account_confirmation_policy

# 文档中向用户承诺的上限；改这里必须同步 outputs/ 下的复盘文档。
DOCUMENTED_AMPLITUDE_CAP_PCT = 5.102


def _implied_amplitude_cap_pct(drawdown: float, rebound: float) -> float:
    """由两条约束推导的隐含振幅上限（百分数）。"""
    return (1.0 + rebound / 100.0) / (1.0 - drawdown / 100.0) * 100.0 - 100.0


def _band_params(account: str = "default") -> tuple[float, float]:
    return (
        float(account_confirmation_policy(account)["max_pullback_from_high_pct"]),
        float(settings.PAPER_AUTO_VALUE_ENTRY_MAX_REBOUND_FROM_LOW_PCT),
    )


def test_documented_cap_matches_the_two_live_parameters():
    drawdown, rebound = _band_params()
    assert drawdown == 2.0, "dd 变了要同步更新复盘文档里的 5.102%"
    assert rebound == 3.0, "rb 变了要同步更新复盘文档里的 5.102%"
    assert _implied_amplitude_cap_pct(drawdown, rebound) == pytest.approx(
        DOCUMENTED_AMPLITUDE_CAP_PCT, abs=1e-3,
    )


def test_band_lower_bound_comes_from_the_confirmation_policy_not_pullback_param():
    """回归护栏：下界参数来源必须仍是 A 的确认策略回撤上限。"""
    lower_dd = float(account_confirmation_policy("default")["max_pullback_from_high_pct"])
    assert lower_dd == settings.PAPER_INTRADAY_CONFIRM_MAX_PULLBACK_FROM_HIGH_PCT
    # 相邻但无关的参数；若有人把它接进入口区间，这里的等式会被破坏。
    assert settings.PAPER_AUTO_PULLBACK_FROM_HIGH_PCT != lower_dd


def _spot(*, high: float, low: float, change_pct: float = 3.0):
    return SimpleNamespace(
        high=high, low=low, change_pct=change_pct, price=(high + low) / 2,
    )


def test_band_is_empty_just_above_the_documented_cap():
    drawdown, rebound = _band_params()
    cap = _implied_amplitude_cap_pct(drawdown, rebound)
    low = 10.0
    # 恰好在上限之上：下界过高，交集为空
    high = low * (1.0 + (cap + 0.5) / 100.0)
    assert paper_module._a_entry_price_band(_spot(high=high, low=low))["status"] == "empty"


def test_band_is_nonempty_just_below_the_documented_cap():
    drawdown, rebound = _band_params()
    cap = _implied_amplitude_cap_pct(drawdown, rebound)
    low = 10.0
    high = low * (1.0 + (cap - 0.5) / 100.0)
    band = paper_module._a_entry_price_band(_spot(high=high, low=low))
    assert band["status"] == "nonempty"
    assert band["lower_price"] <= band["upper_price"]
    # 交集非空不等于可买：审计函数永远不授权入场。
    assert band["sufficient_for_entry"] is False


@pytest.mark.parametrize("change_pct", [0.0, -1.0])
def test_band_not_applicable_without_positive_change(change_pct):
    band = paper_module._a_entry_price_band(
        _spot(high=10.5, low=10.0, change_pct=change_pct)
    )
    assert band["status"] == "not_applicable"


def test_documentation_names_the_real_parameters():
    """settings.py 的 H1 注释必须指向真正生效的两个参数。"""
    import app.config.settings as settings_module

    source = open(settings_module.__file__, encoding="utf-8").read()
    assert "隐含振幅上限" in source
    assert "5.102%" in source
    block_start = source.index("=== 2026-09-18 文档化：A 策略必要价格区间的隐含振幅上限 ===")
    block = source[block_start:block_start + 1800]
    assert "PAPER_INTRADAY_CONFIRM_MAX_PULLBACK_FROM_HIGH_PCT" in block
    assert "PAPER_AUTO_VALUE_ENTRY_MAX_REBOUND_FROM_LOW_PCT" in block
    assert "_a_entry_price_band" in block
