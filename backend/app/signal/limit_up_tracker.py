"""连板股追踪 — 连板梯队 + 晋级概率 + 封板质量

v2: 增加封板质量评估，为推送模板提供真实数据支撑
"""

from dataclasses import dataclass, field
from typing import Optional
from loguru import logger


@dataclass
class LimitUpLadder:
    """连板梯队"""
    consecutive_days: int           # 连板数
    stocks: list[dict]              # 该梯队股票列表
    count: int = 0                  # 股票数
    seal_rate: float = 0            # 封板率%


@dataclass
class SealQuality:
    """封板质量评估结果"""
    seal_amount: float = 0          # 封板资金(元)
    seal_ratio: float = 0           # 封单占成交量比
    first_seal_time: str = ""       # 首次封板时间 HH:MM
    open_times: int = 0             # 开板次数
    turnover: float = 0             # 换手率%
    volume_ratio: float = 0         # 量比
    quality_level: str = "medium"   # solid/good/medium/loose/broken
    quality_score: float = 50       # 封板质量评分 0-100
    risk_warnings: list = field(default_factory=list)  # 风险提示


@dataclass
class PromotionResult:
    """晋级概率结果"""
    code: str
    name: str
    current_days: int               # 当前连板数
    next_days: int                  # 目标连板数
    promotion_prob: float           # 晋级概率 0-1
    confidence: str                 # high/medium/low
    factors: dict = field(default_factory=dict)  # 影响因素


class LimitUpTracker:
    """连板股追踪器"""

    # 历史晋级概率(经验值，后续可从数据库动态统计)
    BASE_PROMOTION_PROBS = {
        1: 0.35,   # 1板→2板: 35%
        2: 0.25,   # 2板→3板: 25%
        3: 0.18,   # 3板→4板: 18%
        4: 0.12,   # 4板→5板: 12%
        5: 0.08,   # 5板→6板: 8%
        6: 0.05,   # 6板→7板: 5%
    }

    def evaluate_seal_quality(
        self,
        seal_amount: float = 0,
        volume: float = 0,
        first_seal_time: str = "",
        open_times: int = 0,
        turnover: float = 0,
        volume_ratio: float = 1.0,
        limit_up_time: str = "",
    ) -> SealQuality:
        """评估封板质量

        基于5个维度的量化评分：
        1. 封板资金量 (权重30%)
        2. 封板时间 (权重25%)
        3. 开板次数 (权重20%)
        4. 封单/成交比 (权重15%)
        5. 换手率水平 (权重10%)

        Args:
            seal_amount: 封板资金(元)
            volume: 成交量(元)
            first_seal_time: 首次封板时间 HH:MM
            open_times: 开板次数
            turnover: 换手率%
            volume_ratio: 量比
            limit_up_time: 最后封板时间(同first_seal_time时用)

        Returns:
            SealQuality
        """
        score = 50  # 基础分
        warnings = []

        # === 1. 封板资金评分 (权重30%) ===
        seal_score = 0
        if seal_amount >= 10e8:      # 10亿以上
            seal_score = 100
        elif seal_amount >= 5e8:     # 5亿
            seal_score = 85
        elif seal_amount >= 2e8:     # 2亿
            seal_score = 70
        elif seal_amount >= 1e8:     # 1亿
            seal_score = 55
        elif seal_amount >= 5e7:     # 5000万
            seal_score = 40
        else:
            seal_score = 20
            warnings.append("封板资金不足5000万，封板不牢")

        # === 2. 封板时间评分 (权重25%) ===
        time_score = 50
        seal_time = first_seal_time or limit_up_time
        if seal_time:
            try:
                h, m = map(int, seal_time.split(":"))
                minutes = h * 60 + m
                # 9:25竞价封板 = 最强, 14:50尾盘封板 = 最弱
                if minutes <= 9 * 60 + 35:      # 9:35前
                    time_score = 100
                elif minutes <= 10 * 60:         # 10:00前
                    time_score = 90
                elif minutes <= 10 * 60 + 30:    # 10:30前
                    time_score = 80
                elif minutes <= 11 * 60 + 30:    # 上午收盘前
                    time_score = 65
                elif minutes <= 13 * 60 + 30:    # 13:30前
                    time_score = 60
                elif minutes <= 14 * 60:         # 14:00前
                    time_score = 50
                elif minutes <= 14 * 60 + 30:    # 14:30前
                    time_score = 35
                    warnings.append("午后封板，资金分歧较大")
                else:                             # 14:30后
                    time_score = 20
                    warnings.append("尾盘封板，次日溢价不确定")
            except (ValueError, AttributeError):
                pass

        # === 3. 开板次数评分 (权重20%) ===
        if open_times == 0:
            open_score = 100
        elif open_times == 1:
            open_score = 60
            warnings.append("炸板1次，封板稳定性一般")
        elif open_times == 2:
            open_score = 35
            warnings.append("炸板2次，封板不牢，次日低开概率大")
        else:
            open_score = 15
            warnings.append(f"炸板{open_times}次，封板极不牢固")

        # === 4. 封单/成交比评分 (权重15%) ===
        seal_ratio = seal_amount / volume if volume > 0 else 0
        if seal_ratio >= 0.5:
            ratio_score = 100
        elif seal_ratio >= 0.3:
            ratio_score = 80
        elif seal_ratio >= 0.15:
            ratio_score = 60
        elif seal_ratio >= 0.05:
            ratio_score = 40
        else:
            ratio_score = 20
            warnings.append("封单占比极低，抛压风险大")

        # === 5. 换手率评分 (权重10%) ===
        if turnover <= 2:
            turnover_score = 80   # 一字板/极度缩量
        elif turnover <= 5:
            turnover_score = 90   # 低换手，筹码稳定
        elif turnover <= 12:
            turnover_score = 70   # 正常换手
        elif turnover <= 20:
            turnover_score = 50
            warnings.append(f"换手率{turnover:.1f}%偏高，筹码松动")
        else:
            turnover_score = 25
            warnings.append(f"换手率{turnover:.1f}%过高，筹码大幅换手")

        # === 加权计算 ===
        final_score = (
            seal_score * 0.30 +
            time_score * 0.25 +
            open_score * 0.20 +
            ratio_score * 0.15 +
            turnover_score * 0.10
        )
        final_score = round(max(0, min(100, final_score)), 1)

        # === 质量等级 ===
        if final_score >= 80:
            quality_level = "solid"     # 封死涨停
        elif final_score >= 60:
            quality_level = "good"      # 质量较好
        elif final_score >= 40:
            quality_level = "medium"    # 一般
        elif final_score >= 25:
            quality_level = "loose"     # 烂板
        else:
            quality_level = "broken"    # 炸板/极差

        return SealQuality(
            seal_amount=seal_amount,
            seal_ratio=round(seal_ratio, 4),
            first_seal_time=seal_time,
            open_times=open_times,
            turnover=turnover,
            volume_ratio=volume_ratio,
            quality_level=quality_level,
            quality_score=final_score,
            risk_warnings=warnings,
        )

    def build_ladder(self, limit_ups: list[dict]) -> list[LimitUpLadder]:
        """构建连板梯队

        Args:
            limit_ups: 涨停股列表, 每个 dict 包含:
                - code, name, consecutive_days, seal_amount,
                  break_count, turnover, limit_up_time

        Returns:
            按连板数降序排列的梯队列表
        """
        if not limit_ups:
            return []

        # 按连板数分组
        groups: dict[int, list[dict]] = {}
        for s in limit_ups:
            days = s.get("consecutive_days", 1)
            groups.setdefault(days, []).append(s)

        # 构建梯队
        ladders = []
        for days in sorted(groups.keys(), reverse=True):
            stocks = groups[days]
            # 计算封板率: 炸板次数=0的占比
            sealed = sum(1 for s in stocks if s.get("break_count", 0) == 0)
            seal_rate = round(sealed / len(stocks) * 100, 1) if stocks else 0

            ladders.append(LimitUpLadder(
                consecutive_days=days,
                stocks=sorted(stocks, key=lambda s: s.get("seal_amount", 0), reverse=True),
                count=len(stocks),
                seal_rate=seal_rate,
            ))

        return ladders

    def predict_promotion(
        self,
        code: str,
        name: str,
        current_days: int,
        stock_data: dict = None,
    ) -> PromotionResult:
        """预测晋级概率

        Args:
            code: 股票代码
            name: 股票名称
            current_days: 当前连板数
            stock_data: 额外数据(seal_amount, break_count, turnover, sector_strength...)

        Returns:
            晋级概率 + 影响因素
        """
        stock_data = stock_data or {}

        # 基础概率
        base_prob = self.BASE_PROMOTION_PROBS.get(current_days, 0.03)

        # 因子调整
        adjustments = {}

        # 封板资金(越大越容易晋级)
        seal = stock_data.get("seal_amount", 0)
        if seal > 5e8:  # 5亿以上大单封板
            adjustments["seal_strength"] = 0.10
        elif seal > 2e8:  # 2亿
            adjustments["seal_strength"] = 0.05
        elif seal > 5e7:  # 5000万
            adjustments["seal_strength"] = 0.02
        else:
            adjustments["seal_strength"] = -0.05

        # 炸板次数(越多越难)
        breaks = stock_data.get("break_count", 0)
        if breaks == 0:
            adjustments["break_count"] = 0.03
        elif breaks == 1:
            adjustments["break_count"] = -0.02
        else:
            adjustments["break_count"] = -0.10

        # 换手率(适中最好)
        turnover = stock_data.get("turnover", 0)
        if 3 <= turnover <= 12:
            adjustments["turnover"] = 0.03
        elif turnover > 20:
            adjustments["turnover"] = -0.05

        # 板块强度
        sector_strength = stock_data.get("sector_strength", 0.5)
        if sector_strength > 0.7:
            adjustments["sector_strength"] = 0.05
        elif sector_strength < 0.3:
            adjustments["sector_strength"] = -0.05

        # 情绪周期
        sentiment = stock_data.get("sentiment_cycle", "recovery")
        sentiment_adj = {"climax": 0.08, "divergence": 0.02, "recovery": 0, "freezing": -0.10}
        adjustments["sentiment"] = sentiment_adj.get(sentiment, 0)

        # 计算最终概率
        prob = base_prob + sum(adjustments.values())
        prob = max(0.01, min(0.95, prob))

        # 置信度
        if abs(sum(adjustments.values())) >= 0.10:
            confidence = "high"
        elif abs(sum(adjustments.values())) >= 0.05:
            confidence = "medium"
        else:
            confidence = "low"

        return PromotionResult(
            code=code,
            name=name,
            current_days=current_days,
            next_days=current_days + 1,
            promotion_prob=round(prob, 3),
            confidence=confidence,
            factors=adjustments,
        )

    def get_board_height(self, limit_ups: list[dict]) -> dict:
        """获取市场连板高度

        Returns:
            {height: 最高连板数, leader: 领头股, ladder_summary: 梯队摘要}
        """
        ladders = self.build_ladder(limit_ups)

        if not ladders:
            return {"height": 0, "leader": None, "ladder_summary": []}

        height = ladders[0].consecutive_days
        leader = ladders[0].stocks[0] if ladders[0].stocks else None

        return {
            "height": height,
            "leader": {
                "code": leader["code"],
                "name": leader.get("name", ""),
                "days": height,
            } if leader else None,
            "ladder_summary": [
                {
                    "days": l.consecutive_days,
                    "count": l.count,
                    "seal_rate": l.seal_rate,
                    "top_stocks": [
                        {"code": s["code"], "name": s.get("name", "")}
                        for s in l.stocks[:3]
                    ],
                }
                for l in ladders
            ],
        }
