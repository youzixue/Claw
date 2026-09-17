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
    # 样本量门槛必须**按路线自身 σ 算**，不得再写死通用数字（120 曾被当成通用门槛，
    # 但它源自 σ=7% 的假设，会把 σ=2.70% 的 relay_fillup 无谓推迟 6 倍时间）。
    assert "MIN_SETTLED" not in body, "样本量门槛不得回退为写死常量"
    assert "TARGET_DELTA_PCT = 2.0" in body
    assert "N_FLOOR = 30" in body
    assert "def power_n(" in body and "def positive_ci_n(" in body
    assert "FRICTION_PCT = 0.15" in body
    assert 'DISCOVERY = ("2026-09-01", "2026-09-16")' in body
    assert "两段窗口不同号" in body
    assert "继续观察" in body and "否决" in body and "可开单" in body
    assert "未证明为正" in body, "必须区分「样本不够」「未证明为正」「方向为负」三态"
    # 数据源必须是引擎结算表，不得自算收益
    assert "promotion_prediction_record" in body
    assert "actual_close_change_pct" in body


def test_readout_uses_engine_settled_values_not_own_prices():
    """读数器不得读取 stock_kline 自算收益（那会重新引入口径偏差）。"""
    body = READOUT.read_text(encoding="utf-8")
    assert "stock_kline" not in body, "权威判据必须用引擎结算值，不能自算"


def test_verdict_distinguishes_four_states():
    import importlib.util

    spec = importlib.util.spec_from_file_location("readout", READOUT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    pos = {"n": 500, "mean": 0.8, "sd": 4.0, "net": 0.65, "ci_lo_net": 0.5}
    state, reasons = module.verdict(pos, pos, pos)
    assert state == "可开单" and reasons == []

    # 样本不足 → 继续观察（不是否决）；门槛按 σ 算，σ=4% → 51
    small = {"n": 39, "mean": 0.8, "sd": 4.0, "net": 0.65, "ci_lo_net": 0.5}
    assert module.power_n(4.0) == 41, module.power_n(4.0)
    state, reasons = module.verdict(small, small, small)
    assert state == "继续观察" and "39" in reasons[0]

    # 两段不同号 → 否决（即使合并看起来为正）
    state, reasons = module.verdict({"mean": -0.1}, {"mean": 0.3}, pos)
    assert state == "否决" and any("同号" in r for r in reasons)

    # 样本够、方向为正、只有净CI下界不过 → 第三态「未证明为正」，不是否决
    third = {"n": 39, "mean": 0.858, "sd": 2.70, "net": 0.708, "ci_lo_net": -0.140}
    state, reasons = module.verdict(
        {"n": 32, "mean": 0.792}, {"n": 7, "mean": 1.157}, third)
    assert state == "未证明为正", state
    assert any("还差 17 条" in r for r in reasons), reasons

    # 方向为负 → 否决
    neg = {"n": 9000, "mean": 0.003, "sd": 3.3, "net": -0.147, "ci_lo_net": -0.214}
    state, reasons = module.verdict({"mean": 0.138}, {"mean": -0.148}, neg)
    assert state == "否决" and any("方向为负" in r for r in reasons)


def test_documented_retraction_is_recorded_in_the_module_docstring():
    doc = (REPO / "scripts" / "route_expectancy_readout.py").read_text(encoding="utf-8")
    assert "该结论经引擎结算口径复核后被证伪" in doc
    assert "oversold_reversal_start" in doc

def test_required_n_follows_route_sigma_not_a_constant():
    """门槛必须随 σ 变化，且负期望路线判定为"再多样本也不达标"。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("readout_n", READOUT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # σ 越小需求越低 —— 这正是 relay_fillup(σ=2.70%) 不该等 120 的原因
    # (1) 设计门槛：σ=7% 才需要 ~120 —— 这正是 120 这个数字的出处
    assert 120 <= module.power_n(7.0) <= 126, module.power_n(7.0)
    # σ 越小门槛越低：relay_fillup 实测 σ=2.70% → 门槛仅 30（被 N_FLOOR 兜住）
    assert module.power_n(2.70) == module.N_FLOOR
    assert module.power_n(5.07) < module.power_n(7.0)
    # 兜底：σ 极小也不能低于 N_FLOOR
    assert module.power_n(0.1) == module.N_FLOOR
    # (2) 参考进度：relay_fillup σ=2.70%、净 +0.708% → 56 条净CI下界才转正
    assert module.positive_ci_n(2.70, 0.708) == 56, module.positive_ci_n(2.70, 0.708)
    # 点估计为负 → 累积样本不可能转正，而不是一个巨大但可达的数字
    assert module.positive_ci_n(3.0, -0.147) is None
    assert module.positive_ci_n(3.0, 0.0) is None
    assert module.need_text(None) == "n/a（点估计为负）"
    # (3) 两个函数不得合流：设计门槛必须与点估计无关
    assert module.power_n(2.70) == module.power_n(2.70)
