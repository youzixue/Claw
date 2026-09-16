"""生命周期因子(5) — 牛股生命周期阶段/阶段得分/加速信号/分歧度/见顶预警"""

import numpy as np
import pandas as pd
from app.factors.base import FactorBase, FactorCategory, FactorResult, FactorRegistry, validate_factor_inputs


class LifecycleStageFactor(FactorBase):
    """生命周期阶段"""
    factor_name = "lifecycle_stage"
    context_enums = {"lifecycle_stage":["accumulation","launch","acceleration","divergence","topping"]}
    required_context = ["lifecycle_stage"]
    category = FactorCategory.LIFECYCLE
    direction = 1
    description = "牛股生命周期(1=蓄势,2=启动,3=加速,4=分歧,5=见顶)，2-3=最佳参与"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        stage = kwargs.get("lifecycle_stage", "launch")
        stage_score = {
            "accumulation": 1,  # 蓄势
            "launch": 2,        # 启动
            "acceleration": 3,  # 加速
            "divergence": 4,    # 分歧
            "topping": 5,       # 见顶
        }
        score = stage_score.get(stage, 2)

        # 反转: 启动+加速=好, 蓄势=中, 分歧/见顶=差
        value_map = {1: 60, 2: 90, 3: 95, 4: 40, 5: 10}

        return FactorResult(
            factor_name=self.factor_name,
            value=float(value_map.get(score, 60)),
            direction=self.direction,
            meta={"stage": stage, "stage_id": score},
        )


class AccelerationSignalFactor(FactorBase):
    """加速信号"""
    factor_name = "acceleration_signal"
    input_window = 5
    category = FactorCategory.LIFECYCLE
    direction = 1
    description = "量价齐升+涨幅递增+缩量封板=加速信号强度"
    dependencies = ["close", "volume"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if len(df) < 5 or not all(c in df.columns for c in ["close", "volume"]):
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        close = df["close"]
        volume = df["volume"]

        # 涨幅递增(近3日)
        changes = close.pct_change().iloc[-3:] * 100
        increasing_change = all(changes.iloc[i] < changes.iloc[i + 1] for i in range(len(changes) - 1)) if len(changes) >= 2 else False

        # 量能变化
        if volume.iloc[-2] <= 0:
            return self._unavailable("zero_volume_denominator", "volume")
        vol_change = volume.pct_change().iloc[-1]

        # 综合评分
        score = 0
        if increasing_change:
            score += 40
        if changes.iloc[-1] > 5:  # 今日涨幅>5%
            score += 30
        if vol_change > 0.5:  # 放量
            score += 15
        if changes.iloc[-1] > 9.5:  # 涨停
            score += 15

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(score),
            direction=self.direction,
        )


class DivergenceFactor(FactorBase):
    """分歧度"""
    factor_name = "divergence_degree"
    context_bounds = {"break_count":[0,None]}
    required_context = ["break_count"]
    category = FactorCategory.LIFECYCLE
    direction = -1  # 分歧大=不好
    description = "盘中分歧程度(振幅+炸板+换手)，高=多空分歧大"
    dependencies = ["amplitude", "turnover"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        break_count = kwargs.get("break_count", 0)

        if "amplitude" not in df.columns or "turnover" not in df.columns or len(df) == 0:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        amplitude = df["amplitude"].iloc[-1] or 0
        turnover = df["turnover"].iloc[-1] or 0

        # 振幅>10% + 高换手 + 炸板 = 高分歧
        score = amplitude * 3 + turnover * 0.5 + break_count * 20

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(min(score, 100)),
            direction=self.direction,
        )


class ToppingWarningFactor(FactorBase):
    """见顶预警"""
    factor_name = "topping_warning"
    input_window = 20
    category = FactorCategory.LIFECYCLE
    direction = -1  # 越高越危险
    description = "见顶信号综合评分(天量+长上影+板块退潮)，>60=危险"
    dependencies = ["close", "high", "low", "volume"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if len(df) < 20 or not all(c in df.columns for c in ["close", "high", "low", "volume"]):
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        score = 0

        # 天量: 今日量=20日最大
        if volume.iloc[-1] >= volume.iloc[-20:].max():
            score += 30

        # 长上影线: (最高-收盘)/(最高-最低) > 0.6
        body_range = high.iloc[-1] - low.iloc[-1]
        if body_range > 0:
            upper_shadow = (high.iloc[-1] - close.iloc[-1]) / body_range
            if upper_shadow > 0.6:
                score += 25

        # 放量滞涨: 量大但涨幅<2%
        change = (close.iloc[-1] / close.iloc[-2] - 1) * 100 if len(close) >= 2 else 0
        avg_vol = volume.iloc[-20:-1].mean()
        if volume.iloc[-1] > avg_vol * 2 and change < 2:
            score += 25

        # 高位阴线
        if change < -3 and close.iloc[-1] > close.iloc[-20:].mean():
            score += 20

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(min(score, 100)),
            direction=self.direction,
        )


class LifecycleScoreFactor(FactorBase):
    """生命周期综合评分"""
    factor_name = "lifecycle_score"
    required_context = ["lifecycle_stage_value","acceleration_signal_value","divergence_degree_value","topping_warning_value"]
    category = FactorCategory.LIFECYCLE
    direction = 1
    description = "基于阶段+加速+分歧+见顶的综合评分(0-100)"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        stage_val = kwargs.get("lifecycle_stage_value", 60)
        accel_val = kwargs.get("acceleration_signal_value", 0)
        diverge_val = kwargs.get("divergence_degree_value", 0)
        topping_val = kwargs.get("topping_warning_value", 0)

        # 加权: 阶段40% + 加速25% + 分歧(反)20% + 见顶(反)15%
        score = (
            stage_val * 0.40
            + accel_val * 0.25
            - diverge_val * 0.20
            - topping_val * 0.15
        )
        score = max(0, min(100, score))

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(score),
            direction=self.direction,
        )


# 注册
FactorRegistry.register(LifecycleStageFactor())
FactorRegistry.register(AccelerationSignalFactor())
FactorRegistry.register(DivergenceFactor())
FactorRegistry.register(ToppingWarningFactor())
FactorRegistry.register(LifecycleScoreFactor())
