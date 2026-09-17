"""锁住可执行收益实验脚本的口径决定，防止回退成"拿结算列当期望"。

背景：`promotion_prediction_record.actual_close_change_pct` 实测等于
`stock_kline.change_pct(outcome_trade_date)`（close(P)→close(P+1)），是**预测
命中口径**。我曾据此得出"second_board_promotion 是唯一可开单路线"，
被本实验证伪：B 的候选主要来自 P 日收盘后批次，P 日收盘价买不到，
P+1 入场者是在**付出**隔夜跳空，可执行收益符号相反。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "executable_expectancy_promotion_routes.py"


def _code_only() -> str:
    """去掉模块 docstring 后的代码部分。

    docstring 里**必须**提到 `actual_close_change_pct`，因为那正是本实验要
    说明的反例口径；所以断言必须只看代码，不能看文档。
    """
    source = SCRIPT.read_text(encoding="utf-8")
    doc = ast.get_docstring(ast.parse(source)) or ""
    return source.replace(doc, "", 1)


def test_script_exists_and_is_read_only():
    assert SCRIPT.is_file()
    body = SCRIPT.read_text(encoding="utf-8")
    assert "mode=ro" in body, "必须只读打开数据库"
    for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP "):
        assert forbidden not in body, (forbidden,)


def test_script_uses_real_quotes_for_entry_not_settled_column():
    body = SCRIPT.read_text(encoding="utf-8")
    code = _code_only()
    assert "quote_rounds" in body, "入场价必须取逐轮真实报价归档"
    assert "price" in body
    assert "actual_close_change_pct" not in code, (
        "代码不得把结算列当收益；它只允许出现在说明口径的文档里"
    )


def test_script_documents_the_known_coverage_gaps():
    """必须在输出里显式声明三个缺口：量比缺失、报价非成交价、样本仅 9 日。"""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "volume_ratio" in body, "必须说明归档无量比"
    assert "缩量未复刻" in body
    assert "入场价是「该轮报价」而非成交价" in body
    assert "9 个买入日" in body
    assert "E(十倍)/F(反转)" in body, "必须说明本脚本不覆盖 E/F"


def test_script_replicates_the_real_admission_contract():
    """三条路线的准入/确认带/门槛必须与 paper.py + settings 对齐。"""
    body = SCRIPT.read_text(encoding="utf-8")
    # C 的准入是 actionable OR rank_scope（paper.py:5126-5129）
    assert "actionable_or_ranked" in body
    assert "ranked" in body and "recall_ranked" in body
    # 三条路线的确认带
    for token in ('"min_confirm": 0.0, "max_confirm": 5.8',
                  '"min_confirm": -0.5, "max_confirm": 4.0',
                  '"min_confirm": 1.0, "max_confirm": 6.0'):
        assert token.replace('"min_confirm"', "min_confirm").replace('"max_confirm"', "max_confirm") in body \
            or token in body, token
    # C/D 不套绝对概率门槛
    assert body.count('"enforce_floor": False') == 2


def test_script_avoids_the_sqlite_truthiness_trap():
    """`1 is not True` 为 True —— 曾导致候选池恒为空。必须用真值判断。"""
    # 查 AST 里是否真的存在 `X is not True` / `X is True` 表达式。
    # 注释里提到这个陷阱是正当的，所以不能靠字符串匹配。
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    offenders = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops)
        and any(isinstance(c, ast.Constant) and c.value is True for c in node.comparators)
    ]
    assert not offenders, f"不得用 `is True` 比较 sqlite 的 1/0（行 {[o.lineno for o in offenders]}）"
    code = _code_only()
    assert "if not actionable" in code or "bool(actionable)" in code
