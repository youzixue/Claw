"""十倍潜力股模型 — 5维评分 v3.3 (预测增强版)

5维度:
1. 市值起点(25%) — 小市值弹性大, <100亿最佳
2. 增速动力(25%) — 净利润增速>30%高增长
3. 估值合理性(20%) — PE/PB合理性
4. 赛道爆发力(15%) — 板块Lifecycle持续活跃
5. 资金认可度(15%) — 主力连续净流入

总分0-100, 等级: T/A/B/C (T=十倍潜力)

v3.3 预测增强 (P0 动量降温):
- P0: 涨停/大阳线股加入次日回调概率惩罚(首板涨停总分×0.85, 涨幅>7%总分×0.90)
- P0: 连板≥3的续强股回调概率低，惩罚减半(总分×0.95)

v3.2 修复:
- 负PE=亏损，不应与数据缺失等同
- PB 5-8偏贵但不极端
- 跌停判定完全依赖is_limit_down参数

v3.1 修复清单:
- P0: 跌停股风控 — 当日跌停直接降级, 总分×0.4, 等级最多B
- P0: 暴跌股风控 — 跌幅>7%(非跌停)总分×0.6, 等级最多A
- P1: 大跌股风控 — 跌幅>5%总分×0.8
- 新增 change_pct / is_limit_down 参数

v3.0 修复清单:
- P1: 候选池扩大(由tenbagger.py处理)
- P2: PE 40-60/60-80区分
- P2: 资金维度按市值比例调整(小市值>5亿几乎不可能)
- P2: 板块持续性>=5天=90分→80分(5天可能只是短期炒作)
- P2: 赛道维度增加板块资金趋势权重
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


@dataclass
class TenbaggerResult:
    """十倍潜力评分结果"""
    code: str
    name: str
    total_score: float              # 总分 0-100
    level: str                      # T/A/B/C
    dimensions: dict = field(default_factory=dict)  # 各维度得分
    highlights: list = field(default_factory=list)   # 亮点
    risks: list = field(default_factory=list)        # 风险
    suggestion: str = ""


class TenbaggerModel:
    """十倍潜力股评分模型 v3.3 — 基础评分+动量降温+学习闭环"""

    # [v3.2 P3] 学习闭环自适应调整量(由 scheduler 盘后写入)
    learning_adjustment: float = 0.0

    WEIGHTS = {
        "market_cap": 0.25,
        "growth": 0.25,
        "valuation": 0.20,
        "sector": 0.15,
        "capital": 0.15,
    }

    LEVELS = [
        (80, "T"),   # 十倍潜力
        (60, "A"),   # 高潜力
        (40, "B"),   # 中等
        (0, "C"),    # 低潜力
    ]

    def score(
        self,
        code: str,
        name: str,
        # 市值
        circ_market_cap: float = 0,       # 流通市值(亿)
        # 增长
        net_profit_growth: float = 0,     # 净利润增速%
        # 估值
        pe_ttm: float = 0,               # PE(TTM)
        pb: float = 0,                   # PB
        # 赛道
        sector_consecutive_days: int = 0, # 板块持续活跃天数
        sector_fund_flow: float = 0,      # 板块资金净流入(亿)
        # 资金
        main_net_inflow: float = 0,       # 当日主力净流入(元)
        main_net_inflow_pct: float = 0,   # 当日主力净流入-净占比%
        super_net_inflow: float = 0,      # 当日超大单净流入(元)
        super_net_inflow_pct: float = 0,  # 当日超大单净流入占比%
        big_net_inflow: float = 0,        # 当日大单净流入(元)
        big_net_inflow_pct: float = 0,    # 当日大单净流入占比%
        main_net_inflow_5d: float = 0,    # 近5日主力净流入合计(元)
        # [v3.1新增] 极端行情风控
        change_pct: float = 0,            # 当日涨跌幅%
        is_limit_down: bool = False,      # 当日是否跌停
        is_20pct_board: bool = False,     # 是否20%涨跌停板块(创业板/科创板)
        # [v3.3 P0] 动量降温所需
        is_limit_up: bool = False,        # 当日是否涨停
        consecutive_days: int = 0,        # 连板天数
    ) -> TenbaggerResult:
        """计算十倍潜力评分"""
        # === NaN穿透防御 ===
        circ_market_cap = _safe_float(circ_market_cap, 0)
        net_profit_growth = _safe_float(net_profit_growth, 0)
        pe_ttm = _safe_float(pe_ttm, 0)
        pb = _safe_float(pb, 0)
        sector_consecutive_days = int(_safe_float(sector_consecutive_days, 0))
        sector_fund_flow = _safe_float(sector_fund_flow, 0)
        main_net_inflow = _safe_float(main_net_inflow, 0)
        main_net_inflow_pct = _safe_float(main_net_inflow_pct, 0)
        super_net_inflow = _safe_float(super_net_inflow, 0)
        super_net_inflow_pct = _safe_float(super_net_inflow_pct, 0)
        big_net_inflow = _safe_float(big_net_inflow, 0)
        big_net_inflow_pct = _safe_float(big_net_inflow_pct, 0)
        main_net_inflow_5d = _safe_float(main_net_inflow_5d, 0)
        change_pct = _safe_float(change_pct, 0)

        dimensions = {}
        highlights = []
        risks = []

        # === 1. 市值起点(25%) ===
        # 注意: circ_market_cap DB中存的单位是亿(入库时已除1e8)
        cap_billion = circ_market_cap if circ_market_cap > 0 else 0
        if cap_billion <= 0:
            cap_score = 30  # 无数据, 中性
        elif cap_billion < 20:
            cap_score = 80   # 微盘股流动性极差，机构无法建仓
            risks.append(f"微盘股({cap_billion:.0f}亿), 流动性风险")
        elif cap_billion < 50:
            cap_score = 95
            highlights.append(f"小市值({cap_billion:.0f}亿), 弹性极大")
        elif cap_billion < 100:
            cap_score = 80
            highlights.append(f"中小市值({cap_billion:.0f}亿)")
        elif cap_billion < 300:
            cap_score = 55
        elif cap_billion < 500:
            cap_score = 35
            risks.append(f"大市值({cap_billion:.0f}亿), 弹性有限")
        else:
            cap_score = 15
            risks.append(f"超大市值({cap_billion:.0f}亿), 十倍难度极高")
        dimensions["market_cap"] = cap_score

        # === 2. 增速动力(25%) ===
        if net_profit_growth > 200:
            growth_score = 70  # 超高增速可能是低基数效应，降级
            risks.append(f"净利润增速{net_profit_growth:.0f}%, 疑似低基数效应")
        elif net_profit_growth > 100:
            growth_score = 95
            highlights.append(f"净利润增速{net_profit_growth:.0f}%, 超高增长")
        elif net_profit_growth > 50:
            growth_score = 80
            highlights.append(f"净利润增速{net_profit_growth:.0f}%, 高增长")
        elif net_profit_growth > 30:
            growth_score = 65
        elif net_profit_growth > 10:
            growth_score = 45
        elif net_profit_growth > 0:
            growth_score = 30
        elif net_profit_growth < -20:
            growth_score = 5
            risks.append(f"净利润下滑{abs(net_profit_growth):.0f}%")
        else:
            growth_score = 20
        dimensions["growth"] = growth_score

        # === 3. 估值合理性(20%) — v3.0: PE区间细化 ===
        val_score = 50  # 基准
        if pe_ttm > 0:
            if pe_ttm < 20:
                val_score += 25       # → 75
                highlights.append(f"PE={pe_ttm:.0f}, 低估值")
            elif pe_ttm < 40:
                val_score += 15       # → 65
            elif pe_ttm < 60:
                val_score += 5        # → 55 [v3.0新增] 合理成长溢价
            elif pe_ttm < 80:
                val_score -= 5        # → 45 [v3.0修复] 开始偏贵
            elif pe_ttm < 150:
                val_score -= 15       # → 35
                risks.append(f"PE={pe_ttm:.0f}偏高")
            else:
                val_score -= 25       # → 25
                risks.append(f"PE={pe_ttm:.0f}极高")

        if pb > 0:
            if pb < 2:
                val_score += 10
            elif pb < 5:
                val_score += 5
            elif pb >= 5 and pb <= 8:
                val_score -= 3   # [v3.2] PB 5-8偏贵但不极端
            elif pb > 10:       # [v3.2修复] 先判断>10再判断>8
                val_score -= 15
                risks.append(f"PB={pb:.1f}极高")
            elif pb > 8:
                val_score -= 10
                risks.append(f"PB={pb:.1f}偏高")

        # [v3.2修复] 负PE=亏损，不应与数据缺失等同
        if pe_ttm < 0:
            val_score -= 15
            risks.append(f"PE为负({pe_ttm:.0f}), 公司当前亏损")

        val_score = max(0, min(100, val_score))
        dimensions["valuation"] = val_score

        # === 4. 赛道爆发力(15%) — v3.0: 降低5天=90→80, 增加资金趋势 ===
        if sector_consecutive_days >= 7:     # [v3.0] ≥7天才算强赛道
            sector_score = 90
            highlights.append(f"板块连续{sector_consecutive_days}天活跃")
        elif sector_consecutive_days >= 5:
            sector_score = 80                # [v3.0修复] 从90→80
            highlights.append(f"板块连续{sector_consecutive_days}天活跃")
        elif sector_consecutive_days >= 3:
            sector_score = 65
        elif sector_consecutive_days >= 1:
            sector_score = 45
        elif sector_fund_flow > 0:
            sector_score = 35
        else:
            sector_score = 15

        # [v3.0新增] 板块资金趋势加成
        if sector_fund_flow > 5:  # 板块净流入>5亿
            sector_score = min(100, sector_score + 10)
            highlights.append(f"板块资金净流入{sector_fund_flow:.1f}亿")
        elif sector_fund_flow > 1:
            sector_score = min(100, sector_score + 5)

        dimensions["sector"] = min(100, sector_score)

        # === 5. 资金认可度(15%) — v3.0: 按市值比例调整 ===
        inflow_5d_billion = main_net_inflow_5d / 1e8 if main_net_inflow_5d != 0 else 0
        inflow_today_billion = main_net_inflow / 1e8 if main_net_inflow != 0 else 0

        # [v3.0修复] 按市值比例调整阈值(小市值5亿几乎不可能)
        if cap_billion > 0 and inflow_5d_billion != 0:
            inflow_ratio = abs(inflow_5d_billion) / cap_billion  # 5日净流入/流通市值
            if inflow_5d_billion > 0:
                if inflow_ratio > 0.05:      # 5%以上
                    capital_score = 90
                    highlights.append(f"5日东财主力资金净额占市值{inflow_ratio*100:.1f}%")
                elif inflow_ratio > 0.02:    # 2%以上
                    capital_score = 70
                elif inflow_ratio > 0.005:   # 0.5%以上
                    capital_score = 50
                else:
                    capital_score = 35
            else:
                if inflow_ratio > 0.03:
                    capital_score = 10
                    risks.append(f"5日东财主力资金净额占市值{inflow_ratio*100:.1f}%")
                elif inflow_ratio > 0.01:
                    capital_score = 20
                else:
                    capital_score = 30
        else:
            # 回退到绝对值判断
            if inflow_5d_billion > 5:
                capital_score = 90
                highlights.append(f"5日东财主力资金净额{inflow_5d_billion:.1f}亿")
            elif inflow_5d_billion > 2:
                capital_score = 70
            elif inflow_5d_billion > 0.5:
                capital_score = 50
            elif inflow_5d_billion < -3:
                capital_score = 10
                risks.append(f"5日东财主力资金净额-{abs(inflow_5d_billion):.1f}亿")
            else:
                capital_score = 20  # 无明确资金信号，中性偏低

        # 当日流入加成
        if inflow_today_billion > 1:
            capital_score = min(100, capital_score + 10)
        elif inflow_today_billion > 0.3:
            capital_score = min(100, capital_score + 5)
        elif inflow_today_billion < -1:
            capital_score = max(0, capital_score - 8)
        elif inflow_today_billion < -0.3:
            capital_score = max(0, capital_score - 4)

        # 东方财富主力净流入占比(优先使用官方主口径字段)
        if main_net_inflow_pct > 10:
            capital_score = min(100, capital_score + 10)
            highlights.append(f"东财主力净占比{main_net_inflow_pct:.1f}%")
        elif main_net_inflow_pct > 5:
            capital_score = min(100, capital_score + 6)
        elif main_net_inflow_pct > 2:
            capital_score = min(100, capital_score + 3)
        elif main_net_inflow_pct < -10:
            capital_score = max(0, capital_score - 10)
            risks.append(f"东财主力净占比{main_net_inflow_pct:.1f}%")
        elif main_net_inflow_pct < -5:
            capital_score = max(0, capital_score - 6)
        elif main_net_inflow_pct < -2:
            capital_score = max(0, capital_score - 3)

        # 超大单/大单方向: 作为主力真实性确认
        if main_net_inflow > 0 and super_net_inflow > 0 and big_net_inflow > 0:
            capital_score = min(100, capital_score + 6)
            if super_net_inflow_pct > 1.5 or big_net_inflow_pct > 2.0:
                capital_score = min(100, capital_score + 4)
            highlights.append("超大/大单同步流入")
        elif main_net_inflow < 0 and super_net_inflow < 0 and big_net_inflow < 0:
            capital_score = max(0, capital_score - 6)
            if super_net_inflow_pct < -1.5 or big_net_inflow_pct < -2.0:
                capital_score = max(0, capital_score - 4)
            risks.append("超大/大单同步流出")

        dimensions["capital"] = max(0, min(100, capital_score))

        # === 加权总分 ===
        total = sum(
            dimensions.get(k, 0) * w
            for k, w in self.WEIGHTS.items()
        )
        total = round(min(100, max(0, total)), 1)

        # === [v3.3 P0] 动量降温: 涨停/大阳线股加入次日回调概率惩罚 ===
        # A股实证: 首板涨停次日平均涨幅~0.5%, 回调概率~60%
        # 连板≥3的续强股回调概率低，惩罚减半
        pullback_penalty = 1.0
        pullback_reason = ""
        if is_limit_up and consecutive_days < 3:
            pullback_penalty = 0.85
            pullback_reason = "首板涨停次日回调概率较高"
        elif is_limit_up and consecutive_days >= 3:
            pullback_penalty = 0.95
            pullback_reason = f"{consecutive_days}连板续强，回调风险降低"
        elif change_pct > 7 and not is_limit_up:
            pullback_penalty = 0.90
            pullback_reason = f"大阳线{change_pct:.1f}%未涨停，次日回调概率较高"
        # [v3.2 P3] 学习闭环自适应调整
        if self.learning_adjustment != 0.0:
            pullback_penalty += self.learning_adjustment
            pullback_penalty = max(0.5, min(1.0, pullback_penalty))
        if pullback_penalty < 1.0:
            total = round(total * pullback_penalty, 1)
            if pullback_reason:
                risks.append(pullback_reason)

        # === [v3.1新增] 极端行情风控 — 区分主板10%/创业板科创板20%涨跌停 ===
        # 主板: 跌停(-10%), 暴跌(-7%), 大跌(-5%)
        # 20%板: 跌停(-20%), 暴跌(-14%), 大跌(-10%)
        crash_threshold = -14 if is_20pct_board else -7   # 暴跌阈值
        drop_threshold = -10 if is_20pct_board else -5    # 大跌阈值

        # 跌停股: 总分直接×0.4, 等级最多B
        if is_limit_down:  # [v3.2修复] 移除硬编码-9.5%, 完全依赖调用方传入的is_limit_down
            total = round(total * 0.4, 1)
            risks.insert(0, f"⚠️ 当日跌停({change_pct:.1f}%), 严重风险信号")
            max_level = "B"
        # 暴跌(主板>7%/20%板>14%但未跌停): 总分×0.6, 等级最多A
        elif change_pct <= crash_threshold:
            total = round(total * 0.6, 1)
            risks.insert(0, f"⚠️ 当日暴跌({change_pct:.1f}%), 短期风险极大")
            max_level = "A"
        # 大跌(主板>5%/20%板>10%): 总分×0.8
        elif change_pct <= drop_threshold:
            total = round(total * 0.8, 1)
            risks.insert(0, f"当日大跌({change_pct:.1f}%), 需警惕")
            max_level = None
        else:
            max_level = None

        # 等级(含风控上限)
        level = "C"
        level_order = {"T": 0, "A": 1, "B": 2, "C": 3}
        for threshold, lvl in self.LEVELS:
            if total >= threshold:
                level = lvl
                break
        # [v3.1] 风控降级: 跌停最多B, 暴跌最多A
        if max_level and level_order.get(level, 3) < level_order.get(max_level, 3):
            level = max_level

        # 建议
        if level == "T":
            suggestion = "十倍潜力标的, 长期重点关注"
        elif level == "A":
            suggestion = "高潜力标的, 可适度关注"
        elif level == "B":
            suggestion = "中等潜力, 需观察催化剂"
        else:
            suggestion = "当前潜力不足, 暂不建议"

        return TenbaggerResult(
            code=code,
            name=name,
            total_score=total,
            level=level,
            dimensions=dimensions,
            highlights=highlights[:5],
            risks=risks[:5],
            suggestion=suggestion,
        )


# 全局单例
tenbagger_model = TenbaggerModel()
