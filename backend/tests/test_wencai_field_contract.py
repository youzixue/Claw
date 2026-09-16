"""锁定 `WencaiStreamSource` 与旧 pywencai 的**字段契约**一致性。

背景（2026-09-17 实测）
-----------------------
上游 `stream-query` 把多值字段作为 JSON 数组返回，经 pandas 成为 Python ``list``；
旧 pywencai 返回的是**字符串**。下游 `scheduler` 按旧契约解析：

* 盘前 `_premarket`：``str(row["所属同花顺行业"])`` → ``f"pw_industry_{ind}"``
  作为 ``sector_code`` 写入 ``SectorInfo``；
* 盘后 `_after_market`：同上写入 ``stock_sector_mapping``，概念按 ``split(";")``；
* 股票状态：校验 ``股票代码`` 必须是可归一化的 ``str``。

若形状不还原，``str(list)`` 会让**每只股票生成一个唯一 sector_code**：
实测 200 行样本会产出 200 个"行业板块"（正常为 92），全 A 股 5574 只
会产出 5574 个假板块，并污染 ``stock_sector_mapping``（7.5 万条健康数据）。

旧契约基线（由库内 7.5 万条历史映射实测）
----------------------------------------
* 行业 264 个，**全部**含 ``-``（同花顺三级全名）；
* 概念 395 个，**零个**含 ``;`` 或 ``[``；
* 概念集 ∩ 行业集 = 0，行业 L3(257 个) ∩ 概念集 = 0
  —— 故「从概念中剔除行业三级名」有实测依据。
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.data.sources.wencai_stream_source import (
    _CONCEPT_COLUMN,
    _INDUSTRY_COLUMN,
    _LIST_SEPARATOR,
    _to_frame,
    WencaiStreamError,
    normalize_list_columns,
)


def _frame(**columns) -> pd.DataFrame:
    """按“每列一个 Series”构造，避免 pandas 把嵌套 list 当作多行展开。"""
    return pd.DataFrame(
        {name: pd.Series(values, dtype=object) for name, values in columns.items()}
    )


# ---------- 行业：list -> "一级-二级-三级" ----------


def test_industry_list_joined_with_hyphen():
    frame = _frame(**{_INDUSTRY_COLUMN: [["机械设备", "专用设备", "能源及重型设备"]]})
    out = normalize_list_columns(frame)
    assert out[_INDUSTRY_COLUMN].iloc[0] == "机械设备-专用设备-能源及重型设备"
    assert isinstance(out[_INDUSTRY_COLUMN].iloc[0], str)


def test_industry_string_passthrough_unchanged():
    """旧式字符串输入必须原样保留（幂等，不重复处理）。"""
    frame = _frame(**{_INDUSTRY_COLUMN: ["医药生物-中药-中药Ⅲ"]})
    out = normalize_list_columns(frame)
    assert out[_INDUSTRY_COLUMN].iloc[0] == "医药生物-中药-中药Ⅲ"


def test_industry_parts_are_stripped():
    frame = _frame(**{_INDUSTRY_COLUMN: [[" 计算机 ", " IT服务 ", " IT服务Ⅲ "]]})
    out = normalize_list_columns(frame)
    assert out[_INDUSTRY_COLUMN].iloc[0] == "计算机-IT服务-IT服务Ⅲ"


def test_industry_none_and_nan_preserved():
    """空行业不得被拼成 list 的 repr；下游靠 `if ind and ind != "nan"` 跳过。"""
    frame = _frame(**{_INDUSTRY_COLUMN: [None, float("nan"), []]})
    out = normalize_list_columns(frame)
    assert pd.isna(out[_INDUSTRY_COLUMN].iloc[0])
    assert pd.isna(out[_INDUSTRY_COLUMN].iloc[1])
    # 空数组连接成空串，下游视为缺失（falsy），不会生成 `pw_industry_`
    assert out[_INDUSTRY_COLUMN].iloc[2] == ""
    for value in out[_INDUSTRY_COLUMN]:
        assert "[" not in str(value) and "'" not in str(value)


def test_placeholder_elements_are_dropped():
    """占位值不得成为板块名 —— 库内曾实存 16 条 `pw_concept_None`。"""
    frame = _frame(
        **{
            _CONCEPT_COLUMN: [
                ["机器人概念", None, "nan", "", "None", "NULL", "军工"],
                ["NaN", "-"],
            ]
        }
    )
    out = normalize_list_columns(frame)
    assert out[_CONCEPT_COLUMN].iloc[0] == "机器人概念;军工"
    # 全占位 -> 空串，下游 `if con_str and con_str != "nan"` 会跳过
    assert out[_CONCEPT_COLUMN].iloc[1] == ""
    assert not any(
        "None" in str(v) or "nan" in str(v).lower() for v in out[_CONCEPT_COLUMN]
    )


def test_placeholder_in_industry_is_dropped():
    """占位值被清洗掉，剩下的 3 个真实层级仍然通过 3 元守卫。"""
    frame = _frame(**{_INDUSTRY_COLUMN: [["电子", None, "半导体", "半导体材料"]]})
    out = normalize_list_columns(frame)
    assert out[_INDUSTRY_COLUMN].iloc[0] == "电子-半导体-半导体材料"


# ---------- 概念：list -> 剔除行业三级 -> ";" 连接 ----------


def test_concepts_joined_with_semicolon_and_industry_removed():
    frame = _frame(
        **{
            _INDUSTRY_COLUMN: [["机械设备", "专用设备", "能源及重型设备"]],
            _CONCEPT_COLUMN: [
                ["新股与次新股", "融资融券", "机械设备", "专用设备", "能源及重型设备"]
            ],
        }
    )
    out = normalize_list_columns(frame)
    value = out[_CONCEPT_COLUMN].iloc[0]
    assert value == "新股与次新股;融资融券"
    # 行业三级名必须全部从概念中消失
    for part in ("机械设备", "专用设备", "能源及重型设备"):
        assert part not in value.split(";")


def test_concepts_industry_leak_is_removed_even_when_not_suffix():
    """行业三级名可能夹在概念列表中间，不能只做尾部裁剪。"""
    frame = _frame(
        **{
            _INDUSTRY_COLUMN: [["计算机", "IT服务", "IT服务Ⅲ"]],
            _CONCEPT_COLUMN: [["AIGC概念", "计算机", "IT服务", "IT服务Ⅲ", "AI内容审核"]],
        }
    )
    out = normalize_list_columns(frame)
    assert out[_CONCEPT_COLUMN].iloc[0] == "AIGC概念;AI内容审核"


def test_concept_separator_is_semicolon_not_comma():
    """下游按 `;` 切分；`;` 必须存在，`,` 不得成为分隔符。"""
    frame = _frame(**{_CONCEPT_COLUMN: [["A", "B", "C"]]})
    out = normalize_list_columns(frame)
    assert out[_CONCEPT_COLUMN].iloc[0] == "A;B;C"
    assert out[_CONCEPT_COLUMN].iloc[0].split(";") == ["A", "B", "C"]
    assert _LIST_SEPARATOR == ";"


def test_concepts_without_industry_column_still_joined():
    """缺行业列时不得丢概念，只是无法剔除。"""
    frame = _frame(**{_CONCEPT_COLUMN: [["A", "B"]]})
    out = normalize_list_columns(frame)
    assert out[_CONCEPT_COLUMN].iloc[0] == "A;B"


def test_concepts_string_passthrough_unchanged():
    frame = _frame(**{_CONCEPT_COLUMN: ["机器人概念;无人机;军工"]})
    out = normalize_list_columns(frame)
    assert out[_CONCEPT_COLUMN].iloc[0] == "机器人概念;无人机;军工"


def test_multi_row_industry_drop_is_row_aligned():
    """逐行对齐：每行只剔除**自己**的行业名，不得串行剔除别行的行业名。"""
    frame = _frame(
        **{
            _INDUSTRY_COLUMN: [
                ["电子", "半导体", "半导体材料"],
                ["医药生物", "中药", "中药Ⅲ"],
            ],
            _CONCEPT_COLUMN: [
                ["芯片概念", "中药", "电子"],
                ["创新药", "电子", "医药生物"],
            ],
        }
    )
    out = normalize_list_columns(frame)
    # 第 0 行：只剔除「电子」；「中药」属于第 1 行行业，必须保留
    assert out[_CONCEPT_COLUMN].iloc[0] == "芯片概念;中药"
    # 第 1 行：只剔除「医药生物」；「电子」属于第 0 行行业，必须保留
    assert out[_CONCEPT_COLUMN].iloc[1] == "创新药;电子"


# ---------- 一对多 / 去重 / 行业层级守卫 ----------


def test_concept_duplicates_are_deduped_order_preserving():
    """上游确有行内重复（5574 行中 149 行）；不去重会让 stock_count 重复计数。"""
    frame = _frame(**{_CONCEPT_COLUMN: [["融资融券", "芯片概念", "融资融券", "军工"]]})
    out = normalize_list_columns(frame)
    assert out[_CONCEPT_COLUMN].iloc[0] == "融资融券;芯片概念;军工"
    assert out[_CONCEPT_COLUMN].iloc[0].split(";").count("融资融券") == 1


def test_industry_duplicates_are_deduped():
    frame = _frame(**{_INDUSTRY_COLUMN: [["电子", "半导体", "半导体"]]})
    out = normalize_list_columns(frame)
    assert out[_INDUSTRY_COLUMN].iloc[0] == "电子-半导体"


def test_one_stock_maps_to_many_concepts():
    """1:N —— N 个概念必须产出 N 条映射记录，不能被截断。"""
    concepts = [f"概念{i}" for i in range(30)]
    frame = _frame(
        **{
            _INDUSTRY_COLUMN: [["电子", "半导体", "半导体材料"]],
            _CONCEPT_COLUMN: [concepts],
        }
    )
    out = normalize_list_columns(frame)
    parts = [c for c in out[_CONCEPT_COLUMN].iloc[0].split(";") if c]
    assert len(parts) == 30
    assert parts == concepts


def test_multi_stock_multi_concept_row_alignment():
    """多股票 × 多概念：每行的概念数必须与自己的一致，不得错行。"""
    frame = _frame(
        **{
            _INDUSTRY_COLUMN: [
                ["电子", "半导体", "半导体材料"],
                ["医药生物", "中药", "中药Ⅲ"],
                ["计算机", "IT服务", "IT服务Ⅲ"],
            ],
            _CONCEPT_COLUMN: [
                ["A", "B"],
                ["C", "D", "E", "F"],
                ["G"],
            ],
        }
    )
    out = normalize_list_columns(frame)
    counts = [len([c for c in str(v).split(";") if c]) for v in out[_CONCEPT_COLUMN]]
    assert counts == [2, 4, 1]


def test_industry_multiple_hierarchies_raises_instead_of_garbage():
    """6 元（= 2 个三级层级）说明上游口径变了，必须抛错而不是拼成 A-B-C-D-E-F。"""
    frame = _frame(
        **{
            _INDUSTRY_COLUMN: [
                ["电子", "半导体", "半导体材料", "计算机", "IT服务", "IT服务Ⅲ"]
            ]
        }
    )
    with pytest.raises(WencaiStreamError, match="三级分类"):
        normalize_list_columns(frame)


@pytest.mark.parametrize("bad", [["电子"], ["电子", "半导体"], ["a", "b", "c", "d"]])
def test_industry_non_three_length_raises(bad):
    frame = _frame(**{_INDUSTRY_COLUMN: [bad]})
    with pytest.raises(WencaiStreamError):
        normalize_list_columns(frame)


def test_industry_empty_list_is_allowed():
    """空行业 = 该股无行业分类，应降级为空串而不是抛错。"""
    frame = _frame(**{_INDUSTRY_COLUMN: [[]], _CONCEPT_COLUMN: [["A"]]})
    out = normalize_list_columns(frame)
    assert out[_INDUSTRY_COLUMN].iloc[0] == ""
    assert out[_CONCEPT_COLUMN].iloc[0] == "A"


def test_industry_all_placeholders_is_allowed():
    """全占位值清洗后为空 -> 视为无行业，不触发长度守卫。"""
    frame = _frame(**{_INDUSTRY_COLUMN: [[None, "nan", ""]]})
    out = normalize_list_columns(frame)
    assert out[_INDUSTRY_COLUMN].iloc[0] == ""


# ---------- 兜底与其他数组列 ----------


def test_unknown_list_column_is_joined():
    frame = _frame(其他=["a", "b"], 兜底列=[["x", "y"], ["z"]])
    out = normalize_list_columns(frame)
    assert out["兜底列"].iloc[0] == "x;y"
    assert out["兜底列"].iloc[1] == "z"
    assert out["其他"].iloc[0] == "a"


def test_empty_frame_returned_as_is():
    out = normalize_list_columns(pd.DataFrame())
    assert out.empty


def test_normalize_is_idempotent():
    frame = _frame(
        **{
            _INDUSTRY_COLUMN: [["电子", "半导体", "半导体材料"]],
            _CONCEPT_COLUMN: [["芯片概念", "电子"]],
        }
    )
    once = normalize_list_columns(frame.copy())
    twice = normalize_list_columns(once.copy())
    assert once[_INDUSTRY_COLUMN].iloc[0] == twice[_INDUSTRY_COLUMN].iloc[0]
    assert once[_CONCEPT_COLUMN].iloc[0] == twice[_CONCEPT_COLUMN].iloc[0]


# ---------- 端到端：_to_frame 输出喂给下游拼接 ----------


def _rows_with_list_values():
    return {
        "code": ["920298", "688837", "300243"],
        "股票代码": ["920298.BJ", "688837.SH", "300243.SZ"],
        "股票简称": ["腾信精密", "信诺维", "瑞丰高材"],
        _INDUSTRY_COLUMN: [
            ["机械设备", "专用设备", "能源及重型设备"],
            ["医药生物", "生物制品", "其他生物制品"],
            ["基础化工", "塑料制品", "其他塑料制品"],
        ],
        _CONCEPT_COLUMN: [
            ["新股与次新股", "融资融券", "机械设备", "专用设备", "能源及重型设备"],
            ["创新药", "医药生物", "生物制品", "其他生物制品"],
            ["可降解塑料", "基础化工", "塑料制品", "其他塑料制品"],
        ],
        "code_count": 3,
        "row_count": 3,
    }


def test_to_frame_restores_contract_end_to_end():
    data = _rows_with_list_values()
    frame = _to_frame(
        {
            "datas": [
                {k: v[i] for k, v in data.items() if k in
                 ("code", "股票代码", "股票简称", _INDUSTRY_COLUMN, _CONCEPT_COLUMN)}
                for i in range(3)
            ],
            "code_count": 3,
            "row_count": 3,
        }
    )
    # 下游 scheduler 盘前/盘后的真实拼接方式
    industries, concepts = set(), set()
    for _, row in frame.iterrows():
        ind = str(row.get(_INDUSTRY_COLUMN, "")).strip()
        if ind and ind != "nan":
            industries.add(f"pw_industry_{ind}")
        con = str(row.get(_CONCEPT_COLUMN, ""))
        if con and con != "nan":
            concepts.update(c.strip() for c in con.split(";") if c.strip())

    # 3 行只应产出 3 个行业板块；形状不还原时会变成 3 个唯一 list-repr
    assert len(industries) == 3
    assert all("[" not in code and "'" not in code for code in industries)
    assert "pw_industry_机械设备-专用设备-能源及重型设备" in industries
    # 概念中不得混入行业三级名
    assert "机械设备" not in concepts and "专用设备" not in concepts
    assert not any("[" in c for c in concepts)
    assert {"新股与次新股", "融资融券", "创新药", "可降解塑料"} <= concepts


def test_to_frame_synthesizes_stock_code_and_keeps_attrs():
    frame = _to_frame(
        {"datas": [{"code": "600000", "股票简称": "浦发银行"}], "code_count": 1}
    )
    assert frame["股票代码"].iloc[0] == "600000"
    assert frame.attrs["code_count"] == 1


def test_to_frame_handles_empty_datas():
    frame = _to_frame({"datas": [], "code_count": 0})
    assert frame.empty
    assert frame.attrs["code_count"] == 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
