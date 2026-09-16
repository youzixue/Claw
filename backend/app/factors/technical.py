"""技术因子(8) — 价格/趋势/波动/量价"""

import numpy as np
import pandas as pd
from app.factors.base import FactorBase, FactorCategory, FactorResult, FactorRegistry, validate_factor_inputs


class MA5Factor(FactorBase):
    """5日均线偏离度"""
    factor_name = "ma5_bias"
    input_window = 5
    category = FactorCategory.TECHNICAL
    direction = 1
    description = "收盘价偏离5日均线幅度，正值=均线之上"
    dependencies = ["close"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if len(df) < 5 or "close" not in df.columns:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)
        close = df["close"].iloc[-1]
        ma5 = df["close"].rolling(5).mean().iloc[-1]
        bias = (close - ma5) / ma5 * 100 if ma5 != 0 else np.nan
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(bias),
            direction=self.direction,
        )


class MA20Factor(FactorBase):
    """20日均线偏离度"""
    factor_name = "ma20_bias"
    input_window = 20
    category = FactorCategory.TECHNICAL
    direction = 1
    description = "收盘价偏离20日均线幅度，趋势强度指标"
    dependencies = ["close"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if len(df) < 20 or "close" not in df.columns:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)
        close = df["close"].iloc[-1]
        ma20 = df["close"].rolling(20).mean().iloc[-1]
        bias = (close - ma20) / ma20 * 100 if ma20 != 0 else np.nan
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(bias),
            direction=self.direction,
        )


class MACDFactor(FactorBase):
    """MACD金叉/死叉信号"""
    factor_name = "macd_signal"
    input_window = None
    category = FactorCategory.TECHNICAL
    direction = 1
    description = "MACD金叉=1/死叉=-1/无信号=0，含DIF强度"
    dependencies = ["close"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if len(df) < 26 or "close" not in df.columns:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        close = df["close"]
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        macd_hist = 2 * (dif - dea)

        # 金叉/死叉判断
        signal = 0
        if len(dif) >= 2:
            if dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2]:
                signal = 1   # 金叉
            elif dif.iloc[-1] < dea.iloc[-1] and dif.iloc[-2] >= dea.iloc[-2]:
                signal = -1  # 死叉

        # DIF强度(归一化到0-100)
        dif_strength = 0.0
        if close.iloc[-1] > 0:
            raw = abs(dif.iloc[-1]) / close.iloc[-1] * 1000
            dif_strength = self._safe_value(raw, default=0.0)

        raw_val = signal + dif_strength * 0.1
        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(raw_val, default=None),
            direction=self.direction,
            meta={"cross": signal, "dif": float(dif.iloc[-1]), "dea": float(dea.iloc[-1])},
        )


class RSIFactor(FactorBase):
    """RSI相对强弱"""
    factor_name = "rsi_14"
    input_window = 15
    category = FactorCategory.TECHNICAL
    direction = -1  # RSI>70超买, <30超卖, 中性区偏高反而好(短线)
    description = "14日RSI，超买区>70，超卖区<30"
    dependencies = ["close"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if len(df) < 15 or "close" not in df.columns:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        close = df["close"]
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()

        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        rsi_val = rsi.iloc[-1]

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(rsi_val),
            direction=self.direction,
        )


class KDJGoldFactor(FactorBase):
    """KDJ金叉信号"""
    factor_name = "kdj_golden"
    input_window = None
    category = FactorCategory.TECHNICAL
    direction = 1
    description = "KDJ金叉=1/死叉=-1/无=0，含K值强度"
    dependencies = ["close", "high", "low"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if len(df) < 9 or not all(c in df.columns for c in ["close", "high", "low"]):
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        low9 = df["low"].rolling(9).min()
        high9 = df["high"].rolling(9).max()
        rsv = (df["close"] - low9) / (high9 - low9).replace(0, np.nan) * 100

        k = rsv.ewm(com=2, adjust=False).mean()
        d = k.ewm(com=2, adjust=False).mean()
        j = 3 * k - 2 * d

        signal = 0
        if len(k) >= 2:
            if k.iloc[-1] > d.iloc[-1] and k.iloc[-2] <= d.iloc[-2]:
                signal = 1   # 金叉
            elif k.iloc[-1] < d.iloc[-1] and k.iloc[-2] >= d.iloc[-2]:
                signal = -1  # 死叉

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(signal + k.iloc[-1] * 0.01),
            direction=self.direction,
            meta={"K": float(k.iloc[-1]), "D": float(d.iloc[-1]), "J": float(j.iloc[-1])},
        )


class BollingerFactor(FactorBase):
    """布林带位置"""
    factor_name = "boll_position"
    input_window = 20
    category = FactorCategory.TECHNICAL
    direction = 1
    description = "价格在布林带中的位置(0=下轨, 1=上轨)，突破上轨>1"
    dependencies = ["close"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if len(df) < 20 or "close" not in df.columns:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        close = df["close"]
        ma20 = close.rolling(20).mean()
        std20 = close.rolling(20).std()
        upper = ma20 + 2 * std20
        lower = ma20 - 2 * std20

        band_width = upper.iloc[-1] - lower.iloc[-1]
        pos = (close.iloc[-1] - lower.iloc[-1]) / band_width if band_width != 0 and not np.isnan(band_width) else np.nan

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(pos),
            direction=self.direction,
            meta={"upper": float(upper.iloc[-1]), "lower": float(lower.iloc[-1])},
        )


class VolumeRatioFactor(FactorBase):
    """量比(今日均量/5日均量)"""
    factor_name = "volume_ratio"
    input_window = 6
    category = FactorCategory.TECHNICAL
    direction = 1
    description = "量比>2为放量，<0.5为缩量"
    dependencies = ["volume"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if len(df) < 6 or "volume" not in df.columns:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        vol = df["volume"]
        avg5 = vol.iloc[-6:-1].mean()
        today_avg = vol.iloc[-1]
        ratio = today_avg / avg5 if avg5 > 0 else np.nan

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(ratio),
            direction=self.direction,
        )


class TurnoverRateFactor(FactorBase):
    """换手率"""
    factor_name = "turnover_rate"
    category = FactorCategory.TECHNICAL
    direction = 1
    description = "换手率，高换手=活跃"
    dependencies = ["turnover"]

    @validate_factor_inputs()
    def calculate(self, df: pd.DataFrame, **kwargs) -> FactorResult:
        if "turnover" not in df.columns or len(df) == 0:
            return FactorResult(factor_name=self.factor_name, confidence=0.0)

        return FactorResult(
            factor_name=self.factor_name,
            value=self._safe_value(df["turnover"].iloc[-1]),
            direction=self.direction,
        )


# 注册
FactorRegistry.register(MA5Factor())
FactorRegistry.register(MA20Factor())
FactorRegistry.register(MACDFactor())
FactorRegistry.register(RSIFactor())
FactorRegistry.register(KDJGoldFactor())
FactorRegistry.register(BollingerFactor())
FactorRegistry.register(VolumeRatioFactor())
FactorRegistry.register(TurnoverRateFactor())
