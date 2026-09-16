"""情绪因子(5) — 市场情绪周期/涨跌/封板率/连板高/涨跌比"""

import numpy as np
import pandas as pd
from app.factors.base import FactorBase, FactorCategory, FactorResult, FactorRegistry, validate_factor_inputs


class SentimentCycleFactor(FactorBase):
    """市场情绪周期位置"""
    factor_name = "sentiment_cycle"
    context_enums = {"sentiment_cycle":["freezing","recovery","divergence","climax"]}
    required_context = ["sentiment_cycle"]
    category = FactorCategory.SENTIMENT
    direction = 1
    description = "市场情绪周期(1=冰点,2=修复,3=分歧,4=亢奋)，修复→分歧阶段最适合短线"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        cycle = kwargs.get("sentiment_cycle", "recovery")
        cycle_score = {
            "freezing": 1,
            "recovery": 2,
            "divergence": 3,
            "climax": 4,
        }
        score = cycle_score.get(cycle, 2)
        return FactorResult(
            factor_name=self.factor_name,
            value=float(score),
            direction=1,  # 分歧→修复=最佳
            meta={"cycle": cycle},
        )


class LimitUpCountFactor(FactorBase):
    """涨停家数"""
    factor_name = "limit_up_count"
    context_bounds = {"limit_up_count":[0,None]}
    required_context = ["limit_up_count"]
    category = FactorCategory.SENTIMENT
    direction = 1
    description = "市场涨停家数，>80=亢奋，<20=冰点"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        count = kwargs.get("limit_up_count", 0)
        return FactorResult(
            factor_name=self.factor_name,
            value=float(count),
            direction=self.direction,
        )


class SealRateFactor(FactorBase):
    """封板率"""
    factor_name = "seal_rate"
    context_bounds = {"seal_rate":[0,100]}
    required_context = ["seal_rate"]
    category = FactorCategory.SENTIMENT
    direction = 1
    description = "封板率=涨停数/(涨停+炸板)，>70%=情绪强"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        seal_rate = kwargs.get("seal_rate", np.nan)
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(seal_rate),
            direction=self.direction,
        )


class BoardHeightFactor(FactorBase):
    """最高连板数"""
    factor_name = "board_height"
    context_bounds = {"board_height":[0,None]}
    required_context = ["board_height"]
    category = FactorCategory.SENTIMENT
    direction = 1
    description = "市场最高连板天数，>5板=强赚钱效应"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        height = kwargs.get("board_height", 0)
        return FactorResult(
            factor_name=self.factor_name,
            value=float(height),
            direction=self.direction,
        )


class AdvanceDeclineFactor(FactorBase):
    """涨跌比"""
    factor_name = "advance_decline_ratio"
    context_bounds = {"advance_decline_ratio":[0,None]}
    required_context = ["advance_decline_ratio"]
    category = FactorCategory.SENTIMENT
    direction = 1
    description = "上涨家数/下跌家数，>2=普涨，<0.5=普跌"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        ratio = kwargs.get("advance_decline_ratio", 1.0)
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(ratio),
            direction=self.direction,
        )


# 注册
FactorRegistry.register(SentimentCycleFactor())
FactorRegistry.register(LimitUpCountFactor())
FactorRegistry.register(SealRateFactor())
FactorRegistry.register(BoardHeightFactor())
FactorRegistry.register(AdvanceDeclineFactor())
