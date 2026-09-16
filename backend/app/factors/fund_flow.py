"""资金因子(5) — 主力/大单/散户/北向/板块资金"""

import numpy as np
import pandas as pd
from app.factors.base import FactorBase, FactorCategory, FactorResult, FactorRegistry, validate_factor_inputs


class MainInflowFactor(FactorBase):
    """主力净流入强度"""
    factor_name = "main_inflow_strength"
    category = FactorCategory.FUND_FLOW
    direction = 1
    description = "主力净流入/成交额，衡量大资金方向性"
    dependencies = ["main_net_inflow", "amount"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if "main_net_inflow" not in df.columns or "amount" not in df.columns:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)
        if len(df) == 0:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        inflow = df["main_net_inflow"].iloc[-1]
        amount = df["amount"].iloc[-1]
        strength = (inflow / amount * 100) if amount and amount > 0 else np.nan

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(strength),
            direction=self.direction,
        )


class BigOrderFactor(FactorBase):
    """大单净买入占比"""
    factor_name = "big_order_pct"
    category = FactorCategory.FUND_FLOW
    direction = 1
    description = "大单净买入/总成交额，主力资金确认"
    dependencies = ["big_net_inflow", "amount"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if "big_net_inflow" not in df.columns or "amount" not in df.columns:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)
        if len(df) == 0:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        big = df["big_net_inflow"].iloc[-1]
        amount = df["amount"].iloc[-1]
        pct = (big / amount * 100) if amount and amount > 0 else np.nan

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(pct),
            direction=self.direction,
        )


class FundFlowTrendFactor(FactorBase):
    """资金流向趋势(3日)"""
    factor_name = "fund_flow_trend_3d"
    input_window = 3
    category = FactorCategory.FUND_FLOW
    direction = 1
    description = "近3日主力净流入趋势，正值=持续流入"
    dependencies = ["main_net_inflow"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if "main_net_inflow" not in df.columns or len(df) < 3:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        recent = df["main_net_inflow"].iloc[-3:]
        trend = recent.sum()

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(trend),
            direction=self.direction,
            meta={"day1": float(recent.iloc[-1]), "day2": float(recent.iloc[-2]), "day3": float(recent.iloc[-3])},
        )


class SmallRetailFactor(FactorBase):
    """散户净流出比(反向指标)"""
    factor_name = "retail_outflow_pct"
    category = FactorCategory.FUND_FLOW
    direction = 1
    description = "散户净流出/总成交额，散户离场=主力吸筹"
    dependencies = ["small_net_inflow", "amount"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if "small_net_inflow" not in df.columns or "amount" not in df.columns:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)
        if len(df) == 0:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        small = df["small_net_inflow"].iloc[-1]
        amount = df["amount"].iloc[-1]
        # 散户净流出(负值)=好事, 取反
        retail_outflow = -small / amount * 100 if amount and amount > 0 else np.nan

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(retail_outflow),
            direction=self.direction,
        )


class SectorFundFactor(FactorBase):
    """所属板块资金流向"""
    factor_name = "sector_fund_flow"
    required_context = ["sector_fund_flow"]
    category = FactorCategory.FUND_FLOW
    direction = 1
    description = "所属板块整体资金净流入(亿)，板块共振"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        # 从kwargs中取板块资金数据
        sector_flow = kwargs.get("sector_fund_flow", np.nan)
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(sector_flow),
            direction=self.direction,
        )


# 注册
FactorRegistry.register(MainInflowFactor())
FactorRegistry.register(BigOrderFactor())
FactorRegistry.register(FundFlowTrendFactor())
FactorRegistry.register(SmallRetailFactor())
FactorRegistry.register(SectorFundFactor())
