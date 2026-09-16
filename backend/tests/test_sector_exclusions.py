"""锁定「无业务关联概念不展示在板块营地」的口径单一来源。

背景（2026-09-17 审计）
----------------------
该排除规则原先**只**写在 `scripts/collect_pywencai_sectors.py`（一次性采集
脚本）里。但概念板块改由调度器盘前路径写入（`scheduler._premarket` →
`_upsert_sector_info`），而调度器**不经过**那个脚本 —— 新发现的概念会以
`SectorInfo.is_excluded` 的列默认值 0 建库，直接漏进板块营地。
实测：迁移到 `WencaiStreamSource` 后待写入 827 个概念，其中
`2026中报预增` 属规则命中却不在库内。

这些用例锁定：
1. 规则语义（名单 + 年更模式）；
2. 规则**覆盖**历史上已排除的全部概念，防止上移过程把人工口径丢掉；
3. 调度器与脚本共用同一份规则，不允许各留一份导致漂移。
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from app.data.sector_exclusions import (
    EXCLUDED_CONCEPT_NAMES,
    EXCLUDED_CONCEPT_PATTERNS,
    is_excluded_concept,
)

# 库内 2026-09-17 实测 is_excluded=1 的全部 38 个概念（人工口径基线）。
# 规则上移后必须仍然覆盖它们，否则会让已排除的板块重新出现在营地。
DB_EXCLUDED_CONCEPTS_20260917 = (
    "国家大基金持股", "央企国企改革", "中国AI 50", "同花顺中特估100",
    "高股息精选", "证金持股", "PPP概念", "同花顺出海50",
    "股权转让(并购重组)", "融资融券", "深股通", "注册制次新股",
    "独角兽概念", "专精特新", "同花顺漂亮100", "沪股通",
    "ST板块", "新股与次新股", "科创次新股", "回购增持再贷款概念",
    "同花顺果指数", "同花顺新质50", "中船系", "中芯国际概念",
    "中字头股票", "超级品牌", "参股保险", "参股券商", "参股银行",
    "信托概念", "期货概念", "摘帽", "兵装重组概念", "国企改革",
    "上海国企改革", "深圳国企改革", "2025年报预增", "2025中报预增",
)


# ---------- 1. 规则语义 ----------


@pytest.mark.parametrize(
    "name", ["融资融券", "沪股通", "ST板块", "参股券商", "股权转让(并购重组)"]
)
def test_named_exclusions(name):
    assert is_excluded_concept(name) is True


@pytest.mark.parametrize(
    "name",
    ["2026中报预增", "2025年报预增", "2027一季报预减", "2026三季报预盈", "2026年报预亏"],
)
def test_earnings_forecast_patterns(name):
    """财报预告类名称随年份变化，必须靠模式而不是枚举。"""
    assert is_excluded_concept(name) is True


@pytest.mark.parametrize(
    "name", ["芯片概念", "人工智能", "固态电池", "低空经济", "人形机器人"]
)
def test_business_concepts_are_kept(name):
    assert is_excluded_concept(name) is False


@pytest.mark.parametrize("value", ["", "   ", None])
def test_blank_is_not_excluded(value):
    assert is_excluded_concept(value) is False


def test_whitespace_is_stripped():
    assert is_excluded_concept("  融资融券  ") is True


def test_rule_is_exposed_as_expected_types():
    assert isinstance(EXCLUDED_CONCEPT_NAMES, frozenset)
    assert isinstance(EXCLUDED_CONCEPT_PATTERNS, tuple)
    assert EXCLUDED_CONCEPT_PATTERNS


# ---------- 2. 覆盖基线：不得丢掉既有排除口径 ----------


def test_rule_covers_every_previously_excluded_concept():
    missing = [n for n in DB_EXCLUDED_CONCEPTS_20260917 if not is_excluded_concept(n)]
    assert missing == [], f"上移过程丢掉了已排除的概念: {missing}"


def test_rule_does_not_over_exclude_business_concepts():
    """规则只排「无业务关联」，不得扩大到正常产业概念。"""
    for name in DB_EXCLUDED_CONCEPTS_20260917:
        assert is_excluded_concept(name) is True


# ---------- 3. 单一来源：调度器与脚本共用 ----------


def _repo_backend() -> Path:
    return Path(__file__).resolve().parent.parent


def test_scheduler_uses_shared_rule():
    """调度器盘前概念写入必须打 is_excluded，否则新概念漏进营地。"""
    source = (_repo_backend() / "app/data/scheduler.py").read_text(encoding="utf-8")
    assert "from app.data.sector_exclusions import is_excluded_concept" in source
    # 概念板块记录构造处必须真的用上
    tree = ast.parse(source)
    used = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "is_excluded_concept"
    ]
    assert used, "scheduler 未使用共用排除规则"
    # 且必须出现在 "is_excluded" 键的赋值里
    assert '"is_excluded"' in source


def test_script_shares_rule_instead_of_duplicating():
    """脚本不得再自留一份名单 —— 两处口径会漂移。"""
    path = _repo_backend() / "scripts/collect_pywencai_sectors.py"
    source = path.read_text(encoding="utf-8")
    assert "from app.data.sector_exclusions import" in source
    # 旧的内联字面量名单已被移除
    assert '"融资融券", "沪股通", "深股通"' not in source


def test_script_module_imports_the_shared_rule():
    """脚本导入的必须是**同一个对象**，而不是等值副本。"""
    path = _repo_backend() / "scripts/collect_pywencai_sectors.py"
    spec = importlib.util.spec_from_file_location(
        "collect_pywencai_sectors_under_test", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.is_excluded_concept is is_excluded_concept


def test_script_uses_working_source_not_dead_library():
    """脚本不得再 import 已失效的 pywencai 库。"""
    path = _repo_backend() / "scripts/collect_pywencai_sectors.py"
    source = path.read_text(encoding="utf-8")
    assert "import pywencai" not in source
    assert "WencaiStreamSource" in source
