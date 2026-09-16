"""融资融券因子(2) — 融资买入强度/融资余额变化趋势"""

import numpy as np
import pandas as pd
from app.factors.base import FactorBase, FactorCategory, FactorResult, FactorRegistry, validate_factor_inputs


class MarginBuyStrengthFactor(FactorBase):
    """融资买入强度"""
    factor_name = "margin_buy_strength"
    context_bounds = {"margin_buy":[0,None],"amount":[0,None]}
    required_context = ["margin_buy","amount"]
    category = FactorCategory.MARGIN
    direction = 1
    description = "融资买入额/成交额，高=杠杆资金积极"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        margin_buy = kwargs.get("margin_buy", 0)
        amount = kwargs.get("amount", 0)

        if amount <= 0:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        strength = (margin_buy / amount) * 100

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(strength),
            direction=self.direction,
        )


class MarginTrendFactor(FactorBase):
    """融资余额变化趋势"""
    factor_name = "margin_balance_trend"
    conditional_context = ("margin_balance_change_avg5",)
    category = FactorCategory.MARGIN
    direction = 1
    description = "近5日融资余额环比变化均值，正=持续加杠杆"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        # 优先从DataFrame取历史数据
        if "margin_change" in df.columns and len(df) >= 5:
            recent = [self._safe_value(value) for value in df["margin_change"].iloc[-5:]]
            if not np.isfinite(recent).all():
                return self._unavailable("invalid_window", "margin_change")
            avg_change = np.mean(recent)
            return FactorResult(
                factor_name=self.factor_name,
                value=self._safe_value(avg_change),
                direction=self.direction,
            )

        # 降级: 从kwargs取
        trend = self._safe_value(kwargs.get("margin_balance_change_avg5"), default=None)
        if trend is None:
            return self._unavailable("missing_context", "margin_balance_change_avg5")
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(trend),
            direction=self.direction,
        )


# 注册
FactorRegistry.register(MarginBuyStrengthFactor())
FactorRegistry.register(MarginTrendFactor())
