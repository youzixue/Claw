"""⑦ P0-1：`open_noise_end` 保持 09:45 —— 把"离线实验否决"变成可执行约束。

离线回放（9/01–9/17，n=33，见
`outputs/today_review_20260917/P0-1-09-45分批释放离线实验-20260918.md`）：
把释放时刻后移到 10:15 / 10:45，或分批到三个时点，价格改善分别为
+0.22pp / +0.04pp / +0.02pp，95%CI 全部跨 0，且**杀跌日一律恶化**
（−0.30 / −0.44 / −0.47pp）；94% 的样本在备选时刻之前就已跌破基线成交价。
接受标准"改善≥0.5pp 且三种风格均不恶化"未达成 → 维持 09:45 整点释放。

集中释放（46.5% 的卖出落在 5 分钟窗口）是**症状**，病因是软信号门槛过低与
T 机制把弱信号升级为清仓（改1/改2/改3）。
"""
from __future__ import annotations

from app.config.settings import settings


def test_open_noise_end_stays_at_0945():
    assert settings.PAPER_AUTO_OPEN_NOISE_END == "09:45", (
        "P0-1 离线实验已否决后移释放时刻；若要用新样本重开，"
        "先按同一口径复算并更新文档与注释"
    )


def test_rejection_and_experiment_numbers_are_documented():
    """否决依据必须留在配置里，避免后来者只看到'集中释放'就改时点。"""
    import app.config.settings as settings_module

    source = open(settings_module.__file__, encoding="utf-8").read()
    start = source.index("=== P0-1 已实验否决")
    block = source[start:start + 2200]
    for token in ("46.5%", "10:15", "10:45", "0.5pp", "杀跌日", "症状"):
        assert token in block, token


def test_experiment_script_is_reproducible():
    """离线实验必须留在仓库里可重跑，不能只有结论。"""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    script = repo / "scripts" / "p0_1_open_noise_release_experiment.py"
    assert script.is_file()
    body = script.read_text(encoding="utf-8")
    # 口径必须写死在脚本里：样本区间、判据上限、三种备选释放时刻
    for token in ("2026-09-01", "2026-09-17", "0.5", "10:15", "10:45", "install_extra_ca_bundle"):
        assert token in body, token
    # 禁止用未校验的 TLS 上下文取数据（只看代码，模块 docstring 里是"不使用"的说明）
    import ast

    tree = ast.parse(body)
    doc = ast.get_docstring(tree) or ""
    code = body.replace(doc, "")
    assert "_create_unverified_context" not in code
    assert "import ssl" not in code
