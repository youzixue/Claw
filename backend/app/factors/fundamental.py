"""基本面因子(5) — PE/PB/ROE/营收增长/股息率"""

import numpy as np
import pandas as pd
from app.factors.base import FactorBase, FactorCategory, FactorResult, FactorRegistry, validate_factor_inputs


class PERatioFactor(FactorBase):
    """市盈率(PE-TTM)百分位"""
    factor_name = "pe_pct"
    required_context = ["pe_percentile"]
    category = FactorCategory.FUNDAMENTAL
    direction = -1  # 低PE=好
    description = "PE-TTM在行业中的百分位，低位=估值低"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        pe_pct = kwargs.get("pe_percentile", np.nan)
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(pe_pct),
            direction=self.direction,
        )


class PBRatioFactor(FactorBase):
    """市净率(PB)百分位"""
    factor_name = "pb_pct"
    required_context = ["pb_percentile"]
    category = FactorCategory.FUNDAMENTAL
    direction = -1
    description = "PB在行业中的百分位，破净附近=安全边际"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        pb_pct = kwargs.get("pb_percentile", np.nan)
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(pb_pct),
            direction=self.direction,
        )


class ROEFactor(FactorBase):
    """净资产收益率(ROE)"""
    factor_name = "roe"
    required_context = ["roe"]
    category = FactorCategory.FUNDAMENTAL
    direction = 1
    description = "最近一期ROE，>15%=优质"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        roe = kwargs.get("roe", np.nan)
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(roe),
            direction=self.direction,
        )


class RevenueGrowthFactor(FactorBase):
    """营收同比增长"""
    factor_name = "revenue_growth"
    required_context = ["revenue_growth"]
    category = FactorCategory.FUNDAMENTAL
    direction = 1
    description = "营收同比增长率%，>30%=高增长"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        growth = kwargs.get("revenue_growth", np.nan)
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(growth),
            direction=self.direction,
        )


class DividendYieldFactor(FactorBase):
    """股息率"""
    factor_name = "dividend_yield"
    required_context = ["dividend_yield"]
    category = FactorCategory.FUNDAMENTAL
    direction = 1
    description = "股息率%，>3%=高股息"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        dividend = kwargs.get("dividend_yield", np.nan)
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(dividend),
            direction=self.direction,
        )


# 注册
FactorRegistry.register(PERatioFactor())
FactorRegistry.register(PBRatioFactor())
FactorRegistry.register(ROEFactor())
FactorRegistry.register(RevenueGrowthFactor())
FactorRegistry.register(DividendYieldFactor())
