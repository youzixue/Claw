"""⑥ P1-2：A 路由不加涨停排板能力 —— 把"实验否决"变成可执行约束。

实验（9/03–9/17，见 settings.py 对应注释与
outputs/today_review_20260917/P1-2-A路由涨停排板取证-20260918.md）：
被 `卖一无量或已封板，影子成交不可实现` 淘汰的 33 条事件中，
剔除尚无次日 K 线的 4 条后 n=29：
  次日开盘溢价 mean −0.01%（扣成本 −0.16%），95%CI 含 0；
  次日收盘溢价 mean +1.51%，t=+1.36，95%CI [−0.66%, +3.68%] 含 0，
  按 σ=5.97%、δ=1% 需 n≈364 才有 80% 检验力（现有 29）。
接受标准"次日平均溢价>1% 且 n≥15"未达成 → 维持封板即放弃。

本文件锁定：(1) A 路由没有排板开关；(2) 已有排板开关只属于挑战者E/高标账户；
(3) 实验口径（剔除无次日K线）不被放宽。
"""
from __future__ import annotations

from app.config.settings import settings

# 挑战者E 与高标账户各自的排板开关；A 路由不得复用它们。
_OTHER_ACCOUNT_QUEUE_FLAGS = (
    "PAPER_CHALLENGER_E_LIMIT_UP_QUEUE_ENABLED",
    "PAPER_HIGHBOARD_LIMIT_UP_QUEUE_ENABLED",
)


def test_a_route_has_no_limit_up_queue_switch():
    names = [name for name in dir(settings) if "LIMIT_UP_QUEUE" in name]
    assert sorted(names) == sorted(_OTHER_ACCOUNT_QUEUE_FLAGS), (
        f"A 路由出现了排板开关，但实验已否决；若确要重开，先按同一口径复算并更新文档: {names}"
    )
    for name in ("PAPER_MOMENTUM_RETEST_LIMIT_UP_QUEUE_ENABLED",
                 "PAPER_CHALLENGER_A_LIMIT_UP_QUEUE_ENABLED"):
        assert not hasattr(settings, name), f"{name} 不应存在"


def test_momentum_retest_candidate_band_excludes_limit_up():
    """A 路由候选区间上限 6% 低于主板涨停 10%/ST 5%，结构性不含封板标的。

    这是"封板即放弃"的参数化实现：即使不显式加排板开关，
    候选区间本身也不会把涨停股送进确认流程。
    """
    assert settings.PAPER_MOMENTUM_RETEST_CANDIDATE_MAX_CHANGE_PCT == 6.0
    assert settings.PAPER_MOMENTUM_RETEST_CANDIDATE_MIN_CHANGE_PCT == 3.0
    # 主板涨停 10%、ST 5%；上限 6% 都够不到，只有 ST 的 5% 落在区间内。
    assert settings.PAPER_MOMENTUM_RETEST_CANDIDATE_MAX_CHANGE_PCT < 10.0


def test_rejection_is_documented_with_the_experiment_result():
    """否决结论必须留在配置里，避免后来者只看到"少一个能力"而重新加上。"""
    import app.config.settings as settings_module

    source = open(settings_module.__file__, encoding="utf-8").read()
    start = source.index("=== A 路由不做涨停排板")
    block = source[start:start + 2000]
    assert "n=29" in block
    assert "+1.51%" in block and "−0.01%" in block
    assert "363" in block or "364" in block
    assert "封板即放弃" in block
