"""突破信号检测 — 技术面突破识别 + 真假突破判断 + 回踩概率

v2: 增加突破质量评估、均线排列状态、假突破检测、回踩概率计算
"""

from dataclasses import dataclass, field
from typing import Optional
from loguru import logger


@dataclass
class BreakthroughSignal:
    """突破信号"""
    code: str
    name: str
    signal_type: str               # price_high/ma/bollinger/volume/false_breakout
    strength: str                  # strong/moderate/weak
    score: float                   # 突破强度 0-100
    detail: dict = field(default_factory=dict)
    description: str = ""
    # v2新增
    quality_score: float = 0       # 突破质量评分 0-100
    ma_status: str = ""            # 均线排列: multi_long/long/mixed/short
    is_false_breakout: bool = False  # 假突破标记
    pullback_probability: str = ""  # 回踩概率: high/medium/low
    days_near_pressure: int = 0    # 在压力位附近盘整天数


class BreakthroughDetector:
    """突破信号检测器"""

    def _assess_ma_status(
        self,
        ma5: float = 0, ma10: float = 0, ma20: float = 0, ma60: float = 0,
    ) -> str:
        """评估均线排列状态

        Returns:
            multi_long: MA5>MA10>MA20>MA60 完美多头
            long: 短期均线>长期均线但非完美排列
            mixed: 均线缠绕
            short: 空头排列
        """
        if ma5 <= 0 or ma10 <= 0 or ma20 <= 0 or ma60 <= 0:
            return "mixed"

        if ma5 > ma10 > ma20 > ma60:
            return "multi_long"
        elif ma5 > ma20 and ma10 > ma60:
            return "long"
        elif ma5 < ma10 < ma20 < ma60:
            return "short"
        else:
            return "mixed"

    def _calc_days_near_pressure(
        self,
        price: float,
        pressure_level: float,
        recent_closes: list[float] = None,
    ) -> int:
        """计算在压力位附近盘整的天数

        "附近"定义: 价格在压力位的95%-105%范围内

        Args:
            price: 当前价格
            pressure_level: 压力位价格
            recent_closes: 近N日收盘价列表(从远到近)

        Returns:
            在压力位附近的天数
        """
        if pressure_level <= 0 or not recent_closes:
            return 0

        upper = pressure_level * 1.05
        lower = pressure_level * 0.95

        days = 0
        for close in reversed(recent_closes):
            if lower <= close <= upper:
                days += 1
            else:
                break  # 从最近一天开始计数，不连续就停

        return days

    def _estimate_pullback_probability(
        self,
        volume_ratio: float = 1.0,
        ma_status: str = "mixed",
        days_near_pressure: int = 0,
        is_false_breakout: bool = False,
    ) -> str:
        """估算回踩概率

        基于历史统计规律:
        - 放量突破 + 多头排列 + 充分盘整 → 回踩概率低
        - 缩量突破 + 均线缠绕 + 无盘整 → 回踩概率高
        - 假突破特征 → 回踩概率极高

        Returns:
            high/medium/low
        """
        score = 50  # 基准：中等

        # 量比影响
        if volume_ratio >= 2.0:
            score -= 20  # 放量突破，回踩概率降低
        elif volume_ratio >= 1.5:
            score -= 10
        elif volume_ratio < 1.0:
            score += 25  # 缩量突破，回踩概率高
        elif volume_ratio < 0.7:
            score += 35  # 严重缩量

        # 均线排列影响
        ma_adj = {"multi_long": -15, "long": -5, "mixed": 10, "short": 25}
        score += ma_adj.get(ma_status, 0)

        # 盘整天数影响
        if days_near_pressure >= 7:
            score -= 15  # 充分蓄势，回踩少
        elif days_near_pressure >= 4:
            score -= 5
        elif days_near_pressure <= 1:
            score += 15  # 冲动突破，回踩多

        # 假突破
        if is_false_breakout:
            score += 30

        # 映射到概率等级
        if score >= 65:
            return "high"
        elif score >= 40:
            return "medium"
        else:
            return "low"

    def detect(
        self,
        code: str,
        name: str,
        price: float,
        volume: float,
        high_20d: float = 0,
        high_60d: float = 0,
        high_120d: float = 0,
        ma5: float = 0,
        ma10: float = 0,
        ma20: float = 0,
        ma60: float = 0,
        boll_upper: float = 0,
        avg_volume_20d: float = 0,
        # v2新增参数
        open_price: float = 0,
        low_price: float = 0,
        recent_closes: list[float] = None,
        prev_high: float = 0,        # 前一日最高价(用于假突破检测)
    ) -> list[BreakthroughSignal]:
        """检测突破信号

        Args:
            code/name: 股票信息
            price: 当前价(或收盘价)
            volume: 当前成交量
            high_Nd: N日最高价
            ma_N: N日均线
            boll_upper: 布林带上轨
            avg_volume_20d: 20日平均成交量
            open_price: 开盘价(假突破检测)
            low_price: 最低价(假突破检测)
            recent_closes: 近N日收盘价(盘整天数计算)
            prev_high: 前一日最高价

        Returns:
            突破信号列表
        """
        signals = []
        volume_ratio = volume / avg_volume_20d if avg_volume_20d > 0 else 1.0

        # 均线排列状态
        ma_status = self._assess_ma_status(ma5, ma10, ma20, ma60)

        # 假突破检测
        is_false = False
        if open_price > 0 and low_price > 0 and high_20d > 0:
            # 突破N日高点但收盘回落到高点以下
            if price > high_20d and (high_20d - price) / (high_20d - open_price) < -0.3 if (high_20d - open_price) > 0 else False:
                is_false = False
            # 更直接的判断: 盘中突破20日高点但收盘跌回
            intraday_high = max(price, open_price) if open_price > 0 else price
            # 简化: 如果上影线很长且收盘低于突破价
            if high_20d > 0 and price < high_20d:
                # 收盘低于20日高点，可能是假突破
                upper_shadow = max(price, open_price) - high_20d if max(price, open_price) > high_20d else 0
                if upper_shadow > 0 and (max(price, open_price) - price) / (max(price, open_price) - min(price, open_price) + 0.01) > 0.5:
                    is_false = True

        # 1. 价格突破
        pressure_level = 0
        if high_120d > 0 and price > high_120d:
            strength = "strong"
            score = 90
            pressure_level = high_120d
            desc = f"突破120日新高{high_120d:.2f}"
        elif high_60d > 0 and price > high_60d:
            strength = "moderate"
            score = 75
            pressure_level = high_60d
            desc = f"突破60日新高{high_60d:.2f}"
        elif high_20d > 0 and price > high_20d:
            strength = "weak"
            score = 55
            pressure_level = high_20d
            desc = f"突破20日新高{high_20d:.2f}"
        else:
            strength = ""
            score = 0
            desc = ""

        if strength:
            # 量能配合加分
            if volume_ratio >= 1.5:
                score = min(100, score + 10)
                desc += "，放量确认"
            elif volume_ratio < 0.7:
                score = max(0, score - 10)
                desc += "，缩量突破(待确认)"

            # 计算盘整天数
            days_near = self._calc_days_near_pressure(
                price, pressure_level, recent_closes
            ) if recent_closes else 0

            # 估算回踩概率
            pullback_prob = self._estimate_pullback_probability(
                volume_ratio, ma_status, days_near, is_false
            )

            # 突破质量评分
            quality = self._calc_quality_score(
                volume_ratio, ma_status, days_near, is_false
            )

            # 假突破修正
            if is_false:
                desc += " ⚠️疑似假突破"
                score = max(score - 20, 20)

            signals.append(BreakthroughSignal(
                code=code, name=name,
                signal_type="false_breakout" if is_false else "price_high",
                strength=strength, score=score,
                detail={"price": price, "high": pressure_level, "volume_ratio": round(volume_ratio, 2)},
                description=desc,
                quality_score=quality,
                ma_status=ma_status,
                is_false_breakout=is_false,
                pullback_probability=pullback_prob,
                days_near_pressure=days_near,
            ))

        # 2. 均线突破
        ma_breaks = []
        if ma60 > 0 and price > ma60 > 0:
            ma_breaks.append(("MA60", ma60, 70))
        if ma20 > 0 and price > ma20:
            ma_breaks.append(("MA20", ma20, 55))
        if ma10 > 0 and price > ma10:
            ma_breaks.append(("MA10", ma10, 45))
        if ma5 > 0 and price > ma5:
            ma_breaks.append(("MA5", ma5, 35))

        # 多头排列: MA5 > MA10 > MA20 > MA60
        if ma5 > 0 and ma10 > 0 and ma20 > 0 and ma60 > 0:
            if ma5 > ma10 > ma20 > ma60:
                signals.append(BreakthroughSignal(
                    code=code, name=name,
                    signal_type="ma",
                    strength="strong", score=85,
                    detail={"alignment": "bullish", "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60},
                    description="均线多头排列",
                    quality_score=80,
                    ma_status="multi_long",
                    pullback_probability="low",
                ))
            elif ma5 < ma10 < ma20 < ma60:
                signals.append(BreakthroughSignal(
                    code=code, name=name,
                    signal_type="ma",
                    strength="weak", score=25,
                    detail={"alignment": "bearish"},
                    description="均线空头排列",
                    quality_score=20,
                    ma_status="short",
                    pullback_probability="high",
                ))

        # 站上重要均线
        for ma_name, ma_val, base_score in ma_breaks:
            days_near = self._calc_days_near_pressure(
                price, ma_val, recent_closes
            ) if recent_closes else 0
            pullback = self._estimate_pullback_probability(
                volume_ratio, ma_status, days_near
            )
            quality = self._calc_quality_score(
                volume_ratio, ma_status, days_near
            )
            signals.append(BreakthroughSignal(
                code=code, name=name,
                signal_type="ma",
                strength="moderate" if base_score >= 60 else "weak",
                score=base_score,
                detail={"ma": ma_name, "value": ma_val, "price": price},
                description=f"站上{ma_name}({ma_val:.2f})",
                quality_score=quality,
                ma_status=ma_status,
                pullback_probability=pullback,
                days_near_pressure=days_near,
            ))

        # 3. 布林带突破
        if boll_upper > 0 and price > boll_upper:
            boll_score = 65
            if volume_ratio >= 1.5:
                boll_score += 10
            days_near = 0
            pullback = self._estimate_pullback_probability(
                volume_ratio, ma_status, days_near
            )
            quality = self._calc_quality_score(
                volume_ratio, ma_status, days_near
            )
            signals.append(BreakthroughSignal(
                code=code, name=name,
                signal_type="bollinger",
                strength="moderate", score=boll_score,
                detail={"boll_upper": boll_upper, "volume_ratio": round(volume_ratio, 2)},
                description=f"突破布林上轨({boll_upper:.2f})",
                quality_score=quality,
                ma_status=ma_status,
                pullback_probability=pullback,
            ))

        # 去重排序
        signals.sort(key=lambda s: s.score, reverse=True)
        return signals

    def _calc_quality_score(
        self,
        volume_ratio: float,
        ma_status: str,
        days_near_pressure: int,
        is_false: bool = False,
    ) -> float:
        """计算突破质量评分

        基于4个维度:
        1. 量能配合 (40分)
        2. 均线排列 (30分)
        3. 盘整充分度 (20分)
        4. 是否假突破 (10分)

        Returns:
            0-100
        """
        score = 0

        # 1. 量能(40分)
        if volume_ratio >= 2.5:
            score += 40
        elif volume_ratio >= 2.0:
            score += 35
        elif volume_ratio >= 1.5:
            score += 28
        elif volume_ratio >= 1.0:
            score += 18
        elif volume_ratio >= 0.7:
            score += 10
        else:
            score += 5

        # 2. 均线(30分)
        ma_scores = {"multi_long": 30, "long": 22, "mixed": 12, "short": 5}
        score += ma_scores.get(ma_status, 12)

        # 3. 盘整(20分)
        if days_near_pressure >= 7:
            score += 20
        elif days_near_pressure >= 4:
            score += 15
        elif days_near_pressure >= 2:
            score += 8
        else:
            score += 3

        # 4. 假突破(10分)
        score += 0 if is_false else 10

        return min(100, score)

    def detect_batch(
        self,
        stocks: list[dict],
    ) -> list[BreakthroughSignal]:
        """批量检测突破信号

        Args:
            stocks: [{code, name, price, volume, high_20d, ...}]

        Returns:
            所有突破信号(按score降序)
        """
        all_signals = []
        for s in stocks:
            signals = self.detect(
                code=s["code"],
                name=s.get("name", ""),
                price=s.get("price", 0),
                volume=s.get("volume", 0),
                high_20d=s.get("high_20d", 0),
                high_60d=s.get("high_60d", 0),
                high_120d=s.get("high_120d", 0),
                ma5=s.get("ma5", 0),
                ma10=s.get("ma10", 0),
                ma20=s.get("ma20", 0),
                ma60=s.get("ma60", 0),
                boll_upper=s.get("boll_upper", 0),
                avg_volume_20d=s.get("avg_volume_20d", 0),
                open_price=s.get("open_price", 0),
                low_price=s.get("low_price", 0),
                recent_closes=s.get("recent_closes"),
                prev_high=s.get("prev_high", 0),
            )
            all_signals.extend(signals)

        all_signals.sort(key=lambda s: s.score, reverse=True)
        return all_signals
