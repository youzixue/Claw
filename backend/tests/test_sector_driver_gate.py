"""锁定「板块当日正向」判据的概念/行业双口径，以及下游交易门槛不被连带放开。

背景（2026-09-17）
------------------
牛股雷达上 `国投电力`/`中国神华`/`交通银行`/`工商银行`/`光大银行` 的
「主驱动板块」显示为「暂无明确主驱动」，而 125 条里 120 条有值。

根因不是缺数据，是三道关卡的叠加：
1. `is_excluded=1` 的泛标签被剔除（沪股通/融资融券/高股息精选/中特估100/证金持股…）；
2. 申万数字代码（sw_l1/l2/l3）被类型过滤；
3. `fund_flow > 0 且 change_pct > 0` 同时成立。

低波动大盘股身上「当日为正」的标签几乎必是第 1 类泛标签，于是全部落选。

行业资金流的口径问题
--------------------
`scheduler._update_sector_persistence` 把东财**二级行业**资金流按三级子行业
数量均分写入（``per_code_flow = round(fund_flow / len(sub_codes), 2)``），
所以三级行业拿到的是**聚合量的 1/N 份额**：
* 除以正整数**不翻转符号** —— 负值仍是二级行业真净流出；
* 但 1/N 后经 `round(..., 2)`，小正值会被抹成 0，从而被误判为非正向；
* 与概念板块「自身资金流」不可比。

故 `_is_positive_sector_candidate` 分口径：概念要求资金+价格双正，
行业只要求价格为正。

**边界**：本次只放宽上下文/展示候选池。真正控制买点与推送的四个门槛仍各自
要求 `fund_flow > 0`，本文件用测试把它们钉住，防止被连带放开。
"""
from __future__ import annotations

import inspect

import pytest

from app.signal.anomaly_scanner import (
    AnomalyScanner,
    _is_causal_trade_driver_sector,
    _is_positive_sector_candidate,
)

# ---------- 概念：资金与价格双正 ----------


@pytest.mark.parametrize(
    "change_pct,fund_flow,expected",
    [
        (1.20, 5.00, True),    # 双正
        (1.20, -5.00, False),  # 涨但资金净流出 —— 概念不接受
        (-0.50, 5.00, False),  # 资金流入但下跌
        (0.00, 5.00, False),   # 平盘不算涨
        (1.20, 0.00, False),   # 资金为零不算净流入
    ],
)
def test_concept_requires_both_price_and_flow_positive(change_pct, fund_flow, expected):
    assert (
        _is_positive_sector_candidate(
            {"sector_type": "concept", "change_pct": change_pct, "fund_flow": fund_flow}
        )
        is expected
    )


# ---------- 行业：只看价格 ----------


@pytest.mark.parametrize(
    "change_pct,fund_flow,expected",
    [
        (0.53, -2.41, True),   # 真实场景：公用事业-电力-水电（摊薄后为负）
        (0.02, 0.00, True),    # 1/N 后被 round 抹成 0 的小正值
        (1.00, 3.00, True),
        (-0.73, -0.98, False),  # 真实场景：银行-银行-国有大型银行（板块当日在跌）
        (-0.25, -0.90, False),  # 真实场景：煤炭-煤炭开采加工-煤炭开采
        (0.00, 5.00, False),
    ],
)
def test_industry_only_requires_price_positive(change_pct, fund_flow, expected):
    assert (
        _is_positive_sector_candidate(
            {"sector_type": "industry", "change_pct": change_pct, "fund_flow": fund_flow}
        )
        is expected
    )


def test_missing_metadata_is_not_a_candidate():
    """缺字段不得被当成正向（否则会凭空造出主驱动）。"""
    assert _is_positive_sector_candidate({}) is False
    assert _is_positive_sector_candidate(None) is False
    # 有涨跌幅但无板块类型 -> 按更严的概念口径，缺资金流即不通过
    assert _is_positive_sector_candidate({"change_pct": 1.0}) is False


def test_unknown_sector_type_falls_back_to_concept_rule():
    """未标注类型的板块按更严的概念口径处理，不放宽。"""
    assert (
        _is_positive_sector_candidate({"change_pct": 1.0, "fund_flow": -1.0}) is False
    )


# ---------- 判别函数本身仍要求因果口径 ----------


def test_generic_labels_are_still_not_causal_drivers():
    """放宽的是资金门槛，不是因果口径 —— 语义黑名单里的泛标签仍不得当主驱动。"""
    from app.signal.anomaly_scanner import NON_CAUSAL_LINKAGE_SECTOR_TOKENS

    for name in ("融资融券", "沪股通", "国企改革", "高股息精选", "证金持股"):
        assert any(token in name for token in NON_CAUSAL_LINKAGE_SECTOR_TOKENS), name
        assert (
            _is_causal_trade_driver_sector(
                {"sector_name": name, "sector_type": "concept", "source": "pywencai"}
            )
            is False
        )
    assert (
        _is_causal_trade_driver_sector(
            {
                "sector_name": "公用事业-电力-水电",
                "sector_type": "industry",
                "source": "pywencai",
            }
        )
        is True
    )


def test_generic_labels_have_two_independent_exclusion_paths():
    """泛标签由**两套独立机制**拦截，覆盖面不同，不能只依赖其中一套。

    1. **数据层** `SectorInfo.is_excluded == 1`
       —— 由 `app.data.sector_exclusions.EXCLUDED_CONCEPT_NAMES` 决定，
       扫描时在 `code_sector_map` 构造处直接剔除该板块。
    2. **语义层** `NON_CAUSAL_LINKAGE_SECTOR_TOKENS`
       —— 按子串匹配的硬编码黑名单，用于否决主驱动资格。

    实测 `同花顺中特估100` 只在第 1 套里；因此两套都必须保留 —— 只留第 2 套
    会让它与 `高股息精选`（两套都命中）命运不同，只留第 1 套则任何
    `is_excluded` 未及时更新的新泛标签都能当上主驱动。
    """
    from app.data.sector_exclusions import EXCLUDED_CONCEPT_NAMES, is_excluded_concept
    from app.signal.anomaly_scanner import NON_CAUSAL_LINKAGE_SECTOR_TOKENS

    def in_blacklist(name: str) -> bool:
        return any(token in name for token in NON_CAUSAL_LINKAGE_SECTOR_TOKENS)

    # 两套都命中
    for name in ("融资融券", "沪股通", "证金持股", "高股息精选"):
        assert name in EXCLUDED_CONCEPT_NAMES and in_blacklist(name), name
    # 仅数据层命中 —— 这一条正是两套机制必须并存的证据
    assert "同花顺中特估100" in EXCLUDED_CONCEPT_NAMES
    assert not in_blacklist("同花顺中特估100")
    assert is_excluded_concept("同花顺中特估100") is True


# ---------- 下游交易门槛不得被连带放开 ----------


@pytest.mark.parametrize(
    "func_name",
    ["_has_positive_sector_driver", "_resolve_sector_repair_driver"],
)
def test_scanner_trading_gates_still_require_positive_fund_flow(func_name):
    """这四个门槛直接控制买点/推送，本次刻意未改，必须仍要求资金为正。"""
    source = inspect.getsource(getattr(AnomalyScanner, func_name))
    assert 'fund_flow")) > 0' in source or "fund_flow > 0" in source


def test_tenbagger_gate_still_requires_positive_fund_flow():
    from app.api.v1 import tenbagger

    source = inspect.getsource(tenbagger._has_positive_driver_sector)
    assert 'fund_flow")) > 0' in source


def test_scanner_and_gate_deliberately_differ():
    """钉住「显示有主驱动 ≠ 买点门槛放行」这一有意为之的口径差。

    放宽前实测：120 条有主驱动的票里仍有 4 条被门槛拦住。
    若将来有人把二者改成同一判据，这个测试会失败，提醒同步评估影响。
    """
    industry_up_but_outflow = {
        "sector_type": "industry",
        "change_pct": 0.53,
        "fund_flow": -2.41,
        "sector_name": "公用事业-电力-水电",
        "source": "pywencai",
        "strength_score": 7.7,
        "limit_up_count": 1,
    }
    # 上下文判据：接受它作为主驱动候选
    assert _is_positive_sector_candidate(industry_up_but_outflow) is True
    # 买点门槛：仍拒绝（资金为负、强度不足）
    from app.api.v1.tenbagger import _has_positive_driver_sector

    assert _has_positive_driver_sector({"sector_factors": [industry_up_but_outflow]}) is False


# ---------- 无主驱动时单元格必须仍有信息量 ----------


@pytest.mark.parametrize(
    "factors,expected_fragment",
    [
        (
            [
                {"sector_name": "跨境支付(CIPS)", "change_pct": -0.21, "fund_flow": -6.82},
                {"sector_name": "银行-银行-国有大型银行", "change_pct": -0.73, "fund_flow": -0.98},
            ],
            "跨境支付(CIPS) -0.21%／资金-6.82亿",
        ),
        (
            [{"sector_name": "绿色电力", "change_pct": 0.88, "fund_flow": -17.8}],
            "绿色电力 +0.88%／资金-17.80亿",
        ),
    ],
)
def test_reference_hint_shows_sector_state(factors, expected_fragment):
    """无因果主驱动时，副标题要给出参考板块的涨跌与资金，而不是只有占位文案。"""
    from app.api.v1.tenbagger import _format_reference_hint

    out = _format_reference_hint(factors)
    assert expected_fragment in out
    assert out.startswith("无因果主驱动 · 参考：")


def test_reference_hint_mentions_more_when_multiple():
    from app.api.v1.tenbagger import _format_reference_hint

    two = [
        {"sector_name": "A概念", "change_pct": 1.0, "fund_flow": -1.0},
        {"sector_name": "B概念", "change_pct": 0.5, "fund_flow": -2.0},
    ]
    assert "共2个" in _format_reference_hint(two)
    assert "共" not in _format_reference_hint(two[:1])


def test_reference_hint_empty_when_nothing_to_show():
    """没有参考板块、或参考板块无名字时返回空串，让调用方保留原文案。"""
    from app.api.v1.tenbagger import _format_reference_hint

    assert _format_reference_hint([]) == ""
    assert _format_reference_hint([{"sector_name": "", "change_pct": 1.0}]) == ""
    assert _format_reference_hint(None) == ""


def test_reference_hint_falls_back_to_original_copy():
    """副标题表达式：有主驱动用指标串，无主驱动优先参考板块，最后才是原文案。"""
    import inspect

    from app.api.v1 import tenbagger

    source = inspect.getsource(tenbagger._build_aggregated_anomaly_row)
    assert "_format_reference_hint(reference_factors)" in source
    assert "or \"暂无明确主驱动，先看个股盘口与量价确认\"" in source


# ---------- 参考板块：行业优先 ----------


def _concept(name, code=None):
    return {"sector_name": name, "sector_type": "concept", "sector_code": code or f"pw_concept_{name}"}


def _industry(name, code=None):
    return {"sector_name": name, "sector_type": "industry", "sector_code": code or f"pw_industry_{name}"}


def test_reference_selection_puts_industry_first():
    """行业必须排首位 —— 概念有 +2.0 基础分，不做处理必然被挤出前 2。"""
    from app.signal.anomaly_scanner import _select_reference_factors

    out = _select_reference_factors([_concept("A"), _concept("B"), _industry("银行")])
    assert out[0]["sector_type"] == "industry"
    assert out[0]["sector_name"] == "银行"


def test_reference_selection_moves_industry_to_front_even_when_present():
    """行业已在列表里但不在首位时也要提前 —— 只判断「在不在」不够。"""
    from app.signal.anomaly_scanner import _select_reference_factors

    out = _select_reference_factors([_concept("A"), _industry("银行")])
    assert [i["sector_name"] for i in out] == ["银行", "A"]


def test_reference_selection_keeps_concepts_when_no_industry():
    from app.signal.anomaly_scanner import _select_reference_factors

    out = _select_reference_factors([_concept("A"), _concept("B")])
    assert [i["sector_name"] for i in out] == ["A", "B"]


def test_reference_selection_real_china_shenhua_case():
    """真实场景：行业被两个概念挤出前 2，现在必须回到首位。"""
    from app.signal.anomaly_scanner import _select_reference_factors

    out = _select_reference_factors([
        _concept("绿色电力"),
        _concept("煤化工概念"),
        _industry("煤炭-煤炭开采加工-煤炭开采"),
    ])
    assert out[0]["sector_name"] == "煤炭-煤炭开采加工-煤炭开采"
    assert len(out) == 2


def test_reference_selection_handles_empty_and_single():
    from app.signal.anomaly_scanner import _select_reference_factors

    assert _select_reference_factors([]) == []
    assert [i["sector_name"] for i in _select_reference_factors([_industry("银行")])] == ["银行"]


def test_hint_prefers_industry_regardless_of_order():
    """展示层显式优先行业，不依赖上游排序。"""
    from app.api.v1.tenbagger import _format_reference_hint

    hint = _format_reference_hint([
        _concept("跨境支付(CIPS)") | {"change_pct": -0.21, "fund_flow": -6.82},
        _industry("银行-银行-国有大型银行") | {"change_pct": -0.73, "fund_flow": -0.98},
    ])
    assert "银行-银行-国有大型银行" in hint
    assert "跨境支付" not in hint


def test_hint_falls_back_to_concept_when_no_industry():
    from app.api.v1.tenbagger import _format_reference_hint

    hint = _format_reference_hint([
        _concept("绿色电力") | {"change_pct": 0.88, "fund_flow": -17.8},
    ])
    assert "绿色电力" in hint
