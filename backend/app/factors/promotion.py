"""晋级因子(5) — 连板晋级概率/板块支撑/历史晋级率/竞价强度/筹码结构"""

import numpy as np
import pandas as pd
from app.factors.base import FactorBase, FactorCategory, FactorResult, FactorRegistry, validate_factor_inputs


class PromotionRateFactor(FactorBase):
    """连板晋级概率"""
    factor_name = "promotion_rate"
    context_enums = {"sentiment_cycle":["freezing","recovery","divergence","climax"]}
    context_bounds = {"consecutive_days":[1,None]}
    required_context = ["consecutive_days","sentiment_cycle"]
    category = FactorCategory.PROMOTION
    direction = 1
    description = "连板数+情绪的经验启发式分，非经校准晋级概率，不可作为生产概率门禁"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        consecutive_days = kwargs.get("consecutive_days", 1)
        sentiment_cycle = kwargs.get("sentiment_cycle", "recovery")

        if not float(consecutive_days).is_integer():
            return self._unavailable("invalid_board_count", "consecutive_days")

        # 基础晋级率(经验值)
        base_rates = {1: 0.40, 2: 0.25, 3: 0.18, 4: 0.12, 5: 0.08, 6: 0.05}
        base = base_rates.get(consecutive_days, 0.03)

        # 情绪修正
        sentiment_mod = {
            "freezing": -0.15,
            "recovery": 0.0,
            "divergence": 0.05,
            "climax": 0.10,
        }
        rate = base + sentiment_mod.get(sentiment_cycle, 0)
        rate = max(0, min(1, rate))

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(rate * 100),  # 转百分比
            direction=self.direction,
            meta={"base_rate": base, "consecutive_days": consecutive_days,
                  "semantics": "heuristic_score", "calibrated_probability": False},
        )


class SectorSupportFactor(FactorBase):
    """板块支撑度"""
    factor_name = "sector_support"
    context_bounds = {"sector_limit_up_count":[0,None],"sector_stock_count":[1,None]}
    required_context = ["sector_limit_up_count","sector_stock_count"]
    category = FactorCategory.PROMOTION
    direction = 1
    description = "所属板块涨停家数/板块个股总数，>10%=板块强支撑"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        sector_limit_up = kwargs.get("sector_limit_up_count", 0)
        sector_total = kwargs.get("sector_stock_count", 1)

        if sector_limit_up > sector_total:
            return self._unavailable("inconsistent_sector_counts", "sector_limit_up_count", "sector_stock_count")
        support = sector_limit_up / sector_total * 100

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(support),
            direction=self.direction,
        )


class HistoricalPromotionFactor(FactorBase):
    """历史晋级率"""
    factor_name = "historical_promotion"
    required_context = ["historical_promotion_rate"]
    category = FactorCategory.PROMOTION
    direction = 1
    description = "该股历史连板后继续晋级成功率"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        hist_rate = kwargs.get("historical_promotion_rate", np.nan)
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(hist_rate),
            direction=self.direction,
        )


class AuctionStrengthFactor(FactorBase):
    """竞价强度"""
    factor_name = "auction_strength"
    context_bounds = {"auction_volume_ratio":[0,None]}
    required_context = ["auction_volume_ratio","auction_open_change"]
    category = FactorCategory.PROMOTION
    direction = 1
    description = "集合竞价量比+高开幅度综合评分"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        auction_volume_ratio = kwargs.get("auction_volume_ratio", 1.0)
        auction_open_change = kwargs.get("auction_open_change", 0.0)

        # 量比评分(0-50)
        vol_score = min(auction_volume_ratio / 5, 1) * 50

        # 高开评分(0-50): +5%以上=满分
        open_score = min(max(auction_open_change, 0) / 5, 1) * 50

        strength = vol_score + open_score

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(strength),
            direction=self.direction,
            meta={"vol_ratio": auction_volume_ratio, "open_change": auction_open_change},
        )


class ChipStructureFactor(FactorBase):
    """筹码结构"""
    factor_name = "chip_structure"
    input_window = 30
    category = FactorCategory.PROMOTION
    direction = 1
    description = "获利盘占比(基于筹码分布估算)，70%+获利盘=抛压小"
    dependencies = ["close"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if "close" not in df.columns or len(df) < 30:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        close = df["close"]
        current = close.iloc[-1]

        # 简化: 近30日内低于当前价的天数占比
        recent = close.iloc[-30:]
        profit_pct = (recent < current).sum() / len(recent) * 100

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(profit_pct),
            direction=self.direction,
        )


# 注册
FactorRegistry.register(PromotionRateFactor())
FactorRegistry.register(SectorSupportFactor())
FactorRegistry.register(HistoricalPromotionFactor())
FactorRegistry.register(AuctionStrengthFactor())
FactorRegistry.register(ChipStructureFactor())
