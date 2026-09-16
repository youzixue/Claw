"""筹码集中度分析 — V2.2融合增强版

逻辑:
1. 换手率递减 + 涨幅不变 → 筹码锁定(集中)
2. 换手率突然放大 + 高位 → 筹码分散(出货)
3. 股东户数减少 → 筹码集中(季报数据)
4. 量价背离 → 筹码状态判断
5. 集中度评分: 0-100

V2.2融合增强:
- 新增: concentration_90/concentration_70 (90%/70%筹码集中度)
- 新增: pattern 形态识别(单峰密集/双峰分布/低位密集/高位密集/多峰分散)
- 新增: profit_ratio 获利盘比例
- 新增: support_levels/resistance_levels 支撑/压力位
- 新增: signal_strength 信号强度(0-10), 可直接注入BullScoreModel
- 新增: avg_cost/median_cost 成本指标
"""

from dataclasses import dataclass, field
from typing import Optional
from loguru import logger


@dataclass
class ChipResult:
    """筹码集中度结果"""
    code: str
    name: str
    score: float                    # 集中度评分 0-100
    status: str                     # locking/accumulating/distributing/scattering
    description: str = ""
    detail: dict = field(default_factory=dict)
    # V2.2融合新增字段
    concentration_90: float = 0.0   # 90%筹码集中度(越小越集中)
    concentration_70: float = 0.0   # 70%筹码集中度(越小越集中)
    pattern: str = ""               # 单峰密集/双峰分布/低位密集/高位密集/多峰分散
    profit_ratio: float = 0.0       # 获利盘比例 0-1
    signal_strength: float = 5.0    # 筹码信号强度 0-10
    avg_cost: float = 0.0           # 加权平均成本
    support_levels: list = field(default_factory=list)   # 支撑位
    resistance_levels: list = field(default_factory=list) # 压力位


class ChipConcentrationAnalyzer:
    """筹码集中度分析器 — V2.2融合增强版"""

    # 集中度阈值 (V2.2标准)
    CONCENTRATION_THRESHOLDS = {
        "high": 0.08,      # 高度集中
        "medium": 0.15,    # 中度集中
        "low": 0.25,       # 低度集中
    }

    def analyze(
        self,
        code: str,
        name: str,
        recent_turnovers: list[float] = None,
        recent_changes: list[float] = None,
        recent_volumes: list[float] = None,
        recent_closes: list[float] = None,
        recent_highs: list[float] = None,
        recent_lows: list[float] = None,
        shareholder_change: float = None,
        current_price: float = 0,
    ) -> ChipResult:
        """分析个股筹码集中度

        Args:
            code: 股票代码
            name: 股票名称
            recent_turnovers: 近N日换手率
            recent_changes: 近N日涨跌幅%
            recent_volumes: 近N日成交量
            recent_closes: 近N日收盘价(V2.2: 计算成本分布)
            recent_highs: 近N日最高价(V2.2: 计算价格分布)
            recent_lows: 近N日最低价(V2.2: 计算价格分布)
            shareholder_change: 股东户数环比变化%(负=集中)
            current_price: 当前价格(V2.2: 计算获利盘)

        Returns:
            ChipResult
        """
        recent_turnovers = recent_turnovers or []
        recent_changes = recent_changes or []
        recent_volumes = recent_volumes or []
        recent_closes = recent_closes or []
        recent_highs = recent_highs or []
        recent_lows = recent_lows or []

        score = 50  # 默认中性
        status = "accumulating"
        detail = {}
        description = ""

        # V2.2融合新增
        concentration_90 = 0.0
        concentration_70 = 0.0
        pattern = ""
        profit_ratio = 0.0
        signal_strength = 5.0
        avg_cost = 0.0
        support_levels = []
        resistance_levels = []

        # 1. 换手率趋势判断
        if len(recent_turnovers) >= 5:
            avg_first_half = sum(recent_turnovers[:len(recent_turnovers)//2]) / (len(recent_turnovers)//2)
            avg_second_half = sum(recent_turnovers[len(recent_turnovers)//2:]) / (len(recent_turnovers) - len(recent_turnovers)//2)

            if avg_second_half < avg_first_half * 0.7:
                detail["turnover_trend"] = "decreasing"
                score += 20
                description = "换手率递减，筹码锁定"
            elif avg_second_half > avg_first_half * 1.5:
                detail["turnover_trend"] = "increasing"
                score -= 15
                description = "换手率递增，筹码分散"
            else:
                detail["turnover_trend"] = "stable"

            current_turnover = recent_turnovers[-1] if recent_turnovers else 0
            if current_turnover < 2:
                detail["turnover_level"] = "low"
                score += 10
            elif current_turnover > 20:
                detail["turnover_level"] = "high"
                score -= 10

        # 2. 量价配合判断
        if len(recent_volumes) >= 3 and len(recent_changes) >= 3:
            vol_trend = recent_volumes[-1] > recent_volumes[-2] > recent_volumes[-3]
            price_up = recent_changes[-1] > 0 and recent_changes[-2] > 0

            if price_up and not vol_trend:
                detail["volume_price"] = "price_up_vol_shrink"
                score += 15
                if description:
                    description += "；量缩价涨"
                else:
                    description = "量缩价涨，筹码锁定"
            elif price_up and vol_trend:
                detail["volume_price"] = "price_up_vol_expand"
            elif not price_up and vol_trend:
                detail["volume_price"] = "price_down_vol_expand"
                score -= 15
                if description:
                    description += "；放量下跌"
                else:
                    description = "放量下跌，筹码分散"

        # 3. 股东户数变化
        if shareholder_change is not None:
            detail["shareholder_change_pct"] = shareholder_change
            if shareholder_change < -10:
                score += 25
                description = f"股东户数减少{abs(shareholder_change):.0f}%，筹码高度集中"
            elif shareholder_change < -5:
                score += 15
                description = f"股东户数减少{abs(shareholder_change):.0f}%，筹码趋于集中"
            elif shareholder_change > 10:
                score -= 20
                description = f"股东户数增加{shareholder_change:.0f}%，筹码分散"
            elif shareholder_change > 5:
                score -= 10
                description = f"股东户数增加{shareholder_change:.0f}%，筹码趋于分散"

        # ===== V2.2融合: 价格-成交量分布计算 =====
        if len(recent_closes) >= 10 and len(recent_volumes) >= 10 and current_price > 0:
            try:
                # 简化版价格分布计算(不需要numpy)
                price_min = min(recent_lows) if recent_lows else min(recent_closes)
                price_max = max(recent_highs) if recent_highs else max(recent_closes)

                if price_max > price_min:
                    # 分10档
                    bin_count = 10
                    bin_size = (price_max - price_min) / bin_count
                    bins = [0] * bin_count
                    total_vol = 0

                    for i in range(min(len(recent_closes), len(recent_volumes))):
                        close = recent_closes[i] if i < len(recent_closes) else 0
                        vol = recent_volumes[i] if i < len(recent_volumes) else 0
                        if close and vol and close >= price_min and close <= price_max:
                            bin_idx = min(int((close - price_min) / bin_size), bin_count - 1)
                            time_weight = (i + 1) / len(recent_closes)  # 近期权重高
                            bins[bin_idx] += vol * time_weight
                            total_vol += vol * time_weight

                    if total_vol > 0:
                        # 归一化
                        pct = [b / total_vol for b in bins]
                        cumsum = []
                        s = 0
                        for p in pct:
                            s += p
                            cumsum.append(s)

                        # 加权平均成本
                        avg_cost = sum(
                            (price_min + (i + 0.5) * bin_size) * pct[i]
                            for i in range(bin_count)
                        )

                        # 获利盘比例 (当前价格以下的筹码)
                        current_bin = min(int((current_price - price_min) / bin_size), bin_count - 1)
                        current_bin = max(0, current_bin)
                        profit_ratio = min(1.0, cumsum[current_bin] if current_bin < bin_count else 1.0)

                        # 集中度90%/70% (包含对应比例筹码的最小价格区间/当前价格)
                        concentration_90 = self._calc_concentration_from_bins(
                            pct, bin_size, price_min, current_price, 0.90
                        )
                        concentration_70 = self._calc_concentration_from_bins(
                            pct, bin_size, price_min, current_price, 0.70
                        )

                        # 形态识别
                        pattern = self._identify_pattern_from_bins(
                            pct, concentration_90, current_price, avg_cost
                        )

                        # 支撑/压力位
                        support_levels, resistance_levels = self._calc_support_resistance_from_bins(
                            pct, price_min, bin_size, current_price
                        )

                        detail["concentration_90"] = round(concentration_90, 3)
                        detail["concentration_70"] = round(concentration_70, 3)
                        detail["profit_ratio"] = round(profit_ratio, 2)
                        detail["avg_cost"] = round(avg_cost, 2)
                        detail["pattern"] = pattern
                        detail["price_distribution"] = [round(p, 3) for p in pct]

            except Exception as e:
                logger.debug(f"{code} V2.2筹码分布计算失败: {e}")

        # ===== V2.2融合: 信号强度计算 (0-10) =====
        signal_strength = 5.0

        # 集中度评分 (0-3分)
        if concentration_90 > 0:
            if concentration_90 < self.CONCENTRATION_THRESHOLDS["high"]:
                signal_strength += 3.0
            elif concentration_90 < self.CONCENTRATION_THRESHOLDS["medium"]:
                signal_strength += 2.0
            elif concentration_90 < self.CONCENTRATION_THRESHOLDS["low"]:
                signal_strength += 1.0

        # 获利盘评分 (0-2分)
        if 0.5 < profit_ratio < 0.8:
            signal_strength += 2.0
        elif 0.3 < profit_ratio < 0.9:
            signal_strength += 1.0

        # 形态评分 (0-3分)
        pattern_score_map = {
            "单峰密集": 3.0,
            "低位密集": 2.5,
            "双峰分布": 2.0,
            "高位密集": -1.0,
            "多峰分散": 0.0,
        }
        signal_strength += pattern_score_map.get(pattern, 0)

        # 状态评分 (0-2分) — 基于已有score推算
        if score >= 75:
            signal_strength += 2.0  # 高度锁定
        elif score >= 55:
            signal_strength += 1.0  # 收集中
        elif score < 35:
            signal_strength -= 1.0  # 散乱

        signal_strength = max(0, min(10, signal_strength))

        # 评分限制
        score = max(0, min(100, score))

        # 状态判定
        if score >= 75:
            status = "locking"
        elif score >= 55:
            status = "accumulating"
        elif score >= 35:
            status = "distributing"
        else:
            status = "scattering"

        return ChipResult(
            code=code,
            name=name,
            score=round(score, 1),
            status=status,
            description=description or "筹码状态中性",
            detail=detail,
            concentration_90=round(concentration_90, 3),
            concentration_70=round(concentration_70, 3),
            pattern=pattern,
            profit_ratio=round(profit_ratio, 2),
            signal_strength=round(signal_strength, 1),
            avg_cost=round(avg_cost, 2),
            support_levels=[round(s, 2) for s in support_levels[:3]],
            resistance_levels=[round(r, 2) for r in resistance_levels[:3]],
        )

    @staticmethod
    def _calc_concentration_from_bins(
        pct: list[float], bin_size: float, price_min: float,
        current_price: float, target: float
    ) -> float:
        """计算集中度 — 包含target比例筹码的最小价格区间/当前价格"""
        if current_price <= 0:
            return 0.0

        # 滑动窗口: 找到包含target比例的最小窗口
        best_range = float('inf')
        n = len(pct)

        for start in range(n):
            cumsum = 0
            for end in range(start, n):
                cumsum += pct[end]
                if cumsum >= target:
                    price_range = (end - start + 1) * bin_size
                    ratio = price_range / current_price
                    if ratio < best_range:
                        best_range = ratio
                    break

        return min(1.0, best_range) if best_range != float('inf') else 1.0

    @staticmethod
    def _identify_pattern_from_bins(
        pct: list[float], conc_90: float, current_price: float, avg_cost: float
    ) -> str:
        """识别筹码形态"""
        # 高度集中
        if conc_90 < 0.08:
            # 判断位置: 当前价格 > 平均成本1.3倍 → 高位密集(派发), 否则低位密集(吸筹)
            if avg_cost > 0 and current_price > avg_cost * 1.3:
                return "高位密集"
            else:
                return "低位密集"
        elif conc_90 < 0.15:
            # 中度集中 → 单峰或双峰
            # 简化: 看pct是否有两个显著峰
            max_pct = max(pct) if pct else 0
            peak_count = sum(1 for p in pct if p > max_pct * 0.6)
            if peak_count <= 2:
                return "单峰密集"
            else:
                return "双峰分布"
        else:
            return "多峰分散"

    @staticmethod
    def _calc_support_resistance_from_bins(
        pct: list[float], price_min: float, bin_size: float, current_price: float
    ) -> tuple[list[float], list[float]]:
        """从价格分布计算支撑位和压力位"""
        if not pct or current_price <= 0:
            return [], []

        # 找到高密度档位(>平均值1.5倍)
        avg_pct = sum(pct) / len(pct) if pct else 0
        threshold = avg_pct * 1.5

        support = []
        resistance = []

        for i, p in enumerate(pct):
            price = price_min + (i + 0.5) * bin_size
            if p > threshold:
                if price < current_price:
                    support.append(price)
                elif price > current_price:
                    resistance.append(price)

        # 按距离当前价格排序(近→远)
        support.sort(key=lambda x: current_price - x)
        resistance.sort(key=lambda x: x - current_price)

        return support[:3], resistance[:3]

    def analyze_batch(
        self,
        stocks: list[dict],
    ) -> list[ChipResult]:
        """批量分析筹码集中度

        Args:
            stocks: [{code, name, recent_turnovers, recent_changes, ...}]

        Returns:
            按score降序
        """
        results = []
        for s in stocks:
            result = self.analyze(
                code=s["code"],
                name=s.get("name", ""),
                recent_turnovers=s.get("recent_turnovers"),
                recent_changes=s.get("recent_changes"),
                recent_volumes=s.get("recent_volumes"),
                recent_closes=s.get("recent_closes"),
                recent_highs=s.get("recent_highs"),
                recent_lows=s.get("recent_lows"),
                shareholder_change=s.get("shareholder_change"),
                current_price=s.get("current_price", 0),
            )
            results.append(result)

        results.sort(key=lambda r: r.score, reverse=True)
        return results
