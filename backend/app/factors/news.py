"""新闻因子(3) — 新闻热度/利好共振度/政策敏感度"""

import numpy as np
import pandas as pd
from app.factors.base import FactorBase, FactorCategory, FactorResult, FactorRegistry, validate_factor_inputs


class NewsHeatFactor(FactorBase):
    """新闻热度"""
    factor_name = "news_heat"
    context_bounds = {"news_count_1h":[0,None],"news_count_24h":[0,None]}
    required_context = ["news_count_1h","news_count_24h"]
    conditional_context = ("news_avg_importance",)
    category = FactorCategory.NEWS
    direction = 1
    description = "近1h/24h相关新闻条数×重要性加权，高=消息面催化强"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        news_count_1h = kwargs.get("news_count_1h", 0)
        news_count_24h = kwargs.get("news_count_24h", 0)
        if news_count_1h > news_count_24h:
            return self._unavailable("inconsistent_news_window", "news_count_1h", "news_count_24h")
        if news_count_24h == 0:
            # 只有明确提供且一致的空窗口计数才代表零热度；不凭缺省值推断无新闻。
            return FactorResult(factor_name=self.factor_name, value=0.0, direction=self.direction,
                                meta={"count_1h": 0, "count_24h": 0, "empty_window": True})
        avg_importance = self._safe_value(kwargs.get("news_avg_importance"), default=None)
        if avg_importance is None or not 1 <= avg_importance <= 10:
            return self._unavailable("missing_or_invalid_importance", "news_avg_importance")

        # 加权: 1h新闻权重5 + 24h权重2 + 重要性权重3
        heat = news_count_1h * 5 + news_count_24h * 2 + avg_importance * 3
        # 归一化到0-100
        heat = min(heat / 5, 100)

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(heat),
            direction=self.direction,
            meta={"count_1h": news_count_1h, "count_24h": news_count_24h},
        )


class BullResonanceFactor(FactorBase):
    """利好共振度"""
    factor_name = "bull_resonance"
    context_bounds = {"stock_bull_ratio":[0,1],"sector_bull_ratio":[0,1]}
    required_context = ["stock_bull_ratio","sector_bull_ratio"]
    category = FactorCategory.NEWS
    direction = 1
    description = "个股利好新闻占比×板块利好占比，高=利好确定性"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        stock_bull_ratio = kwargs.get("stock_bull_ratio", 0.5)   # 个股利好/总新闻
        sector_bull_ratio = kwargs.get("sector_bull_ratio", 0.5)  # 板块利好/总新闻

        resonance = stock_bull_ratio * sector_bull_ratio * 100

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(resonance),
            direction=self.direction,
        )


class PolicySensitivityFactor(FactorBase):
    """政策敏感度"""
    factor_name = "policy_sensitivity"
    context_bounds = {"policy_frequency_30d":[0,None],"policy_impact_coefficient":[0,None]}
    required_context = ["policy_frequency_30d","policy_impact_coefficient"]
    category = FactorCategory.NEWS
    direction = 1
    description = "所属板块政策利好频率×影响系数，高=政策驱动"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        policy_frequency = kwargs.get("policy_frequency_30d", 0)  # 近30天政策利好次数
        impact_coefficient = kwargs.get("policy_impact_coefficient", 1.0)  # 板块政策敏感度

        sensitivity = policy_frequency * impact_coefficient * 5
        sensitivity = min(sensitivity, 100)

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(sensitivity),
            direction=self.direction,
        )


# 注册
FactorRegistry.register(NewsHeatFactor())
FactorRegistry.register(BullResonanceFactor())
FactorRegistry.register(PolicySensitivityFactor())
