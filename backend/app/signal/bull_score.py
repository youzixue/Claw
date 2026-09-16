"""牛股综合评分模型 — 7维度加权 (v3.2 预测增强版)

维度 (对标最佳实践v2.0 + V2.2八因子融合):
1. 动量强度(20%) — 涨幅+连板+量比+5日动量+量价关系
2. 资金面(25%) — 主力净流入+5日累计+资金趋势+主力占比
3. 技术形态(20%) — MA趋势+BOLL+突破信号+MACD/RSI/KDJ
4. 估值安全(10%) — PE/PB合理性
5. 基本面(10%) — 净利润增速
6. 规模适配(5%) — 市值区间适配度
7. 活跃度(10%) — 量比5级+换手率5级+量价关系+振幅 [V2.2融合: 5级分类体系]

v3.2 预测增强 (P0+P1+P2):
- P0: 动量降温 — 涨停/大阳线股加入次日回调概率惩罚(涨停×0.75, 涨幅>7%×0.85)
- P0: 连板续强豁免 — 连板≥3的涨停股回调概率低，惩罚减半
- P1: 市场环境因子 — 根据涨跌比/涨停数判断趋势日/轮动日/普跌日，动态调整动量权重
- P2: 放量滞涨检测 — 量比>2+换手>10%+涨幅<2% → 疑似出货，动量降级
- P2: 高位放量检测 — 近20日高位+量比>2+换手>15% → 高位兑现风险

V2.2融合增强 (v3.1):
- 活跃度: 量比5级(极度缩量/缩量/正常/放量/巨量) + 换手率5级(死寂/低换手/正常/活跃/高换手)
- 活跃度: 量价关系5态(放量上涨/缩量上涨/放量下跌/缩量下跌/缩量盘整)
- 动量: 量价关系信号影响(放量上涨加分/缩量上涨微加/放量下跌减分)
- 新增: chip_signal子维度输出(0-10)，供前端信号评分Tab展示
- 新增: volume_ratio_level / turnover_level / price_volume_relation 输出

v3.0 修复清单:
- P0: 动量-3%~0%得分漏洞 → 补全梯度
- P1: 添加MACD金叉/死叉、RSI超买超卖、KDJ金叉
- P1: 新高判断用high而非price
- P1: 技术维度第1轮基准值从40→30(降低无数据时的虚高分)
- P2: 缩量涨停区分一字板(一字板不降级)
- P2: PE 40-60/60-80区分
- P2: BOLL突破加分+10→+15
- P2: 高换手低涨幅风险提示
- P2: 资金维度添加主力占比
"""

from dataclasses import dataclass, field
from typing import Optional
from loguru import logger
import math


def _safe_float(val, default: float = 0.0) -> float:
    """NaN/None/inf 安全转换 — 防止NaN穿透到评分逻辑"""
    if val is None:
        return default
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _safe_optional_float(val) -> float | None:
    """Keep a missing technical indicator missing instead of treating it as RSI=50."""
    if val is None:
        return None
    try:
        number = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


@dataclass
class BullScoreResult:
    """牛股综合评分结果"""
    code: str
    name: str
    total_score: float              # 总分 0-100
    level: str                      # S/A/B/C/D
    dimensions: dict = field(default_factory=dict)  # 各维度得分
    top_signals: list = field(default_factory=list)  # 核心信号
    risk_warnings: list = field(default_factory=list)  # 风险提示
    suggestion: str = ""            # 操作建议
    # V2.2融合新增字段
    volume_ratio_level: str = ""    # 量比等级: 极度缩量/缩量/正常/放量/巨量
    turnover_level: str = ""        # 换手率等级: 死寂/低换手/正常/活跃/高换手
    price_volume_relation: str = "" # 量价关系: 放量上涨/缩量上涨/放量下跌/缩量下跌/缩量盘整
    chip_signal: float = 0.0        # 筹码信号强度 0-10


class BullScoreModel:
    """牛股综合评分模型 v3.2 — 7维度+V2.2八因子融合+动量降温+市场环境+量价背离+学习闭环"""

    # [v3.2 P3] 学习闭环自适应调整量(由 scheduler 盘后写入)
    learning_adjustment: float = 0.0

    # 维度权重
    WEIGHTS = {
        "momentum": 0.20,     # 动量强度
        "capital": 0.25,      # 资金面
        "technical": 0.20,    # 技术形态
        "valuation": 0.10,    # 估值安全
        "fundamental": 0.10,  # 基本面
        "scale": 0.05,        # 规模适配
        "activity": 0.10,     # 活跃度
    }

    # 等级划分
    LEVELS = [
        (90, "S"),  # 极强
        (75, "A"),  # 强
        (60, "B"),  # 中
        (40, "C"),  # 弱
        (0, "D"),   # 极弱
    ]

    def score_from_spot(
        self,
        code: str,
        name: str,
        # StockSpot字段
        change_pct: float = 0,
        volume_ratio: float = 1.0,
        turnover: float = 0,
        amplitude: float = 0,
        main_net_inflow: float = 0,
        main_net_inflow_pct: float = 0,
        super_net_inflow: float = 0,
        super_net_inflow_pct: float = 0,
        big_net_inflow: float = 0,
        big_net_inflow_pct: float = 0,
        circ_market_cap: float = 0,
        pe_ttm: float = 0,
        pb: float = 0,
        net_profit_growth: float = 0,
        price: float = 0,
        open: float = 0,
        high: float = 0,
        low: float = 0,
        limit_up: float = 0,
        amount: float = 0,
        # K线衍生字段
        ma5: float = 0,
        ma10: float = 0,
        ma20: float = 0,
        ma60: float = 0,
        boll_upper: float = 0,
        boll_mid: float = 0,
        boll_lower: float = 0,
        high_20d: float = 0,
        high_60d: float = 0,
        # MACD/RSI/KDJ
        macd_signal: str = "",
        rsi14: float | None = None,
        kdj_signal: str = "",
        # 5日资金
        main_net_inflow_5d: float = 0,
        # 5日动量
        change_pct_5d: float = 0,
        # 连板
        consecutive_days: int = 0,
        # 涨停标记
        is_limit_up: bool = False,
        is_one_word: bool = False,
        # V2.2筹码信号
        chip_signal: float = 0.0,
        # [v3.2] 市场环境因子
        market_regime: str = "unknown",
    ) -> BullScoreResult:
        """从StockSpot字段直接计算7维评分 — 无需6次DB查询"""

        # === NaN穿透防御: 统一转换所有数值字段 ===
        change_pct = _safe_float(change_pct, 0)
        volume_ratio = _safe_float(volume_ratio, 1.0)
        turnover = _safe_float(turnover, 0)
        amplitude = _safe_float(amplitude, 0)
        main_net_inflow = _safe_float(main_net_inflow, 0)
        main_net_inflow_pct = _safe_float(main_net_inflow_pct, 0)
        super_net_inflow = _safe_float(super_net_inflow, 0)
        super_net_inflow_pct = _safe_float(super_net_inflow_pct, 0)
        big_net_inflow = _safe_float(big_net_inflow, 0)
        big_net_inflow_pct = _safe_float(big_net_inflow_pct, 0)
        circ_market_cap = _safe_float(circ_market_cap, 0)
        pe_ttm = _safe_float(pe_ttm, 0)
        pb = _safe_float(pb, 0)
        net_profit_growth = _safe_float(net_profit_growth, 0)
        price = _safe_float(price, 0)
        open = _safe_float(open, 0)
        high = _safe_float(high, 0)
        low = _safe_float(low, 0)
        limit_up = _safe_float(limit_up, 0)
        amount = _safe_float(amount, 0)
        ma5 = _safe_float(ma5, 0)
        ma10 = _safe_float(ma10, 0)
        ma20 = _safe_float(ma20, 0)
        ma60 = _safe_float(ma60, 0)
        boll_upper = _safe_float(boll_upper, 0)
        boll_mid = _safe_float(boll_mid, 0)
        boll_lower = _safe_float(boll_lower, 0)
        high_20d = _safe_float(high_20d, 0)
        high_60d = _safe_float(high_60d, 0)
        rsi14 = _safe_optional_float(rsi14)
        main_net_inflow_5d = _safe_float(main_net_inflow_5d, 0)
        change_pct_5d = _safe_float(change_pct_5d, 0)
        chip_signal = _safe_float(chip_signal, 0)

        dimensions = {}
        top_signals = []
        risk_warnings = []

        # ===== V2.2融合: 量比5级 / 换手率5级 / 量价关系 =====
        vr_level = self._classify_volume_ratio(volume_ratio)
        tr_level = self._classify_turnover(turnover)
        pv_relation = self._classify_price_volume(change_pct, volume_ratio)

        # === 1. 动量强度 (20%) — v3.1融合: 量价关系加成 ===
        momentum_score = 20  # 基准(降低，避免无数据时虚高)

        # 当日涨幅 — 区分涨停(含20%涨跌停板块)
        if is_limit_up:  # 统一用is_limit_up判定(主板10%/创业板20%/科创板20%)
            momentum_score = 95
            top_signals.append(f"涨停{'(一字板)' if is_one_word else ''}")
        elif change_pct > 7:
            momentum_score = 85
            top_signals.append(f"涨幅{change_pct:.1f}%")
        elif change_pct > 5:
            momentum_score = 75
            top_signals.append(f"涨幅{change_pct:.1f}%")
        elif change_pct > 3:
            momentum_score = 60
            top_signals.append(f"涨幅{change_pct:.1f}%")
        elif change_pct > 1:
            momentum_score = 45
        elif change_pct > 0:
            momentum_score = 35
        elif change_pct > -0.5:
            momentum_score = 30    # 微跌，接近平盘
        elif change_pct > -1:
            momentum_score = 25
        elif change_pct > -3:
            momentum_score = 15
        elif change_pct > -5:
            momentum_score = 10
            risk_warnings.append(f"跌幅{change_pct:.1f}%")
        else:
            momentum_score = 5
            risk_warnings.append(f"暴跌{change_pct:.1f}%")

        # 连板加成
        if consecutive_days >= 3:
            momentum_score = min(100, momentum_score + 15)
            top_signals.append(f"{consecutive_days}连板")
        elif consecutive_days >= 2:
            momentum_score = min(100, momentum_score + 8)
            top_signals.append(f"{consecutive_days}连板")

        # 5日动量加成 — 连板股5日涨幅>20%是必然的，加成减半避免双重叠加
        if change_pct_5d > 20:
            bonus = 5 if consecutive_days >= 3 else 10  # 连板≥3时减半
            momentum_score = min(100, momentum_score + bonus)
            top_signals.append(f"5日+{change_pct_5d:.0f}%")
        elif change_pct_5d > 10:
            momentum_score = min(100, momentum_score + 5)

        # 量比加成
        if volume_ratio > 3:
            momentum_score = min(100, momentum_score + 5)
        elif volume_ratio > 2:
            momentum_score = min(100, momentum_score + 3)

        # 缩量涨停降级 — 区分一字板(一字板缩量是好事，不降级)
        if is_limit_up and volume_ratio < 0.8 and not is_one_word:
            momentum_score -= 15
            risk_warnings.append("缩量涨停⚠️")

        # [v3.1融合] 量价关系信号影响动量
        if pv_relation == "放量上涨":
            momentum_score = min(100, momentum_score + 3)  # 量价齐升，动量可信
        elif pv_relation == "缩量上涨" and change_pct > 3:
            momentum_score = max(0, momentum_score - 2)  # 涨幅大但缩量，存疑
        elif pv_relation == "放量下跌":
            momentum_score = max(0, momentum_score - 5)  # 放量下跌，恐慌
            if change_pct < -3:
                top_signals.append("放量下跌⚠️")

        dimensions["momentum"] = max(0, min(100, momentum_score))

        # [v3.2 P0] 动量降温: 涨停/大阳线股加入次日回调概率惩罚
        # A股实证: 涨停股次日高开低走概率~60%, 非连板涨停次日平均涨幅仅~0.5%
        # 连板≥3的续强股回调概率低，惩罚减半
        pullback_penalty = 1.0
        pullback_reason = ""
        if is_limit_up and consecutive_days < 3:
            pullback_penalty = 0.75
            pullback_reason = f"首板涨停次日回调概率较高"
        elif is_limit_up and consecutive_days >= 3:
            pullback_penalty = 0.90
            pullback_reason = f"{consecutive_days}连板续强，回调风险降低"
        elif change_pct > 7 and not is_limit_up:
            pullback_penalty = 0.85
            pullback_reason = f"大阳线{change_pct:.1f}%未涨停，次日回调概率较高"
        # [v3.2 P3] 学习闭环自适应调整
        if self.learning_adjustment != 0.0:
            pullback_penalty += self.learning_adjustment
            pullback_penalty = max(0.5, min(1.0, pullback_penalty))
        if pullback_penalty < 1.0:
            dimensions["momentum"] = round(dimensions["momentum"] * pullback_penalty, 1)
            if pullback_reason:
                risk_warnings.append(pullback_reason)

        # === 2. 资金面 (25%) ===
        # 低换手+缩量=流动性枯竭，降低基准避免虚高
        if turnover < 1 and volume_ratio < 0.8:
            capital_score = 15  # 流动性枯竭≈无资金关注
        else:
            capital_score = 25  # 正常基准
        inflow_today_billion = main_net_inflow / 1e8
        inflow_5d_billion = main_net_inflow_5d / 1e8

        # 当日主力净流入
        if inflow_today_billion > 5:
            capital_score = 90
            top_signals.append(f"东财主力资金净额{inflow_today_billion:.1f}亿")
        elif inflow_today_billion > 2:
            capital_score = 75
            top_signals.append(f"东财主力资金净额{inflow_today_billion:.1f}亿")
        elif inflow_today_billion > 0.5:
            capital_score = 60
        elif inflow_today_billion > 0:
            capital_score = 45
        elif inflow_today_billion < -3:
            capital_score = 10
            risk_warnings.append(f"东财主力资金净额{inflow_today_billion:.1f}亿")
        elif inflow_today_billion < -0.5:
            capital_score = 20

        # 5日累计加成/降级
        if inflow_5d_billion > 10:
            capital_score = min(100, capital_score + 10)
        elif inflow_5d_billion > 3:
            capital_score = min(100, capital_score + 5)
        elif inflow_5d_billion < -5:
            capital_score = max(0, capital_score - 15)
        elif inflow_5d_billion < -2:
            capital_score = max(0, capital_score - 8)

        # 只消费供应商主力占比。真实0不能被当缺失重算；也不能把另一
        # 报价时点的成交额拼成资金占比(尤其不能用abs抹掉净流出符号)。
        inflow_pct = main_net_inflow_pct

        if main_net_inflow > 0:
            if inflow_pct > 15:
                capital_score = min(100, capital_score + 8)
                top_signals.append(f"主力占比{inflow_pct:.0f}%")
            elif inflow_pct > 10:
                capital_score = min(100, capital_score + 5)
            elif inflow_pct > 5:
                capital_score = min(100, capital_score + 2)
        elif main_net_inflow < 0:
            if inflow_pct < -15:
                capital_score = max(0, capital_score - 8)
                risk_warnings.append(f"主力占比{inflow_pct:.0f}%")
            elif inflow_pct < -10:
                capital_score = max(0, capital_score - 5)
            elif inflow_pct < -5:
                capital_score = max(0, capital_score - 2)

        # 超大单 / 大单方向确认(东方财富口径)
        if main_net_inflow > 0 and super_net_inflow > 0 and big_net_inflow > 0:
            capital_score = min(100, capital_score + 5)
            if super_net_inflow_pct > 1.5 or big_net_inflow_pct > 2.0:
                capital_score = min(100, capital_score + 3)
            top_signals.append("超大/大单同步流入")
        elif main_net_inflow < 0 and super_net_inflow < 0 and big_net_inflow < 0:
            capital_score = max(0, capital_score - 5)
            if super_net_inflow_pct < -1.5 or big_net_inflow_pct < -2.0:
                capital_score = max(0, capital_score - 3)
            risk_warnings.append("超大/大单同步流出")

        dimensions["capital"] = max(0, min(100, capital_score))

        # === 3. 技术形态 (20%) ===
        tech_score = 30  # 基准从40→30，降低无数据时的虚高

        # MA趋势
        if ma5 > 0 and ma10 > 0 and ma20 > 0:
            if price > ma5 > ma10 > ma20:
                tech_score = 85  # 多头排列
                top_signals.append("均线多头排列")
            elif price > ma5 > ma10:
                tech_score = 70
            elif price > ma20:
                tech_score = 55
            elif price < ma5 < ma10 < ma20:
                tech_score = 15  # 空头排列
                risk_warnings.append("均线空头排列")
            elif price < ma20:
                tech_score = 25
        elif ma5 > 0 and ma20 > 0:
            if price > ma5 and price > ma20:
                tech_score = 60
            elif price < ma5 and price < ma20:
                tech_score = 25

        # MA60趋势加成
        if ma60 > 0 and price > ma60:
            tech_score = min(100, tech_score + 5)

        # BOLL位置 — 突破上轨需区分真假突破+统一风险提示
        if boll_upper > 0 and boll_lower > 0 and boll_mid > 0:
            boll_width = (boll_upper - boll_lower) / boll_mid if boll_mid > 0 else 0
            if price >= boll_upper:
                # 突破上轨=价格偏离均值2σ，超买位
                # 多头排列时突破有效(+10)，非多头时假突破风险大(+5)
                is_bull_align = (ma5 > 0 and ma10 > 0 and ma20 > 0 and price > ma5 > ma10 > ma20)
                if is_bull_align:
                    tech_score = min(100, tech_score + 10)
                    top_signals.append("突破布林上轨(多头)")
                else:
                    tech_score = min(100, tech_score + 5)
                    top_signals.append("突破布林上轨")
                risk_warnings.append("布林上轨附近，注意回调")
            elif price > boll_mid:
                tech_score = min(100, tech_score + 5)
            elif price <= boll_lower:
                tech_score = max(0, tech_score - 5)

        # 突破新高 — 用high而非price判断
        if high_20d > 0 and high >= high_20d:
            tech_score = min(100, tech_score + 8)
            top_signals.append("突破20日新高")
        if high_60d > 0 and high >= high_60d:
            tech_score = min(100, tech_score + 5)
            top_signals.append("突破60日新高")

        # MACD信号
        if macd_signal == "golden_cross":
            tech_score = min(100, tech_score + 10)
            top_signals.append("MACD金叉")
        elif macd_signal == "death_cross":
            tech_score = max(0, tech_score - 10)

        # RSI信号 — 超买区减分(回调风险), 超卖区谨慎加分(仅观察)
        if rsi14 is not None and rsi14 > 0:
            if 50 <= rsi14 <= 70:
                tech_score = min(100, tech_score + 5)       # 健康多头区
            elif 70 < rsi14 <= 80:
                tech_score = max(0, tech_score - 5)         # 超买预警
                risk_warnings.append(f"RSI={rsi14:.0f}超买")
            elif 80 < rsi14 <= 85:
                tech_score = max(0, tech_score - 10)        # 严重超买
                risk_warnings.append(f"RSI={rsi14:.0f}严重超买")
            elif rsi14 > 85:
                tech_score = max(0, tech_score - 15)        # 极度超买
                risk_warnings.append(f"RSI={rsi14:.0f}极度超买⚠️")
            elif 30 <= rsi14 < 50:
                tech_score = min(100, tech_score + 2)       # 弱势区
            elif rsi14 < 25:
                tech_score = min(100, tech_score + 3)       # 深度超卖，反弹概率更高
                top_signals.append(f"RSI={rsi14:.0f}深度超卖观察")
            elif rsi14 < 30:
                tech_score = min(100, tech_score + 2)       # 超卖观察(不等于反弹)
                top_signals.append(f"RSI={rsi14:.0f}超卖观察")

        # KDJ信号
        if kdj_signal == "golden_cross":
            tech_score = min(100, tech_score + 5)
            top_signals.append("KDJ金叉")
        elif kdj_signal == "death_cross":
            tech_score = max(0, tech_score - 5)

        dimensions["technical"] = max(0, min(100, tech_score))

        # === 4. 估值安全 (10%) ===
        val_score = 50
        if pe_ttm > 0:
            if pe_ttm < 15:
                val_score = 85
            elif pe_ttm < 25:
                val_score = 70
            elif pe_ttm < 40:
                val_score = 55
            elif pe_ttm < 60:
                val_score = 40
            elif pe_ttm < 80:
                val_score = 30
            elif pe_ttm < 150:
                val_score = 20
                risk_warnings.append(f"PE={pe_ttm:.0f}偏高")
            else:
                val_score = 10
                risk_warnings.append(f"PE={pe_ttm:.0f}极高")

        if pb > 0:
            if pb < 1.5:
                val_score = min(100, val_score + 10)
            elif pb < 3:
                val_score = min(100, val_score + 5)
            elif pb > 10:
                val_score = max(0, val_score - 10)

        if pe_ttm < 0:
            val_score = 10
            risk_warnings.append("亏损股")

        dimensions["valuation"] = max(0, min(100, val_score))

        # === 5. 基本面 (10%) ===
        fund_score = 25
        if net_profit_growth > 200:
            fund_score = 70  # 疑似低基数效应，降级(与tenbagger_model一致)
            risk_warnings.append(f"增速{net_profit_growth:.0f}%疑似低基数")
        elif net_profit_growth > 100:
            fund_score = 95
            top_signals.append(f"净利润+{net_profit_growth:.0f}%")
        elif net_profit_growth > 50:
            fund_score = 80
            top_signals.append(f"净利润+{net_profit_growth:.0f}%")
        elif net_profit_growth > 30:
            fund_score = 65
        elif net_profit_growth > 10:
            fund_score = 50
        elif net_profit_growth > 0:
            fund_score = 35
        elif net_profit_growth < -30:
            fund_score = 5
            risk_warnings.append(f"净利润{net_profit_growth:.0f}%")
        elif net_profit_growth < -10:
            fund_score = 15

        dimensions["fundamental"] = max(0, min(100, fund_score))

        # === 6. 规模适配 (5%) ===
        cap_billion = circ_market_cap  # 已通过_safe_float防御
        if cap_billion <= 0:
            scale_score = 40
        elif cap_billion < 30:
            scale_score = 70   # 微盘股流动性差，有风险
        elif cap_billion < 80:
            scale_score = 85   # 最佳短线市值区间(流动性+弹性)
        elif cap_billion < 200:
            scale_score = 55
        elif cap_billion < 500:
            scale_score = 35
        else:
            scale_score = 15

        dimensions["scale"] = max(0, min(100, scale_score))

        # === 7. 活跃度 (10%) — v3.1融合: 量比5级+换手率5级+量价关系 ===
        activity_score = 20  # 基准(降低)

        # [v3.1融合] 换手率5级评分 (V2.2 TurnoverRateLevel)
        tr_score_map = {
            "死寂": -5,     # <1%, 无人气
            "低换手": 5,    # 1-3%, 偏低
            "正常": 25,     # 3-7%, 合理
            "活跃": 55,     # 7-15%, 热门
            "高换手": 70,   # >15%, 过热但活跃
        }
        activity_score += tr_score_map.get(tr_level, 0)

        # [v3.1融合] 量比5级评分 (V2.2 VolumeRatioLevel)
        vr_score_map = {
            "极度缩量": -5,  # <0.5, 流动性枯竭
            "缩量": 5,       # 0.5-0.8, 偏低
            "正常": 15,      # 0.8-1.5, 合理
            "放量": 25,      # 1.5-3.0, 关注
            "巨量": 20,      # >3.0, 高度关注但有风险
        }
        activity_score += vr_score_map.get(vr_level, 0)

        # [v3.1融合] 量价关系影响 (V2.2 PriceVolumeRelation)
        # 高换手+放量上涨可能是对倒/出货，降低加成
        pv_score_map = {
            "放量上涨": 5 if tr_level == "高换手" else 15,    # 高换手时降低(可能对倒)
            "缩量上涨": 5,     # 量价背离
            "缩量盘整": 0,     # 多空平衡
            "缩量下跌": -5,    # 卖盘衰竭
            "放量下跌": -10,   # 恐慌性下跌
        }
        activity_score += pv_score_map.get(pv_relation, 0)

        # 高换手+放量上涨风险提示
        if tr_level == "高换手" and pv_relation == "放量上涨":
            risk_warnings.append("高换手放量⚠️疑似对倒")

        # [v3.2 P2] 放量滞涨检测: 量比>2+换手>10%+涨幅<2% → 疑似出货
        if volume_ratio > 2 and turnover > 10 and 0 <= change_pct < 2:
            activity_score = max(0, activity_score - 15)
            risk_warnings.append(f"放量滞涨⚠️量比{volume_ratio:.1f}+换手{turnover:.1f}%+涨幅{change_pct:.1f}%，疑似出货")

        # [v3.2 P2] 高位放量检测: 近20日高位+量比>2+换手>15% → 高位兑现风险
        is_near_20d_high = high_20d > 0 and price > 0 and price / high_20d >= 0.95
        if is_near_20d_high and volume_ratio > 2 and turnover > 15:
            activity_score = max(0, activity_score - 20)
            risk_warnings.append(f"高位放量⚠️近20日高位+量比{volume_ratio:.1f}+换手{turnover:.1f}%，高位兑现风险")

        # 振幅加成
        if amplitude > 8:
            activity_score = min(100, activity_score + 5)

        # [v3.1融合] 特殊量比信号
        if vr_level == "巨量":
            top_signals.append(f"量比{volume_ratio:.1f}巨量")
        elif vr_level == "放量" and change_pct > 3:
            top_signals.append(f"量比{volume_ratio:.1f}放量上涨")

        # 高换手低涨幅风险
        if tr_level == "高换手" and change_pct < 1:  # [v3.2] 阈值从3%→1%
            risk_warnings.append("高换手低涨幅⚠️")

        # 死寂风险
        if tr_level == "死寂" and vr_level == "极度缩量":
            risk_warnings.append("换手+量比双低⚠️")

        dimensions["activity"] = max(0, min(100, activity_score))

        # [v3.2 P1] 市场环境因子: 根据涨跌比/涨停数动态调整维度权重
        # 趋势日: 动量有效，权重不变
        # 轮动日: 动量衰减，增加技术+资金权重
        # 普跌日: 动量大幅衰减，以资金+技术为主
        regime_weights = dict(self.WEIGHTS)
        if market_regime == "decline":
            # 普跌日: 动量权重从20%→10%, 资金从25%→30%, 技术从20%→25%
            regime_weights["momentum"] = 0.10
            regime_weights["capital"] = 0.30
            regime_weights["technical"] = 0.25
            risk_warnings.append("普跌日环境，动量信号大幅衰减")
        elif market_regime == "rotation":
            # 轮动日: 动量权重从20%→15%, 资金从25%→27%, 技术从20%→23%
            regime_weights["momentum"] = 0.15
            regime_weights["capital"] = 0.27
            regime_weights["technical"] = 0.23
        # trend/unknown: 保持默认权重

        # === 加权总分 ===
        total = sum(
            dimensions.get(k, 0) * w
            for k, w in regime_weights.items()
        )
        total = round(min(100, max(0, total)), 1)

        # 等级
        level = "D"
        for threshold, lvl in self.LEVELS:
            if total >= threshold:
                level = lvl
                break

        # 操作建议
        if level == "S":
            suggestion = "强势标的，可重点关注"
        elif level == "A":
            suggestion = "较强标的，可适度关注"
        elif level == "B":
            suggestion = "中等标的，观望为主"
        elif level == "C":
            suggestion = "偏弱标的，暂不建议"
        else:
            suggestion = "弱势标的，回避"

        return BullScoreResult(
            code=code,
            name=name,
            total_score=total,
            level=level,
            dimensions=dimensions,
            top_signals=top_signals[:5],
            risk_warnings=risk_warnings[:3],
            suggestion=suggestion,
            volume_ratio_level=vr_level,
            turnover_level=tr_level,
            price_volume_relation=pv_relation,
            chip_signal=chip_signal,
        )

    # ===== V2.2融合: 量比5级分类 =====
    @staticmethod
    def _classify_volume_ratio(vr: float) -> str:
        """量比5级分类 (V2.2 VolumeRatioLevel)"""
        if vr < 0.5:
            return "极度缩量"
        elif vr < 0.8:
            return "缩量"
        elif vr < 1.5:
            return "正常"
        elif vr < 3.0:
            return "放量"
        else:
            return "巨量"

    # ===== V2.2融合: 换手率5级分类 =====
    @staticmethod
    def _classify_turnover(turnover: float) -> str:
        """换手率5级分类 (V2.2 TurnoverRateLevel)"""
        if turnover < 1:
            return "死寂"
        elif turnover < 3:
            return "低换手"
        elif turnover < 7:
            return "正常"
        elif turnover < 15:
            return "活跃"
        else:
            return "高换手"

    # ===== V2.2融合: 量价关系5态 =====
    @staticmethod
    def _classify_price_volume(change_pct: float, volume_ratio: float) -> str:
        """量价关系5态分类 (V2.2 PriceVolumeRelation)"""
        # 盘整态: 涨跌幅极小且缩量
        if abs(change_pct) < 0.5 and volume_ratio < 1.2:
            return "缩量盘整"
        is_rising = change_pct > 0
        is_high_volume = volume_ratio > 1.2
        if is_rising and is_high_volume:
            return "放量上涨"
        elif is_rising and not is_high_volume:
            return "缩量上涨"
        elif not is_rising and is_high_volume:
            return "放量下跌"
        else:
            return "缩量下跌"

    def score(
        self,
        code: str,
        name: str,
        dragon_result=None,
        promotion_result=None,
        capital_anomalies=None,
        chip_result=None,
        breakthrough_signals=None,
        sentiment_score: float = 50,
        sentiment_cycle: str = "recovery",
        # v2.0 直接字段
        change_pct: float = 0,
        volume_ratio: float = 1.0,
        turnover: float = 0,
        amplitude: float = 0,
        main_net_inflow: float = 0,
        main_net_inflow_pct: float = 0,
        super_net_inflow: float = 0,
        super_net_inflow_pct: float = 0,
        big_net_inflow: float = 0,
        big_net_inflow_pct: float = 0,
        circ_market_cap: float = 0,
        pe_ttm: float = 0,
        pb: float = 0,
        net_profit_growth: float = 0,
        price: float = 0,
        open: float = 0,
        high: float = 0,
        low: float = 0,
        limit_up: float = 0,
        amount: float = 0,
        ma5: float = 0,
        ma10: float = 0,
        ma20: float = 0,
        ma60: float = 0,
        boll_upper: float = 0,
        boll_mid: float = 0,
        boll_lower: float = 0,
        high_20d: float = 0,
        high_60d: float = 0,
        macd_signal: str = "",
        rsi14: float | None = None,
        kdj_signal: str = "",
        main_net_inflow_5d: float = 0,
        change_pct_5d: float = 0,
        consecutive_days: int = 0,
        is_limit_up: bool = False,
        is_one_word: bool = False,
        chip_signal: float = 0.0,
    ) -> BullScoreResult:
        """兼容旧接口 — 优先用v3.1直接字段计算"""
        return self.score_from_spot(
            code=code, name=name,
            change_pct=change_pct,
            volume_ratio=volume_ratio,
            turnover=turnover,
            amplitude=amplitude,
            main_net_inflow=main_net_inflow,
            main_net_inflow_pct=main_net_inflow_pct,
            super_net_inflow=super_net_inflow,
            super_net_inflow_pct=super_net_inflow_pct,
            big_net_inflow=big_net_inflow,
            big_net_inflow_pct=big_net_inflow_pct,
            circ_market_cap=circ_market_cap,
            pe_ttm=pe_ttm,
            pb=pb,
            net_profit_growth=net_profit_growth,
            price=price, open=open, high=high, low=low,
            limit_up=limit_up,
            amount=amount,
            ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60,
            boll_upper=boll_upper, boll_mid=boll_mid, boll_lower=boll_lower,
            high_20d=high_20d, high_60d=high_60d,
            macd_signal=macd_signal, rsi14=rsi14, kdj_signal=kdj_signal,
            main_net_inflow_5d=main_net_inflow_5d,
            change_pct_5d=change_pct_5d,
            consecutive_days=consecutive_days,
            is_limit_up=is_limit_up,
            is_one_word=is_one_word,
            chip_signal=chip_signal,
        )

    def score_batch(self, stocks: list[dict]) -> list[BullScoreResult]:
        """批量评分 — 按 total_score 降序"""
        results = []
        for s in stocks:
            result = self.score_from_spot(
                code=s["code"],
                name=s.get("name", ""),
                change_pct=s.get("change_pct", 0),
                volume_ratio=s.get("volume_ratio", 1.0),
                turnover=s.get("turnover", 0),
                amplitude=s.get("amplitude", 0),
                main_net_inflow=s.get("main_net_inflow", 0),
                main_net_inflow_pct=s.get("main_net_inflow_pct", 0),
                super_net_inflow=s.get("super_net_inflow", 0),
                super_net_inflow_pct=s.get("super_net_inflow_pct", 0),
                big_net_inflow=s.get("big_net_inflow", 0),
                big_net_inflow_pct=s.get("big_net_inflow_pct", 0),
                circ_market_cap=s.get("circ_market_cap", 0),
                pe_ttm=s.get("pe_ttm", 0),
                pb=s.get("pb", 0),
                net_profit_growth=s.get("net_profit_growth", 0),
                price=s.get("price", 0),
                open=s.get("open", 0),
                high=s.get("high", 0),
                low=s.get("low", 0),
                limit_up=s.get("limit_up", 0),
                amount=s.get("amount", 0),
                ma5=s.get("ma5", 0),
                ma10=s.get("ma10", 0),
                ma20=s.get("ma20", 0),
                ma60=s.get("ma60", 0),
                boll_upper=s.get("boll_upper", 0),
                boll_mid=s.get("boll_mid", 0),
                boll_lower=s.get("boll_lower", 0),
                high_20d=s.get("high_20d", 0),
                high_60d=s.get("high_60d", 0),
                macd_signal=s.get("macd_signal", ""),
                rsi14=s.get("rsi14"),
                kdj_signal=s.get("kdj_signal", ""),
                main_net_inflow_5d=s.get("main_net_inflow_5d", 0),
                change_pct_5d=s.get("change_pct_5d", 0),
                consecutive_days=s.get("consecutive_days", 0),
                is_limit_up=s.get("is_limit_up", False),
                is_one_word=s.get("is_one_word", False),
                chip_signal=s.get("chip_signal", 0),
            )
            results.append(result)

        results.sort(key=lambda r: r.total_score, reverse=True)
        return results
