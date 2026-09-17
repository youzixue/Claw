"""锁住 E/F 可执行收益实验脚本的口径决定。

E/F 与 B/C/D 不同：它们是**直读 `limit_up_pool`** 的独立候选机制，
且 `_tenbagger_midline_candidates` / `_reversal_pullback_candidates` 都用
`_spot_by_code`（当前实时行情）做盘中确认，**无法回放历史**。
所以本脚本用「K线+涨停池重建信号 + 归档重放盘中确认」的组合，
这些口径决定必须锁住。
"""
from __future__ import annotations

import ast
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[2]
          / "scripts" / "executable_expectancy_ef.py")


def _code_only() -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    doc = ast.get_docstring(ast.parse(source)) or ""
    return source.replace(doc, "", 1)


def test_script_is_read_only_over_historical_sources():
    body = SCRIPT.read_text(encoding="utf-8")
    assert "mode=ro" in body
    for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP "):
        assert forbidden not in body, forbidden
    assert "limit_up_pool" in body, "E/F 的信号来源必须是涨停池"
    assert "quote_rounds" in body, "入场必须用逐轮报价归档重放"


def test_script_does_not_call_the_live_spot_functions():
    """不得调用 E/F 的候选函数：它们用当前实时行情，回放历史会引入未来数据。"""
    code = _code_only()
    for name in ("_tenbagger_midline_candidates", "_reversal_pullback_candidates",
                 "_spot_by_code"):
        assert name not in code, f"不得调用 {name}（用当前行情，无法回放历史）"


def test_script_replicates_engine_entry_gates():
    body = SCRIPT.read_text(encoding="utf-8")
    # E 用 PAPER_HIGHBOARD_* 前缀（账户6），不是 PAPER_TENBAGGER_*
    assert "E_MIN_CONSEC, E_MAX_CONSEC = 4, 8" in body
    assert "E_MIN_SEAL = 1.0e8" in body and "E_MAX_BREAK = 2" in body
    # F 的形态门槛
    assert "F_MIN_CONSEC = 3" in body and "F_MAX_GAP = 3" in body
    assert "F_MIN_DIP = -5.0" in body and "F_MIN_VOL_RATIO = 1.5" in body
    # 盘中确认
    assert "require_above_vwap" in body or "price < avg" in body
    assert "max_pullback" in body


def test_script_declares_its_known_gaps():
    """必须在输出里声明三个缺口，不得假装复刻完整。"""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "max_peak_change_pct" in body and "未复刻" in body
    assert "先止损" in body, "同根K线同时触及TP/SL 必须声明保守假设"
    assert "样本" in body and "不足以" in body or "样本量可能不足" in body


def test_script_documents_that_ef_are_unproven_not_negative():
    body = SCRIPT.read_text(encoding="utf-8")
    assert "证据不足" in body or "无法判定" in body
