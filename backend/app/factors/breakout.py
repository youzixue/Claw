"""爆发因子(5) — 量价异动/资金集中度/涨停封板/板块龙头辨识/首板质量"""

import numpy as np
import pandas as pd
from app.factors.base import FactorBase, FactorCategory, FactorResult, FactorRegistry, validate_factor_inputs


class VolumeSpikeFactor(FactorBase):
    """量价异动度"""
    factor_name = "volume_spike"
    input_window = 21
    category = FactorCategory.BREAKOUT
    direction = 1
    description = "今日成交量/20日均量，>3=异常放量"
    dependencies = ["volume"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if "volume" not in df.columns or len(df) < 21:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        vol = df["volume"]
        avg20 = vol.iloc[-21:-1].mean()
        today = vol.iloc[-1]
        spike = today / avg20 if avg20 > 0 else np.nan

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(spike),
            direction=self.direction,
        )


class FundConcentrationFactor(FactorBase):
    """资金集中度"""
    factor_name = "fund_concentration"
    category = FactorCategory.BREAKOUT
    direction = 1
    description = "大单占比=大单成交额/总成交额，>40%=资金高度集中"
    dependencies = ["big_net_inflow", "amount"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if "big_net_inflow" not in df.columns or "amount" not in df.columns:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)
        if len(df) == 0:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        big_abs = abs(df["big_net_inflow"].iloc[-1])
        amount = df["amount"].iloc[-1]
        concentration = (big_abs / amount * 100) if amount and amount > 0 else np.nan

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(concentration),
            direction=self.direction,
        )


class SealStrengthFactor(FactorBase):
    """涨停封板强度"""
    factor_name = "seal_strength"
    context_bounds = {"seal_amount":[0,None],"circulating_market_cap":[0,None]}
    required_context = ["seal_amount","circulating_market_cap"]
    category = FactorCategory.BREAKOUT
    direction = 1
    description = "封单量/流通市值，>3%=强封板"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        seal_amount = kwargs.get("seal_amount", 0)
        market_cap = kwargs.get("circulating_market_cap", 0)

        if market_cap <= 0:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        strength = seal_amount / market_cap * 100 if market_cap > 0 else np.nan

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(strength),
            direction=self.direction,
            meta={"seal_amount": seal_amount, "market_cap": market_cap},
        )


class FirstBoardQualityFactor(FactorBase):
    """首板质量"""
    factor_name = "first_board_quality"
    conditional_context = ("limit_up_time",)
    context_bounds = {"seal_amount":[0,None],"break_count":[0,None]}
    required_context = ["seal_amount","break_count"]
    category = FactorCategory.BREAKOUT
    direction = 1
    description = "首板时间+封板资金+炸板次数综合评分，早封+量大+0炸=高质量"
    dependencies = []

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        limit_up_time = kwargs.get("limit_up_time")  # 缺失时不能假定尾板
        seal_amount = kwargs.get("seal_amount", 0)
        break_count = kwargs.get("break_count", 0)

        # 时间评分(早封=好): 9:30=100, 15:00=0
        try:
            h, m, s = map(int, limit_up_time.split(":"))
            if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
                raise ValueError("invalid clock")
            clock = f"{h:02d}:{m:02d}:{s:02d}"
            if not ("09:25:00" <= clock <= "11:30:00" or "13:00:00" <= clock <= "15:00:00"):
                raise ValueError("outside session")
            minutes_from_open = (h - 9) * 60 + m - 30
            total_minutes = 5.5 * 60  # 330分钟
            time_score = max(0, (1 - minutes_from_open / total_minutes)) * 40
        except (ValueError, AttributeError):
            return self._unavailable("missing_or_invalid_limit_time", "limit_up_time")

        # 封板资金评分
        seal_score = min(seal_amount / 1e8, 3) / 3 * 30  # 3亿=满分

        # 炸板扣分
        break_penalty = break_count * 20

        quality = max(0, min(100, time_score + seal_score - break_penalty))

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(quality),
            direction=self.direction,
        )


class BreakoutEnergyFactor(FactorBase):
    """突破能量"""
    factor_name = "breakout_energy"
    input_window = 21
    category = FactorCategory.BREAKOUT
    direction = 1
    description = "量价齐升程度=(涨幅×量比)/波动率，高=强势突破"
    dependencies = ["close", "volume"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if len(df) < 21 or not all(c in df.columns for c in ["close", "volume"]):
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        close = df["close"]
        volume = df["volume"]

        # 涨幅
        change_pct = (close.iloc[-1] / close.iloc[-2] - 1) * 100 if len(close) >= 2 else 0

        # 量比
        avg20 = volume.iloc[-21:-1].mean()
        if avg20 <= 0:
            return self._unavailable("zero_volume_denominator", "volume")
        vol_ratio = volume.iloc[-1] / avg20

        # 波动率(20日)
        returns = close.pct_change().iloc[-20:]
        volatility = returns.std() * 100 if len(returns) > 0 else 1

        energy = (change_pct * vol_ratio) / max(volatility, 0.1)

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(energy),
            direction=self.direction,
        )


# 注册
FactorRegistry.register(VolumeSpikeFactor())
FactorRegistry.register(FundConcentrationFactor())
FactorRegistry.register(SealStrengthFactor())
FactorRegistry.register(FirstBoardQualityFactor())
FactorRegistry.register(BreakoutEnergyFactor())
