"""因子基类 — 所有因子的抽象基类与注册机制"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from functools import wraps
import math
from numbers import Integral, Real
from typing import Any, Optional

import numpy as np
import pandas as pd
from loguru import logger


# ========== 因子分类 ==========

class FactorCategory(str, Enum):
    """10类因子分类"""
    TECHNICAL = "technical"         # 技术因子(8)
    FUND_FLOW = "fund_flow"         # 资金因子(5)
    SENTIMENT = "sentiment"         # 情绪因子(5)
    SECTOR = "sector"               # 板块因子(5)
    FUNDAMENTAL = "fundamental"     # 基本面因子(5)
    BREAKOUT = "breakout"           # 爆发因子(5)
    PROMOTION = "promotion"         # 晋级因子(5)
    LIFECYCLE = "lifecycle"         # 生命周期因子(5)
    NEWS = "news"                   # 新闻因子(3)
    MARGIN = "margin"               # 融资融券因子(2)


FACTOR_INPUT_CONTRACT = "factor_input_v1"


def _finite_number(value: Any) -> Optional[float]:
    """只接受数值标量；bool、容器及非有限数不属于因子观测值。"""
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finite_meta(value: Any) -> Any:
    """因子自行构造的元数据转为严格JSON叶子；不处理外部服务对象。"""
    if isinstance(value, dict):
        return {key: _finite_meta(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_meta(item) for item in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, Real):
        return _finite_number(value)
    return value


@dataclass
class FactorResult:
    """因子计算结果"""
    factor_name: str
    value: Optional[float] = None
    rank: Optional[int] = None          # 截面排名
    pct: Optional[float] = None         # 截面百分位
    direction: int = 1                  # 1=越大越好, -1=越小越好
    confidence: float = 1.0             # 置信度 0-1
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        self.value = _finite_number(self.value)
        confidence = _finite_number(self.confidence)
        self.confidence = confidence if confidence is not None and 0 <= confidence <= 1 else 0.0
        self.meta = _finite_meta(self.meta)
        if self.value is None:
            self.confidence = 0.0
        if self.confidence == 0.0:
            self.rank = self.pct = None
        self.meta.setdefault("input_contract", FACTOR_INPUT_CONTRACT)
        self.meta.setdefault("data_status", "available" if self.value is not None else "unavailable")


def validate_factor_inputs():
    """显式用于calculate：不补默认值，不让pandas跳过缺失后伪造完整窗口。

    window=None校验递归EMA实际依赖的全部输入；有限窗口只校验实际使用部分。
    此契约仅证明输入完整/有限，不证明数据在历史决策时点已经可用。
    """
    def decorate(calculate):
        @wraps(calculate)
        def checked(self, df, **kwargs):
            input_window = self.input_window
            required_context = self.required_context
            context_enums = self.context_enums
            context_bounds = self.context_bounds
            issues = []
            for key in required_context:
                raw = kwargs.get(key)
                if raw is None:
                    issues.append({"field": key, "code": "missing_context"})
                    continue
                if key in context_enums:
                    valid = isinstance(raw, str) and raw in context_enums[key]
                else:
                    value = _finite_number(raw)
                    valid = value is not None
                    if valid and key in context_bounds:
                        lower, upper = context_bounds[key]
                        valid = (lower is None or value >= lower) and (upper is None or value <= upper)
                if not valid:
                    issues.append({"field": key, "code": "invalid_context"})
            if self.dependencies:
                for key in self.dependencies:
                    if key not in df.columns or df.empty:
                        issues.append({"field": key, "code": "missing_column"})
                        continue
                    series = df[key] if input_window is None else df[key].iloc[-input_window:]
                    values = [_finite_number(value) for value in series]
                    if any(value is None for value in values):
                        issues.append({"field": key, "code": "invalid_window"})
                    elif key in ("close", "high", "low", "open") and any(value <= 0 for value in values):
                        issues.append({"field": key, "code": "nonpositive_price"})
                    elif key in ("volume", "amount", "turnover", "amplitude") and any(value < 0 for value in values):
                        issues.append({"field": key, "code": "negative_market_value"})
            if issues:
                return FactorResult(
                    factor_name=self.factor_name, direction=self.direction, confidence=0.0,
                    meta={"data_status": "unavailable", "input_issues": issues},
                )
            result = calculate(self, df, **kwargs)
            result.direction = self.direction
            return result

        checked.input_contract = FACTOR_INPUT_CONTRACT
        return checked
    return decorate


class FactorBase(ABC):
    """因子基类 — 所有因子必须继承"""

    # 子类必须覆盖
    factor_name: str = ""
    category: FactorCategory = FactorCategory.TECHNICAL
    direction: int = 1                  # 1=越大越好, -1=越小越好
    description: str = ""
    dependencies: list[str] = []        # 依赖的数据字段
    input_window: Optional[int] = 1     # None=递归指标所用完整历史
    required_context: tuple[str, ...] = ()
    # Inputs validated conditionally inside calculate (e.g. importance only for
    # a nonempty news window). Declaration does NOT make them unconditional.
    conditional_context: tuple[str, ...] = ()
    context_enums: dict = {}
    context_bounds: dict = {}

    @abstractmethod
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        """计算单个因子值

        Args:
            df: 包含行情/资金等数据的DataFrame
            **kwargs: 额外参数(如板块数据、市场数据等)

        Returns:
            FactorResult
        """
        ...

    def _unavailable(self, code: str, *fields: str) -> FactorResult:
        return FactorResult(
            factor_name=self.factor_name, direction=self.direction, confidence=0.0,
            meta={"input_issues": [{"field": name, "code": code} for name in fields]},
        )

    def _safe_value(self, value: Any, default: float = np.nan) -> float:
        """安全取值；非数值标量和非有限数不得当成中性分。"""
        number = _finite_number(value)
        return default if number is None else number


# ========== 因子注册表 ==========

class FactorRegistry:
    """因子注册表 — 管理所有因子的注册、查询、批量计算"""

    _factors: dict[str, FactorBase] = {}

    @classmethod
    def register(cls, factor: FactorBase):
        """注册因子"""
        if factor.factor_name in cls._factors:
            logger.warning(f"因子重复注册: {factor.factor_name}")
        cls._factors[factor.factor_name] = factor
        logger.debug(f"因子注册: {factor.factor_name} ({factor.category.value})")

    @classmethod
    def get(cls, name: str) -> Optional[FactorBase]:
        """获取因子"""
        return cls._factors.get(name)

    @classmethod
    def get_by_category(cls, category: FactorCategory) -> list[FactorBase]:
        """按分类获取因子"""
        return [f for f in cls._factors.values() if f.category == category]

    @classmethod
    def all_factors(cls) -> dict[str, FactorBase]:
        """获取所有因子"""
        return dict(cls._factors)

    @classmethod
    def count(cls) -> int:
        """已注册因子数"""
        return len(cls._factors)

    @classmethod
    def summary(cls) -> dict[str, int]:
        """按分类统计"""
        result = {}
        for cat in FactorCategory:
            result[cat.value] = len(cls.get_by_category(cat))
        return result


# ========== 因子计算引擎 ==========

class FactorEngine:
    """因子计算引擎 — 批量计算因子并存储"""

    def __init__(self):
        self.registry = FactorRegistry()

    async def compute_single(self, code: str, trade_date: date,
                              df: pd.DataFrame, **kwargs) -> dict[str, FactorResult]:
        """计算单只股票所有因子"""
        results = {}
        for name, factor in self.registry.all_factors().items():
            try:
                result = factor.calculate(df, **kwargs)
                results[name] = result
            except Exception as e:
                logger.warning(f"因子计算失败 [{name}] {code}: {e}")
                results[name] = FactorResult(
                    factor_name=name,
                    value=None,
                    confidence=0.0,
                    direction=factor.direction,
                    meta={"error": str(e), "error_type": type(e).__name__},
                )
        return results

    async def compute_cross_section(self, trade_date: date,
                                     stock_data: dict[str, pd.DataFrame], *,
                                     contexts_by_code: Optional[dict] = None,
                                     **kwargs) -> dict[str, dict[str, FactorResult]]:
        """Rank a supplied research cross-section using explicit per-stock arguments.

        No implicit shared/market context and no "latest" lookup. Caller-supplied
        values are NOT evidence of entity attribution or historical availability.
        Frames remain caller-owned; only membership and scalar contexts are frozen.
        """
        if kwargs:
            raise ValueError("cross-section context broadcast is forbidden; use contexts_by_code")
        if type(trade_date) is not date:
            raise ValueError("cross-section requires an explicit trade date")
        if type(stock_data) is not dict:
            raise ValueError("cross-section requires a stock frame mapping")
        items = tuple(stock_data.items())
        for code, df in items:
            if (type(code) is not str or len(code) != 6 or not code.isascii()
                    or not code.isdecimal()):
                raise ValueError("cross-section requires ASCII six-digit stock codes")
            if not isinstance(df, pd.DataFrame):
                raise ValueError("cross-section stock input must be a DataFrame")
        contexts = {} if contexts_by_code is None else contexts_by_code
        if type(contexts) is not dict or set(contexts) - set(stock_data):
            raise ValueError("cross-section contexts must match supplied stock codes")
        factors = self.registry.all_factors()
        declared = {
            name: set(f.required_context) | set(f.conditional_context)
            for name, f in factors.items()
        }
        known_fields = set().union(*declared.values())
        owned = {}
        # Snapshot ALL stocks before the first await; never enumerate or copy live
        # service/ORM objects. Only explicit dicts of scalar factor arguments.
        for code, values in contexts.items():
            if type(values) is not dict or set(values) - known_fields:
                raise ValueError("unknown or malformed per-stock context fields")
            owned[code] = {}
            for key, value in values.items():
                if value is None or type(value) is str:
                    scalar = value
                elif isinstance(value, (bool, np.bool_)):
                    scalar = bool(value)  # Preserved as invalid, never coerced to 0/1.
                elif isinstance(value, Real):
                    number = _finite_number(value)
                    scalar = int(value) if number is not None and isinstance(value, Integral) else number
                else:
                    raise ValueError("per-stock context requires scalar arguments")
                owned[code][key] = scalar

        all_results = {}
        for code, df in items:
            context = owned.get(code, {})
            results = await self.compute_single(code, trade_date, df, **context)
            for name, result in results.items():
                factor = factors[name]
                result.meta["cross_section_context"] = {
                    "protocol": "factor_cross_section_context_v1",
                    "code": code, "trade_date": trade_date.isoformat(),
                    "source_basis": "caller_supplied_unverified",
                    "argument_scope": "declared_arguments_not_dynamic_usage",
                    "values": {key: context[key] for key in sorted(declared[name]) if key in context},
                    "missing_required_fields": sorted(
                        key for key in factor.required_context if context.get(key) is None),
                    "point_in_time_verified": False, "trading_authority": False,
                    "promotion_eligible": False, "automatic_weight_update": False,
                }
            all_results[code] = results

        # 截面排名
        for factor_name in self.registry.all_factors():
            factor = self.registry.get(factor_name)
            if not factor:
                continue

            # 收集该因子的所有值
            values = {}
            for code, results in all_results.items():
                result = results.get(factor_name)
                if result is not None:
                    result.rank = result.pct = None
                    value = _finite_number(result.value)
                    if value is not None and result.confidence > 0:
                        values[code] = value

            if not values:
                continue

            # 排名
            sorted_codes = sorted(
                values.items(),
                key=lambda x: x[1],
                reverse=(factor.direction == 1),
            )

            total = len(sorted_codes)
            for rank, (code, val) in enumerate(sorted_codes, 1):
                if code in all_results and factor_name in all_results[code]:
                    all_results[code][factor_name].rank = rank
                    all_results[code][factor_name].pct = round(1 - (rank - 1) / max(total - 1, 1), 4)

        return all_results

    def get_factor_summary(self) -> dict:
        """获取因子概览"""
        return {
            "total": self.registry.count(),
            "by_category": self.registry.summary(),
            "factors": {
                name: {
                    "category": f.category.value,
                    "direction": f.direction,
                    "description": f.description,
                    "input_contract": {
                        "version": getattr(f.calculate, "input_contract", None),
                        "window": f.input_window,
                        "required_context": list(f.required_context),
                        "conditional_context": list(f.conditional_context),
                    },
                }
                for name, f in self.registry.all_factors().items()
            },
        }


# 全局引擎
factor_engine = FactorEngine()
