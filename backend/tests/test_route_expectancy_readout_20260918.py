"""路线期望判据护栏：判"能不能开单"必须用引擎结算口径 + 两段同号。

为什么要锁
----------
我用"次日开盘买入"的口径（丢掉隔夜跳空）把 `oversold_reversal_start`
读成净期望 +0.30%（n=3,394）并判为候选。用**引擎自己结算的**真实涨跌
（`promotion_prediction_record.actual_close_change_pct`，n=9,470、
发现窗与样本外两段独立窗口）复核：

    oversold_reversal_start  合并净 −0.147%，净CI下界 −0.214%，
                             发现窗 +0.138% / 样本外 −0.148%（不同号）→ 否决
    mainline_spread_start    发现窗 −0.622% / 样本外 +0.274%（不同号）→ 否决
    second_board_promotion   合并净 +0.641%，净CI下界 +0.502%，
                             两段 +0.764% / +0.801%（同号为正）→ **唯一可开单**

即：**现有在交易的那条路线（B 用 second_board_promotion）恰好是唯一通过
全部判据的**；其余未交易的路线在证据上都不该开单。

本文件把判据与这条更正固化，防止有人再用乐观口径或单窗口结论去开门。
"""
from __future__ import annotations

import inspect
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
READOUT = REPO / "scripts" / "route_expectancy_readout.py"
SCAN = REPO / "scripts" / "positive_expectancy_subset_scan.py"


def test_both_tools_exist_and_are_read_only():
    for path in (READOUT, SCAN):
        assert path.is_file(), path
        body = path.read_text(encoding="utf-8")
        assert "mode=ro" in body
        for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP "):
            assert forbidden not in body, (path.name, forbidden)


def test_open_metric_is_documented_as_optimistic():
    """扫描脚本必须自带口径警告，不能只靠人记住。"""
    body = SCAN.read_text(encoding="utf-8")
    assert "丢掉隔夜跳空" in body
    assert "route_expectancy_readout" in body, "必须指向权威判据工具"


def test_readout_criteria_are_pinned():
    """四条判据逐条固定：样本量、净CI下界、两段同号、以引擎结算为准。"""
    body = READOUT.read_text(encoding="utf-8")
    assert "MIN_SETTLED = 120" in body
    assert "FRICTION_PCT = 0.15" in body
    assert 'DISCOVERY = ("2026-09-01", "2026-09-16")' in body
    assert "两段窗口不同号" in body
    assert "继续观察" in body and "否决" in body and "可开单" in body
    # 数据源必须是引擎结算表，不得自算收益
    assert "promotion_prediction_record" in body
    assert "actual_close_change_pct" in body


def test_readout_uses_engine_settled_values_not_own_prices():
    """读数器不得读取 stock_kline 自算收益（那会重新引入口径偏差）。"""
    body = READOUT.read_text(encoding="utf-8")
    assert "stock_kline" not in body, "权威判据必须用引擎结算值，不能自算"


def test_verdict_requires_all_four_conditions():
    import importlib.util

    spec = importlib.util.spec_from_file_location("readout", READOUT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    pos = {"n": 500, "mean": 0.8, "ci_lo_net": 0.5}
    state, reasons = module.verdict(pos, pos, pos)
    assert state == "可开单" and reasons == []

    # 样本不足 → 继续观察（不是否决）
    state, reasons = module.verdict(pos, pos, {"n": 39, "mean": 0.8, "ci_lo_net": 0.5})
    assert state == "继续观察" and "39" in reasons[0]

    # 两段不同号 → 否决（即使合并看起来为正）
    state, reasons = module.verdict({"mean": -0.1}, {"mean": 0.3}, pos)
    assert state == "否决" and any("同号" in r for r in reasons)

    # 净CI下界不过 → 否决
    state, reasons = module.verdict(pos, pos, {"n": 9000, "mean": 0.003, "ci_lo_net": -0.214})
    assert state == "否决" and any("CI 下界" in r for r in reasons)


def test_documented_retraction_is_recorded_in_the_module_docstring():
    doc = (REPO / "scripts" / "route_expectancy_readout.py").read_text(encoding="utf-8")
    assert "该结论经引擎结算口径复核后被证伪" in doc
    assert "oversold_reversal_start" in doc
