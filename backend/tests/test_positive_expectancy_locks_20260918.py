"""正期望子集扫描的结论护栏：改这些门槛前必须先面对这几组数字。

扫描（`scripts/positive_expectancy_subset_scan.py`，2026-09-18，可重跑）
----------------------------------------------------------------------
信号日 2026-09-01→09-16，入场=次日开盘，摩擦 0.15% 往返，
判据 `n >= 30` 且**净期望 95%CI 下界 > 0** 才叫 CANDIDATE。

E（高标，连板 4–8 + 封单 + 炸板 + 入场涨幅，共 48 个组合）：
  封单≥1.0亿/炸板≤2/入场涨幅≤3.0%   n=8   均值 **+0.25%** 中位 +1.19% 胜率 **75%**  ← 现行配置
  封单≥1.0亿/炸板≤2/入场涨幅≤不限    n=10  均值 −0.07%（净）
  封单≥0.7亿/炸板≤2/入场涨幅≤3.0%   n=11  均值 −1.64%
  封单≥0.5亿/炸板≤2/入场涨幅≤3.0%   n=14  均值 −1.89%
  封单不设限/炸板≤2/入场涨幅≤3.0%   n=15  均值 −1.14%
  把池子放宽到 3–8 板 + 现行门槛    n=17  均值 **−1.72%** 胜率 59%
  把池子放宽到 3–8 板 + 封单≥0.7亿  n=34  均值 −1.12% → **负期望（样本达标）**
  → **现行 E 配置是全部组合里唯一均值为正的；放宽门槛或放宽池子都变负。**

F（断板反包）：
  断板前连板≥3              n=11  均值 +1.19% 胜率 55%   ← 现行
  断板前连板≥2              n=22  均值 −0.23%
  断板前连板≥1              n=80  净 −0.42% CI[−1.70] → **负期望（样本达标）**
  连板≥3 且 深跌≤−5%        n=10  均值 +2.86% 胜率 60%   ← 现行完整门槛，最好
  → **放宽 F 的连板门槛在大样本上被否决。**

引擎首板路线（次日开盘买入，未计门槛）：
  `pre_board_probe_start`  n=14,087 净 −0.19%  → 负
  `news_catalyst_start`    n=3,394  净 −0.35%  → 负
  `auction_surge_start`    n=699    净 −0.65%  → 负
  `second_board_promotion` n=1,045  净 −0.75%  → 负
  `mainline_spread_start`  n=536    净 +0.00% CI[−0.21] → 零/负
  **唯二 CANDIDATE**：
    `oversold_reversal_start` n=3,394 净 **+0.30%** CI 下界 +0.19%
    `relay_fillup`            n=52    净 **+1.08%** CI 下界 +0.19%

本文件的作用
------------
把"这些门槛已被证据检验过、且放宽方向为负"固定下来。任何人要改
`PAPER_HIGHBOARD_*` / `PAPER_TENBAGGER_MAX_INTRADAY_CONFIRM_CHANGE_PCT` /
`PAPER_REVERSAL_MIN_CONSECUTIVE` 时，必须先跑扫描脚本并解释为什么新数字
推翻上面这些结论。
"""

from __future__ import annotations

from pathlib import Path

from app.config.settings import settings

REPO = Path(__file__).resolve().parents[2]


def test_e_highboard_thresholds_are_the_scanned_optimum():
    """E 的现行门槛是 48 个扫描组合里唯一均值为正的，不得无证据放宽。"""
    assert settings.PAPER_HIGHBOARD_MIN_CONSECUTIVE == 4, (
        "扫描显示放宽池子到 3-8 板会把均值从 +0.25% 拉到 −1.72%（n=17），"
        "并在 n=34 上确认负期望；改前先重跑 positive_expectancy_subset_scan"
    )
    assert settings.PAPER_HIGHBOARD_MAX_CONSECUTIVE == 8
    assert settings.PAPER_HIGHBOARD_MIN_SEAL_AMOUNT == 1.0, (
        "扫描显示封单门槛降到 0.7/0.5/不设限，均值分别恶化到 −1.64%/−1.89%/−1.14%"
    )
    assert settings.PAPER_HIGHBOARD_MAX_BREAK_COUNT == 2
    assert settings.PAPER_TENBAGGER_MAX_INTRADAY_CONFIRM_CHANGE_PCT == 3.0, (
        "放宽入场涨幅上限的组合（5%/不限）均值与胜率都不优于 3%"
    )


def test_f_reversal_consecutive_threshold_is_evidence_backed():
    """F 的连板门槛：放宽到 ≥1 已在 n=80 上确认负期望。"""
    assert settings.PAPER_REVERSAL_MIN_CONSECUTIVE == 3, (
        "扫描：≥3 均值 +1.19%（n=11，胜率 55%）；≥2 −0.23%（n=22）；"
        "≥1 净 −0.42% CI[−1.70]（n=80，样本达标且为负）。放宽方向被否决。"
    )
    assert settings.PAPER_REVERSAL_MIN_DIP_PCT == -5.0, (
        "「连板≥3 且 深跌≤−5%」是扫描里最好的组合（+2.86%，n=10）；勿放宽深跌条件"
    )
    assert settings.PAPER_REVERSAL_MAX_INTRADAY_CONFIRM_CHANGE_PCT == 3.0


def test_scan_tool_is_reproducible_and_read_only():
    """结论必须可重跑；脚本不得写库、不得推测不可见字段。"""
    script = REPO / "scripts" / "positive_expectancy_subset_scan.py"
    assert script.is_file()
    body = script.read_text(encoding="utf-8")
    for token in ("mode=ro", "MIN_SAMPLE", "FRICTION_PCT", "CANDIDATE", "证据不足"):
        assert token in body, token
    # 只读：不得出现写语句
    for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP "):
        assert forbidden not in body, forbidden


def test_candidate_subset_rule_is_not_loosened_silently():
    """判定必须同时要求样本量与置信区间下界，不能只看均值。"""
    script = (REPO / "scripts" / "positive_expectancy_subset_scan.py").read_text(encoding="utf-8")
    assert "MIN_SAMPLE = 30" in script
    assert '"net"] - FRICTION_PCT > 0' in script or "lo\"] - FRICTION_PCT > 0" in script
