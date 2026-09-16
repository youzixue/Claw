"""板块因子(5) — 板块强度/持续性/龙头辨识/资金共振/轮动"""

import numpy as np
import pandas as pd
from app.factors.base import FactorBase, FactorCategory, FactorResult, FactorRegistry, validate_factor_inputs


class SectorStrengthFactor(FactorBase):
    """板块强度评分"""
    factor_name = "sector_strength"
    required_context = ["sector_strength_score"]
    category = FactorCategory.SECTOR
    direction = 1
    description = "所属板块当日强弱评分(0-100)"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        score = kwargs.get("sector_strength_score", np.nan)
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(score),
            direction=self.direction,
        )


class SectorPersistenceFactor(FactorBase):
    """板块持续性"""
    factor_name = "sector_persistence"
    context_bounds = {"sector_persistence_days":[0,None]}
    required_context = ["sector_persistence_days"]
    category = FactorCategory.SECTOR
    direction = 1
    description = "板块连续活跃天数，>3天=持续性强"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        days = kwargs.get("sector_persistence_days", 0)
        return FactorResult(
            factor_name=self.factor_name,
            value=float(days),
            direction=self.direction,
        )


class SectorLeaderFactor(FactorBase):
    """板块龙头辨识"""
    factor_name = "sector_leader"
    required_context = ["sector_leader_score"]
    category = FactorCategory.SECTOR
    direction = 1
    description = "是否为板块龙头(涨幅排名/连板高度/封单量综合)，1=龙头, 0.5=龙二, 0=跟风"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        leader_score = kwargs.get("sector_leader_score", 0.0)
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(leader_score),
            direction=self.direction,
        )


class SectorResonanceFactor(FactorBase):
    """板块资金共振度"""
    factor_name = "sector_resonance"
    required_context = ["stock_main_net_inflow","sector_main_net_inflow"]
    category = FactorCategory.SECTOR
    direction = 1
    description = "个股资金方向与板块资金方向一致性，1=同向共振"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        stock_flow = kwargs.get("stock_main_net_inflow", 0)
        sector_flow = kwargs.get("sector_main_net_inflow", 0)

        # 同向=共振
        if stock_flow > 0 and sector_flow > 0:
            resonance = 1.0
        elif stock_flow < 0 and sector_flow < 0:
            resonance = -1.0  # 同向流出
        else:
            resonance = 0.0  # 分歧

        return FactorResult(
            factor_name=self.factor_name,
            value=resonance,
            direction=self.direction,
        )


class SectorRotationFactor(FactorBase):
    """板块轮动信号"""
    factor_name = "sector_rotation"
    required_context = ["sector_rotation_signal"]
    category = FactorCategory.SECTOR
    direction = 1
    description = "板块是否处于资金流入切换阶段，1=资金流入, -1=流出"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        rotation_signal = kwargs.get("sector_rotation_signal", 0.0)
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(rotation_signal),
            direction=self.direction,
        )


# 注册
FactorRegistry.register(SectorStrengthFactor())
FactorRegistry.register(SectorPersistenceFactor())
FactorRegistry.register(SectorLeaderFactor())
FactorRegistry.register(SectorResonanceFactor())
FactorRegistry.register(SectorRotationFactor())
