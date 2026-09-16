"""明日预案策略引擎 — 强者恒强 + 主升确认 + 趋势买法 + 激进买法

核心设计:
- 复用强势排行 BullScoreModel 的7维评分结果
- 在评分数据基础上生成: 趋势买法/激进买法/不买条件
- 严格风控: 止损/目标价/仓位/失效条件全部量化

策略体系:
🟠 强势回踩承接(龙头/核心票):
   - 条件: BullScore≥A且高分 + 趋势/新高/连板/放量/资金/板块共振多项确认
   - 买入: 次日不追竞价；首次缩量回踩开盘价/分时均价后止跌回收 → 1/4仓
   - 止损: MA5或强势止损位
   - 失效: 高开低走、跌破开盘价、板块退潮、极端过热

🟣 主升确认买法(高确定性):
   - 条件: BullScore≥A + price>MA5>MA10>MA20 + 贴近MA5/支撑 + 量能/资金确认
   - 买入: 贴MA5或支撑位低吸 → 1/2仓
   - 止损: 支撑位×0.98
   - 目标: 压力位或主升延伸目标

🔵 趋势买法(胜率优先):
   - 条件: BullScore≥B + 均线多头排列/价格站上MA20 + 5日资金净流入
   - 买入: 回踩支撑位附近 → 1/3仓
   - 止损: 支撑位×0.98
   - 目标: 压力位 / 压力位×1.03(激进)
   - 失效: 跌破支撑位 / 5日资金转向净流出

🔴 激进买法(赔率优先):
   - 条件: BullScore≥A + MACD金叉/RSI健康/KDJ金叉(至少1项) + 放量
   - 买入: 竞价高开2-5%+量比>1.5 → 1/4仓 / 盘中突破前高+量价齐升 → 1/4仓
   - 止损: 开盘价×0.97 / 突破价×0.97
   - 目标: 涨停价 / 压力位×1.05
   - 失效: 高开低走(涨幅从+3%翻绿) / 量能萎缩(量比<0.8)

⛔ 不买条件(任一触发即排除):
   - BullScore < B级
   - 跌破MA20
   - 所属板块当日跌幅>2%
   - 5日主力净流出>3亿
   - RSI>80(极度超买)
   - 缩量涨停(非一字板)
   - 明日直接买点过高: 大阳/涨停后、距支撑过远、贴近BOLL上轨或赔率不足
"""

from dataclasses import dataclass, field
from typing import Optional
from loguru import logger
import math


def _safe_float(val, default: float = 0.0) -> float:
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
    """Preserve missing indicator values instead of inventing a neutral signal."""
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
class SupportResistance:
    """支撑/压力位"""
    primary_support: float = 0.0        # 第一支撑(MA20/筹码密集区)
    secondary_support: float = 0.0      # 第二支撑(MA60/前低)
    primary_resistance: float = 0.0     # 第一压力(20日高/BOLL上轨)
    secondary_resistance: float = 0.0   # 第二压力(60日高/整数关口)
    support_source: str = ""            # 支撑位来源说明
    resistance_source: str = ""         # 压力位来源说明


@dataclass
class StrategyPlan:
    """单策略计划"""
    strategy_type: str = ""             # trend / aggressive / avoid
    strategy_label: str = ""            # 趋势买法 / 激进买法 / 不建议
    entry_condition: str = ""           # 买入条件描述
    entry_price_hint: str = ""          # 入场价位描述
    position_ratio: str = ""            # 仓位建议 "1/3仓" / "1/4仓"
    stop_loss: float = 0.0              # 止损价
    stop_loss_pct: float = 0.0          # 止损幅度%
    target_price: float = 0.0           # 目标价
    target_price_pct: float = 0.0       # 目标涨幅%
    invalidation: str = ""              # 失效条件
    risk_reward_ratio: float = 0.0      # 风险收益比
    confidence: str = ""                # 信心等级: 高/中/低


@dataclass
class NextDayPlanResult:
    """明日预案完整结果"""
    code: str = ""
    name: str = ""
    bull_level: str = ""
    bull_score: float = 0.0
    price: float = 0.0
    change_pct: float = 0.0
    # 策略
    strategies: list[StrategyPlan] = field(default_factory=list)
    # 不买原因(如果有)
    avoid_reasons: list[str] = field(default_factory=list)
    # 支撑压力
    sr: SupportResistance = field(default_factory=SupportResistance)
    # 核心信号
    top_signals: list[str] = field(default_factory=list)
    risk_warnings: list[str] = field(default_factory=list)
    # 5日资金方向
    fund_5d_direction: str = ""         # 净流入 / 净流出 / 平衡
    fund_5d_billion: float = 0.0
    # 技术状态概要
    tech_summary: str = ""              # 如 "多头排列+MACD金叉+RSI健康"
    # 板块状态
    sector_status: str = ""
    # 板块共振(新增)
    sector_resonance: str = ""          # 强共振 / 弱共振 / 独立行情 / 逆势
    sector_resonance_score: float = 0.0
    sector_resonance_name: str = ""     # 共振板块名称
    sector_resonance_change: float = 0.0
    sector_resonance_fund: float = 0.0  # 板块资金(亿)
    # 情绪面(新增)
    market_environment: str = ""        # strong / neutral / weak
    sentiment_cycle: str = ""           # 亢奋 / 分歧 / 冰点 / 修复


class NextDayPlanEngine:
    """明日预案策略引擎"""

    DIRECT_BUY_MAX_CHANGE_PCT = 6.5
    DIRECT_BUY_MAX_SUPPORT_GAP_PCT = 6.0
    DIRECT_BUY_MAX_MA5_GAP_PCT = 5.0
    DIRECT_BUY_RSI_OVERHEAT = 74.0
    DIRECT_BUY_MIN_TARGET_PCT = 5.0
    DIRECT_BUY_MIN_RR_RATIO = 1.2
    MAIN_WAVE_CONFIRM_MAX_SUPPORT_GAP_PCT = 3.0
    MAIN_WAVE_CONFIRM_MAX_MA5_GAP_PCT = 3.0
    STRONG_GET_STRONGER_MAX_SUPPORT_GAP_PCT = 12.0
    STRONG_GET_STRONGER_MAX_RSI = 85.0
    DIRECT_BUY_BLOCKER_KEYWORDS = (
        "明日不追高",
        "直接买点过高",
        "等回踩",
        "等分歧",
        "性价比不足",
        "赔率不足",
        "风险收益比",
        "贴近20日高点且当日大涨",
        "板块生命周期",
        "资金数据不足",
    )

    def generate(
        self,
        code: str,
        name: str,
        bull_level: str,
        bull_score: float,
        price: float,
        change_pct: float,
        # BullScore维度详情
        dimensions: dict = None,
        top_signals: list = None,
        risk_warnings: list = None,
        # 技术指标
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
        # V2.2指标
        volume_ratio: float = 1.0,
        volume_ratio_level: str = "",
        turnover: float = 0,
        turnover_level: str = "",
        price_volume_relation: str = "",
        # 资金
        main_net_inflow_billion: float = 0,
        fund_5d_billion: float = 0,
        current_fund_available: bool = True,
        fund_5d_complete: bool = True,
        # 连板/涨停
        consecutive_days: int = 0,
        is_limit_up: bool = False,
        is_one_word: bool = False,
        # 筹码
        chip_signal: float = 0,
        # 板块
        sector_change_pct: float = 0,
        sector_lifecycle: str = "",
        sector_lifecycle_state: str = "",
        # 板块共振(新增)
        sector_resonance: str = "",         # 强共振/弱共振/独立行情/逆势
        sector_resonance_score: float = 0,
        sector_resonance_name: str = "",
        sector_resonance_change: float = 0,
        sector_resonance_fund: float = 0,
        # 情绪面(新增)
        market_environment: str = "",       # strong/neutral/weak
        sentiment_cycle: str = "",          # 亢奋/分歧/冰点/修复
        # 其他
        circ_market_cap: float = 0,
        pe_ttm: float = 0,
        open_price: float = 0,
        high: float = 0,
        low: float = 0,
    ) -> NextDayPlanResult:
        """生成明日预案 — 核心入口"""

        # NaN防御
        price = _safe_float(price)
        change_pct = _safe_float(change_pct)
        bull_score = _safe_float(bull_score)
        ma5 = _safe_float(ma5)
        ma10 = _safe_float(ma10)
        ma20 = _safe_float(ma20)
        ma60 = _safe_float(ma60)
        boll_upper = _safe_float(boll_upper)
        boll_mid = _safe_float(boll_mid)
        boll_lower = _safe_float(boll_lower)
        high_20d = _safe_float(high_20d)
        high_60d = _safe_float(high_60d)
        rsi14 = _safe_optional_float(rsi14)
        volume_ratio = _safe_float(volume_ratio, 1.0)
        turnover = _safe_float(turnover)
        main_net_inflow_billion = _safe_float(main_net_inflow_billion)
        fund_5d_billion = _safe_float(fund_5d_billion)
        circ_market_cap = _safe_float(circ_market_cap)
        chip_signal = _safe_float(chip_signal)
        sector_change_pct = _safe_float(sector_change_pct)
        sector_lifecycle_state = str(sector_lifecycle_state or "").strip().lower()
        open_price = _safe_float(open_price)
        high = _safe_float(high)
        low = _safe_float(low)

        dimensions = dimensions or {}
        top_signals = top_signals or []
        risk_warnings = risk_warnings or []

        result = NextDayPlanResult(
            code=code,
            name=name,
            bull_level=bull_level,
            bull_score=bull_score,
            price=price,
            change_pct=change_pct,
            top_signals=top_signals[:5],
            risk_warnings=risk_warnings[:5],
            fund_5d_billion=round(fund_5d_billion, 2),
        )

        # ===== 1. 5日资金方向 =====
        if fund_5d_billion > 1:
            result.fund_5d_direction = "净流入"
        elif fund_5d_billion < -1:
            result.fund_5d_direction = "净流出"
        else:
            result.fund_5d_direction = "平衡"

        # ===== 2. 计算支撑/压力位 =====
        sr = self._calc_support_resistance(
            price=price, low=low,
            ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60,
            boll_upper=boll_upper, boll_mid=boll_mid, boll_lower=boll_lower,
            high_20d=high_20d, high_60d=high_60d,
            chip_signal=chip_signal,
        )
        result.sr = sr

        # ===== 3. 技术状态概要 =====
        result.tech_summary = self._build_tech_summary(
            ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60,
            macd_signal=macd_signal, rsi14=rsi14, kdj_signal=kdj_signal,
            boll_upper=boll_upper, price=price,
        )

        # ===== 4. 板块状态 =====
        if sector_lifecycle:
            result.sector_status = sector_lifecycle
        elif sector_change_pct < -2:
            result.sector_status = "板块弱势"
        elif sector_change_pct > 2:
            result.sector_status = "板块强势"
        else:
            result.sector_status = "板块中性"

        # ===== 4.5 板块共振 =====
        result.sector_resonance = sector_resonance
        result.sector_resonance_score = sector_resonance_score
        result.sector_resonance_name = sector_resonance_name
        result.sector_resonance_change = sector_resonance_change
        result.sector_resonance_fund = sector_resonance_fund
        # 板块共振影响板块状态描述
        if sector_resonance == "强共振":
            result.sector_status += "+强共振"
        elif sector_resonance == "逆势":
            result.sector_status += "+逆势⚠️"

        # ===== 4.6 情绪面 =====
        result.market_environment = market_environment
        result.sentiment_cycle = sentiment_cycle

        # ===== 5. 不买条件判定(任一触发即标注) =====
        avoid_reasons = []
        if bull_level in ("C", "D"):
            avoid_reasons.append(f"评分{bull_level}级过低")
        if ma20 > 0 and price < ma20:
            avoid_reasons.append("跌破MA20")
        if sector_change_pct < -2:
            avoid_reasons.append(f"板块跌{sector_change_pct:.1f}%")
        blocked_lifecycle_labels = {
            "dormant": "休眠",
            "climax": "高潮",
            "diverging": "分化",
            "declining": "退潮",
            "one_day": "一日游",
        }
        if sector_lifecycle_state in blocked_lifecycle_labels:
            avoid_reasons.append(
                f"板块生命周期{blocked_lifecycle_labels[sector_lifecycle_state]}，不支持直接买入"
            )
        if fund_5d_billion < -3:
            avoid_reasons.append(f"5日净流出{abs(fund_5d_billion):.1f}亿")
        if not fund_5d_complete:
            avoid_reasons.append("近5日资金数据不足，不支持直接买入")
        if not current_fund_available:
            avoid_reasons.append("当日资金数据不足，不支持直接买入")
        if rsi14 is not None and rsi14 > 80:
            avoid_reasons.append(f"RSI={rsi14:.0f}极度超买")
        if is_limit_up and volume_ratio < 0.8 and not is_one_word:
            avoid_reasons.append("缩量涨停(非一字板)")
        avoid_reasons.extend(self._build_direct_buy_blockers(
            price=price,
            change_pct=change_pct,
            ma5=ma5,
            sr=sr,
            rsi14=rsi14,
            boll_upper=boll_upper,
            high_20d=high_20d,
            is_limit_up=is_limit_up,
        ))

        result.avoid_reasons = avoid_reasons

        # ===== 6. 生成策略 =====
        strategies = []

        # --- 强者恒强买法: 龙头/高辨识度票只做次日承接确认 ---
        strong_get_stronger_plan = self._build_strong_get_stronger_strategy(
            price=price, bull_level=bull_level, bull_score=bull_score,
            ma5=ma5, ma10=ma10, ma20=ma20,
            sr=sr, fund_5d_billion=fund_5d_billion,
            fund_5d_direction=result.fund_5d_direction,
            rsi14=rsi14, volume_ratio=volume_ratio,
            turnover=turnover,
            change_pct=change_pct, avoid_reasons=avoid_reasons,
            high_20d=high_20d, high_60d=high_60d,
            consecutive_days=consecutive_days,
            is_limit_up=is_limit_up,
            is_one_word=is_one_word,
            price_volume_relation=price_volume_relation,
            sector_resonance=sector_resonance,
        )
        if strong_get_stronger_plan:
            strategies.append(strong_get_stronger_plan)

        # --- 主升确认买法: 动能强、多头排列明确、位置仍贴线 ---
        main_wave_confirm_plan = None
        if not strong_get_stronger_plan:
            main_wave_confirm_plan = self._build_main_wave_confirm_strategy(
                price=price, bull_level=bull_level, bull_score=bull_score,
                ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60,
                sr=sr, fund_5d_billion=fund_5d_billion,
                fund_5d_direction=result.fund_5d_direction,
                rsi14=rsi14, volume_ratio=volume_ratio,
                turnover=turnover,
                change_pct=change_pct, avoid_reasons=avoid_reasons,
                macd_signal=macd_signal, kdj_signal=kdj_signal,
            )
            if main_wave_confirm_plan:
                strategies.append(main_wave_confirm_plan)

        # 强者恒强/主升确认已经是更严格的入场，命中时不再叠加普通趋势/激进标签。
        if not strong_get_stronger_plan and not main_wave_confirm_plan:
            # --- 趋势买法 ---
            trend_plan = self._build_trend_strategy(
                price=price, bull_level=bull_level, bull_score=bull_score,
                ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60,
                sr=sr, fund_5d_billion=fund_5d_billion,
                fund_5d_direction=result.fund_5d_direction,
                rsi14=rsi14, volume_ratio=volume_ratio,
                change_pct=change_pct, avoid_reasons=avoid_reasons,
                macd_signal=macd_signal, kdj_signal=kdj_signal,
            )
            if trend_plan:
                strategies.append(trend_plan)

            # --- 激进买法 ---
            aggressive_plan = self._build_aggressive_strategy(
                price=price, bull_level=bull_level, bull_score=bull_score,
                ma5=ma5, ma10=ma10, ma20=ma20,
                sr=sr, rsi14=rsi14,
                macd_signal=macd_signal, kdj_signal=kdj_signal,
                volume_ratio=volume_ratio, volume_ratio_level=volume_ratio_level,
                turnover=turnover, turnover_level=turnover_level,
                price_volume_relation=price_volume_relation,
                fund_5d_billion=fund_5d_billion,
                change_pct=change_pct, avoid_reasons=avoid_reasons,
                consecutive_days=consecutive_days,
                high_20d=high_20d, boll_upper=boll_upper,
                open_price=open_price,
            )
            if aggressive_plan:
                strategies.append(aggressive_plan)

        # --- 如果没有可用策略且有不买原因 → 标注不建议 ---
        if not strategies and avoid_reasons:
            strategies.append(StrategyPlan(
                strategy_type="avoid",
                strategy_label="⛔ 不建议",
                entry_condition="不满足买入条件",
                entry_price_hint="--",
                position_ratio="0",
                stop_loss=0,
                stop_loss_pct=0,
                target_price=0,
                target_price_pct=0,
                invalidation="; ".join(avoid_reasons),
                risk_reward_ratio=0,
                confidence="低",
            ))

        # --- 如果什么策略都没有(中性) → 观望 ---
        if not strategies:
            strategies.append(StrategyPlan(
                strategy_type="watch",
                strategy_label="👁️ 观望",
                entry_condition="等待信号增强",
                entry_price_hint="--",
                position_ratio="0",
                stop_loss=0,
                stop_loss_pct=0,
                target_price=0,
                target_price_pct=0,
                invalidation="暂无明确方向",
                risk_reward_ratio=0,
                confidence="低",
            ))

        # ===== 7. 板块共振+情绪面调校 =====
        for s in strategies:
            if s.strategy_type in ("strong_get_stronger", "main_wave_confirm", "trend", "aggressive"):
                # 强共振: 升级信心
                if sector_resonance == "强共振" and s.confidence == "低":
                    s.confidence = "中"
                elif sector_resonance == "强共振" and s.confidence == "中":
                    s.confidence = "高"
                # 逆势: 降级信心+增加失效条件
                if sector_resonance == "逆势":
                    if s.confidence == "高":
                        s.confidence = "中"
                    elif s.confidence == "中":
                        s.confidence = "低"
                    s.invalidation += "; 板块逆势(个股与板块方向相反)"
                # 大盘弱势: 降级信心
                if market_environment == "weak" and s.confidence == "高":
                    s.confidence = "中"
                # 情绪冰点: 降低仓位
                if sentiment_cycle == "冰点" and s.position_ratio == "1/2仓":
                    s.position_ratio = "1/3仓"
                # 强共振+板块资金流入: 提升仓位
                if sector_resonance == "强共振" and sector_resonance_fund > 5:
                    if s.strategy_type in ("strong_get_stronger", "main_wave_confirm", "trend") and s.position_ratio == "1/3仓":
                        s.position_ratio = "1/2仓"

        result.strategies = strategies
        return result

    def _build_direct_buy_blockers(
        self,
        *,
        price: float,
        change_pct: float,
        ma5: float,
        sr: SupportResistance,
        rsi14: float | None,
        boll_upper: float,
        high_20d: float,
        is_limit_up: bool,
    ) -> list[str]:
        """明日可执行买点过滤: 区分强势跟踪和能直接下手的低风险位置。"""
        if price <= 0:
            return []

        blockers: list[str] = []
        support = sr.primary_support
        support_gap_pct = (price - support) / price * 100 if support > 0 and price > support else 0.0
        ma5_gap_pct = (price - ma5) / price * 100 if ma5 > 0 and price > ma5 else 0.0
        target = self._pick_primary_target(price, sr)
        target_pct = (target - price) / price * 100 if target > price else 0.0
        stop_loss = support * 0.98 if support > 0 else price * 0.95
        risk = price - stop_loss
        reward = target - price
        rr_ratio = reward / risk if risk > 0 and reward > 0 else 0.0

        if is_limit_up or change_pct >= self.DIRECT_BUY_MAX_CHANGE_PCT:
            blockers.append(f"当日涨幅{change_pct:.1f}%，明日不追高")
        if support_gap_pct > self.DIRECT_BUY_MAX_SUPPORT_GAP_PCT:
            blockers.append(f"距支撑{support_gap_pct:.1f}%，直接买点过高")
        if ma5_gap_pct > self.DIRECT_BUY_MAX_MA5_GAP_PCT:
            blockers.append(f"偏离MA5 {ma5_gap_pct:.1f}%，等回踩")
        if rsi14 is not None and rsi14 >= self.DIRECT_BUY_RSI_OVERHEAT:
            blockers.append(f"RSI={rsi14:.0f}过热，等分歧")
        if boll_upper > 0 and price >= boll_upper * 0.98:
            blockers.append("贴近BOLL上轨，性价比不足")
        if high_20d > 0 and price >= high_20d * 0.99 and change_pct >= 4:
            blockers.append("贴近20日高点且当日大涨")
        if target_pct < self.DIRECT_BUY_MIN_TARGET_PCT:
            blockers.append(f"上方空间{target_pct:.1f}%，赔率不足")
        if rr_ratio < self.DIRECT_BUY_MIN_RR_RATIO:
            blockers.append(f"风险收益比{rr_ratio:.1f}不足")

        return blockers

    def _pick_primary_target(self, price: float, sr: SupportResistance) -> float:
        if sr.primary_resistance > 0 and sr.primary_resistance > price:
            return sr.primary_resistance
        if sr.secondary_resistance > 0 and sr.secondary_resistance > price:
            return sr.secondary_resistance
        return price * 1.08

    # =========================================================================
    # 支撑/压力位计算
    # =========================================================================
    def _calc_support_resistance(
        self,
        price: float,
        low: float,
        ma5: float, ma10: float, ma20: float, ma60: float,
        boll_upper: float, boll_mid: float, boll_lower: float,
        high_20d: float, high_60d: float,
        chip_signal: float,
    ) -> SupportResistance:
        """多源支撑/压力位 — 取最优值"""
        sr = SupportResistance()

        # --- 支撑位 ---
        # 短线支撑优先级: MA5 > MA10 > MA20 > MA60 > BOLL下轨 > 筹码密集区
        # 同一个价位区间内, 短期均线更有效(回踩MA5比回踩MA60更常见)
        supports = []  # (price, source, priority) — priority越小越优先
        if ma5 > 0:
            supports.append((ma5, "MA5", 1))
        if ma10 > 0:
            supports.append((ma10, "MA10", 2))
        if ma20 > 0:
            supports.append((ma20, "MA20", 3))
        if ma60 > 0:
            supports.append((ma60, "MA60", 4))
        if boll_lower > 0:
            supports.append((boll_lower, "BOLL下轨", 5))
        # 筹码密集区(近似): 当chip_signal>=6时，BOLL中轨附近是筹码密集区
        if chip_signal >= 6 and boll_mid > 0:
            supports.append((boll_mid, "筹码密集区", 3))

        # 选最接近当前价下方的两个支撑 — 短线MA优先
        # 策略: 差距<0.5%内的支撑位视为"同等有效", 此时按priority(短线优先)排序
        # 差距≥0.5%的才按距离远近排序
        below_supports = [(p, s, pri) for p, s, pri in supports if p > 0 and p <= price]
        def _sort_key(item):
            p, s, pri = item
            gap_pct = (price - p) / price * 100 if price > 0 else 999
            # 差距<0.5%视为同档, 同档内按priority(短线优先)
            tier = int(gap_pct / 0.5)  # 0.5%一档
            return (tier, pri, gap_pct)
        below_supports.sort(key=_sort_key)

        if below_supports:
            sr.primary_support = below_supports[0][0]
            sr.support_source = below_supports[0][1]
            if len(below_supports) > 1:
                sr.secondary_support = below_supports[1][0]

        # 如果没有下方支撑(极端情况)，用最低支撑位
        if sr.primary_support == 0 and supports:
            valid = [(p, s, pri) for p, s, pri in supports if p > 0]
            valid.sort(key=lambda x: (price - x[0], x[2]))
            if valid:
                sr.primary_support = valid[0][0]
                sr.support_source = valid[0][1] + "(上方)"

        # --- 压力位 ---
        resistances = []
        if high_20d > 0:
            resistances.append((high_20d, "20日高点"))
        if high_60d > 0:
            resistances.append((high_60d, "60日高点"))
        if boll_upper > 0:
            resistances.append((boll_upper, "BOLL上轨"))
        # 整数关口: 价格上方最近的整数
        if price > 0:
            import math as m
            ceil_int = m.ceil(price / 10) * 10   # 10元整数关
            ceil_5 = m.ceil(price / 5) * 5        # 5元整数关
            if price < 50:
                # 低价股看5元关口
                resistances.append((ceil_5, f"整数关{ceil_5:.0f}"))
            else:
                resistances.append((ceil_int, f"整数关{ceil_int:.0f}"))

        # 选最接近当前价上方的两个压力
        above_resistances = [(p, s) for p, s in resistances if p > 0 and p >= price]
        above_resistances.sort(key=lambda x: x[0])  # 从低到高

        if above_resistances:
            sr.primary_resistance = above_resistances[0][0]
            sr.resistance_source = above_resistances[0][1]
            if len(above_resistances) > 1:
                sr.secondary_resistance = above_resistances[1][0]

        # 如果没有上方压力(突破所有位)
        if sr.primary_resistance == 0 and resistances:
            valid = [(p, s) for p, s in resistances if p > 0]
            valid.sort(key=lambda x: x[0], reverse=True)
            if valid:
                sr.primary_resistance = valid[0][0]
                sr.resistance_source = valid[0][1] + "(已突破)"

        return sr

    # =========================================================================
    # 技术状态概要
    # =========================================================================
    def _build_tech_summary(
        self,
        ma5: float, ma10: float, ma20: float, ma60: float,
        macd_signal: str, rsi14: float | None, kdj_signal: str,
        boll_upper: float, price: float,
    ) -> str:
        parts = []
        # MA排列
        if ma5 > 0 and ma10 > 0 and ma20 > 0:
            if price > ma5 > ma10 > ma20:
                parts.append("多头排列")
            elif price < ma5 < ma10 < ma20:
                parts.append("空头排列")
            elif price > ma20:
                parts.append("站上MA20")
            else:
                parts.append("跌破MA20")
        # 信号
        if macd_signal == "golden_cross":
            parts.append("MACD金叉")
        elif macd_signal == "death_cross":
            parts.append("MACD死叉")
        if kdj_signal == "golden_cross":
            parts.append("KDJ金叉")
        elif kdj_signal == "death_cross":
            parts.append("KDJ死叉")
        # RSI区间
        if rsi14 is not None and rsi14 > 0:
            if rsi14 > 80:
                parts.append("RSI极度超买")
            elif rsi14 > 70:
                parts.append("RSI超买")
            elif 50 <= rsi14 <= 70:
                parts.append("RSI健康")
            elif rsi14 < 30:
                parts.append("RSI超卖")
        # BOLL位置
        if boll_upper > 0 and price >= boll_upper:
            parts.append("触及BOLL上轨")

        return "+".join(parts) if parts else "数据不足"

    # =========================================================================
    # 强者恒强买法(龙头承接)
    # =========================================================================
    def _build_strong_get_stronger_strategy(
        self,
        price: float, bull_level: str, bull_score: float,
        ma5: float, ma10: float, ma20: float,
        sr: SupportResistance,
        fund_5d_billion: float, fund_5d_direction: str,
        rsi14: float | None, volume_ratio: float, turnover: float,
        change_pct: float, avoid_reasons: list,
        high_20d: float, high_60d: float,
        consecutive_days: int,
        is_limit_up: bool,
        is_one_word: bool,
        price_volume_relation: str,
        sector_resonance: str,
    ) -> Optional[StrategyPlan]:
        """强者恒强: 高辨识度强势票只给次日承接确认，不给无条件追高。"""
        if bull_level not in ("S", "A") or bull_score < 90:
            return None
        if price <= 0 or ma20 <= 0 or price < ma20:
            return None
        if is_one_word:
            return None
        if rsi14 is not None and rsi14 > self.STRONG_GET_STRONGER_MAX_RSI:
            return None
        if change_pct < 1.5 or change_pct > 10.5:
            return None

        hard_avoids = [
            r for r in avoid_reasons
            if any(kw in r for kw in [
                "评分C", "评分D", "跌破MA20", "5日净流出",
                "缩量涨停", "极度超买",
                *self.DIRECT_BUY_BLOCKER_KEYWORDS,
            ])
        ]
        if hard_avoids:
            return None

        support = sr.primary_support if sr.primary_support > 0 else (ma5 if ma5 > 0 else ma20)
        support_gap_pct = (price - support) / price * 100 if support > 0 and price > support else 0.0
        if support_gap_pct > self.STRONG_GET_STRONGER_MAX_SUPPORT_GAP_PCT:
            return None

        confirmations: list[str] = []
        if ma5 > 0 and ma10 > 0 and price > ma5 > ma10 > ma20:
            confirmations.append("多头排列")
        elif ma5 > 0 and price >= ma5 * 0.98:
            confirmations.append("贴近MA5强势承接")
        if high_20d > 0 and price >= high_20d * 0.97:
            confirmations.append("接近20日新高")
        if is_limit_up or consecutive_days >= 1:
            confirmations.append(f"{consecutive_days or 1}板辨识度")
        if volume_ratio >= 1.3 or turnover >= 5:
            confirmations.append("量能放大")
        if fund_5d_billion > 1:
            confirmations.append(f"5日资金{fund_5d_direction}")
        if price_volume_relation == "放量上涨":
            confirmations.append("量价齐升")
        if sector_resonance == "强共振":
            confirmations.append("板块强共振")
        if bull_score >= 94:
            confirmations.append("评分顶格")

        if len(confirmations) < 3:
            return None

        stop_anchor = support if support > 0 else price * 0.94
        stop_loss = round(max(stop_anchor * 0.98, price * 0.94), 2)
        if stop_loss >= price:
            stop_loss = round(price * 0.94, 2)

        if sr.secondary_resistance > 0 and sr.secondary_resistance > price:
            target = sr.secondary_resistance
        elif sr.primary_resistance > 0 and sr.primary_resistance > price:
            target = round(sr.primary_resistance * 1.03, 2)
        elif high_60d > price:
            target = high_60d
        else:
            target = round(price * 1.10, 2)

        risk = price - stop_loss
        reward = target - price
        rr_ratio = round(reward / risk, 1) if risk > 0 and reward > 0 else 0.0
        if rr_ratio < 1.0:
            return None

        entry_parts = [
            "竞价0%-3.5%只观察",
            "首次缩量回踩开盘价/分时均价不破",
            "滚动60秒止跌回收且增量成交转正",
        ]
        invalidations = [
            "开盘急拉超过5%且未回踩不追",
            "回踩后不能重回开盘价/分时均价",
            f"跌破强势支撑{support:.2f}",
            "板块强度转弱",
        ]
        if rsi14 is not None and rsi14 >= 78:
            invalidations.append(f"RSI继续上冲至86以上(当前{rsi14:.0f})")

        confidence = "高" if len(confirmations) >= 5 and bull_score >= 94 else "中"
        return StrategyPlan(
            strategy_type="strong_get_stronger",
            strategy_label="🟠 强势回踩承接",
            entry_condition=f"{' + '.join(entry_parts)}；{'+'.join(confirmations[:4])}",
            entry_price_hint=f"回踩参考≈{support:.2f}-{support * 1.015:.2f}",
            position_ratio="1/4仓",
            stop_loss=stop_loss,
            stop_loss_pct=round((price - stop_loss) / price * 100, 2),
            target_price=round(target, 2),
            target_price_pct=round((target - price) / price * 100, 2),
            invalidation="; ".join(invalidations),
            risk_reward_ratio=rr_ratio,
            confidence=confidence,
        )

    # =========================================================================
    # 主升确认买法(高确定性)
    # =========================================================================
    def _build_main_wave_confirm_strategy(
        self,
        price: float, bull_level: str, bull_score: float,
        ma5: float, ma10: float, ma20: float, ma60: float,
        sr: SupportResistance,
        fund_5d_billion: float, fund_5d_direction: str,
        rsi14: float | None, volume_ratio: float, turnover: float,
        change_pct: float, avoid_reasons: list,
        macd_signal: str, kdj_signal: str,
    ) -> Optional[StrategyPlan]:
        """主升确认: 多头排列和动能确认后，只在贴线/贴支撑位置给入场。"""
        if bull_level not in ("S", "A"):
            return None
        if price <= 0 or not (price > ma5 > ma10 > ma20 > 0):
            return None

        hard_avoids = [
            r for r in avoid_reasons
            if any(kw in r for kw in [
                "评分C", "评分D", "跌破MA20", "5日净流出",
                "明日不追高", "直接买点过高", "等回踩",
                "等分歧", "性价比不足", "赔率不足", "风险收益比",
                "缩量涨停", "板块生命周期", "资金数据不足",
            ])
        ]
        if hard_avoids:
            return None

        support = sr.primary_support if sr.primary_support > 0 else ma5
        support_gap_pct = (price - support) / price * 100 if support > 0 else 999.0
        ma5_gap_pct = (price - ma5) / price * 100 if ma5 > 0 else 999.0
        if support_gap_pct > self.MAIN_WAVE_CONFIRM_MAX_SUPPORT_GAP_PCT:
            return None
        if ma5_gap_pct > self.MAIN_WAVE_CONFIRM_MAX_MA5_GAP_PCT:
            return None
        if change_pct < -4 or change_pct > 5.8:
            return None
        if rsi14 is None or not (50 <= rsi14 <= 73):
            return None

        momentum_confirmations = []
        if fund_5d_billion > 0:
            momentum_confirmations.append(f"5日资金{fund_5d_direction}")
        if volume_ratio >= 1.05 or turnover >= 3:
            momentum_confirmations.append("量能承接")
        if macd_signal == "golden_cross":
            momentum_confirmations.append("MACD金叉")
        if kdj_signal == "golden_cross":
            momentum_confirmations.append("KDJ金叉")
        if bull_score >= 88:
            momentum_confirmations.append("评分强")
        if len(momentum_confirmations) < 2:
            return None

        stop_loss = round(support * 0.98, 2)
        target = self._pick_primary_target(price, sr)
        target_pct = round((target - price) / price * 100, 2) if price > 0 else 0
        risk = price - stop_loss
        reward = target - price
        rr_ratio = round(reward / risk, 1) if risk > 0 else 0
        if rr_ratio < self.DIRECT_BUY_MIN_RR_RATIO or target_pct < self.DIRECT_BUY_MIN_TARGET_PCT:
            return None

        invalidations = [
            f"跌破支撑{support:.2f}",
            "MA5失守后不接回",
        ]
        if fund_5d_billion > 0 and fund_5d_billion < 2:
            invalidations.append("5日资金转向净流出")
        if rsi14 is not None and rsi14 > 68:
            invalidations.append(f"RSI突破76(当前{rsi14:.0f})")

        entry_basis = "+".join(momentum_confirmations[:3])
        return StrategyPlan(
            strategy_type="main_wave_confirm",
            strategy_label="🟣 主升确认",
            entry_condition=f"贴近MA5/支撑({support:.2f})，{entry_basis}",
            entry_price_hint=f"≈{support:.2f}",
            position_ratio="1/2仓" if rr_ratio >= 1.8 and bull_score >= 88 else "1/3仓",
            stop_loss=stop_loss,
            stop_loss_pct=round((price - stop_loss) / price * 100, 2) if price > 0 else 0,
            target_price=round(target, 2),
            target_price_pct=target_pct,
            invalidation="; ".join(invalidations),
            risk_reward_ratio=rr_ratio,
            confidence="高",
        )

    # =========================================================================
    # 趋势买法(胜率优先)
    # =========================================================================
    def _build_trend_strategy(
        self,
        price: float, bull_level: str, bull_score: float,
        ma5: float, ma10: float, ma20: float, ma60: float,
        sr: SupportResistance,
        fund_5d_billion: float, fund_5d_direction: str,
        rsi14: float | None, volume_ratio: float,
        change_pct: float, avoid_reasons: list,
        macd_signal: str, kdj_signal: str,
    ) -> Optional[StrategyPlan]:
        """趋势买法: 回踩支撑位入场, 胜率优先"""

        # --- 条件判定 ---
        conditions_met = 0
        conditions_total = 3
        entry_details = []

        # 条件1: 评分≥B
        if bull_level in ("S", "A", "B"):
            conditions_met += 1
            entry_details.append(f"评级{bull_level}")
        else:
            return None  # 评分不够, 无趋势买法

        # 条件2: 均线多头排列 OR 站上MA20
        is_bull_align = (ma5 > 0 and ma10 > 0 and ma20 > 0 and price > ma5 > ma10 > ma20)
        is_above_ma20 = ma20 > 0 and price > ma20
        if is_bull_align:
            conditions_met += 1
            entry_details.append("均线多头排列")
        elif is_above_ma20:
            conditions_met += 1
            entry_details.append("站上MA20")
        else:
            # 趋势买法要求至少站上MA20
            return None

        # 条件3: 5日资金方向偏多 OR MACD/KDJ金叉
        fund_positive = fund_5d_billion > 0
        tech_signal = macd_signal == "golden_cross" or kdj_signal == "golden_cross"
        if fund_positive:
            conditions_met += 1
            entry_details.append(f"5日资金{fund_5d_direction}")
        elif tech_signal:
            conditions_met += 1
            entry_details.append("技术信号确认")
        else:
            # 资金和技术都不支持, 降低信心但不排除
            entry_details.append("资金/技术待确认")

        # 如果有严重不买原因, 不生成趋势买法
        hard_avoids = [
            r for r in avoid_reasons
            if any(kw in r for kw in [
                "评分C", "评分D", "跌破MA20", "5日净流出",
                "明日不追高", "直接买点过高", "等回踩",
                "等分歧", "性价比不足", "赔率不足", "风险收益比",
                "板块生命周期", "资金数据不足",
            ])
        ]
        if hard_avoids:
            return None

        # --- 止损/目标价 ---
        support = sr.primary_support if sr.primary_support > 0 else (ma20 if ma20 > 0 else price * 0.95)
        stop_loss = round(support * 0.98, 2)
        stop_loss_pct = round((price - stop_loss) / price * 100, 2) if price > 0 else 0

        target = self._pick_primary_target(price, sr)
        target_pct = round((target - price) / price * 100, 2) if price > 0 else 0

        # 风险收益比
        risk = price - stop_loss
        reward = target - price
        rr_ratio = round(reward / risk, 1) if risk > 0 else 0

        # 仓位: 趋势买法1/3仓, 信心高时1/2仓
        if conditions_met >= 3 and rr_ratio >= 2:
            position = "1/2仓"
        else:
            position = "1/3仓"

        # 信心等级
        if conditions_met >= 3 and rr_ratio >= 2:
            confidence = "高"
        elif conditions_met >= 2:
            confidence = "中"
        else:
            confidence = "低"

        # 入场条件描述
        entry_condition = f"回踩{sr.support_source or '支撑位'}附近({sr.primary_support:.2f})企稳"
        entry_price_hint = f"≈{sr.primary_support:.2f}"

        # 失效条件
        invalidations = [f"跌破支撑{support:.2f}"]
        if fund_5d_billion > 0 and fund_5d_billion < 2:
            invalidations.append("5日资金转向净流出")
        elif fund_5d_billion < 0:
            invalidations.append(f"5日资金净流出扩大(当前{fund_5d_billion:.1f}亿)")
        if rsi14 is not None and rsi14 > 70:
            invalidations.append(f"RSI突破80(当前{rsi14:.0f})")

        return StrategyPlan(
            strategy_type="trend",
            strategy_label="🔵 趋势买法",
            entry_condition=entry_condition,
            entry_price_hint=entry_price_hint,
            position_ratio=position,
            stop_loss=stop_loss,
            stop_loss_pct=stop_loss_pct,
            target_price=round(target, 2),
            target_price_pct=target_pct,
            invalidation="; ".join(invalidations),
            risk_reward_ratio=rr_ratio,
            confidence=confidence,
        )

    # =========================================================================
    # 激进买法(赔率优先)
    # =========================================================================
    def _build_aggressive_strategy(
        self,
        price: float, bull_level: str, bull_score: float,
        ma5: float, ma10: float, ma20: float,
        sr: SupportResistance,
        rsi14: float | None,
        macd_signal: str, kdj_signal: str,
        volume_ratio: float, volume_ratio_level: str,
        turnover: float, turnover_level: str,
        price_volume_relation: str,
        fund_5d_billion: float,
        change_pct: float, avoid_reasons: list,
        consecutive_days: int,
        high_20d: float, boll_upper: float,
        open_price: float,
    ) -> Optional[StrategyPlan]:
        """激进买法: 竞价/突破追入, 赔率优先"""

        # --- 条件判定 ---
        # 最低门槛: BullScore≥A + 至少1项技术确认 + 放量
        if bull_level not in ("S", "A"):
            return None

        # 硬性不买
        hard_avoids = [
            r for r in avoid_reasons
            if any(kw in r for kw in [
                "5日净流出", "RSI", "缩量涨停",
                "明日不追高", "直接买点过高", "等回踩",
                "等分歧", "性价比不足", "赔率不足", "风险收益比",
                "板块生命周期", "资金数据不足",
            ])
        ]
        if hard_avoids:
            return None

        # 技术确认(MACD金叉/RSI健康/KDJ金叉 至少1项)
        tech_confirmations = []
        if macd_signal == "golden_cross":
            tech_confirmations.append("MACD金叉")
        if kdj_signal == "golden_cross":
            tech_confirmations.append("KDJ金叉")
        if rsi14 is not None and 50 <= rsi14 <= 70:
            tech_confirmations.append(f"RSI健康({rsi14:.0f})")
        elif rsi14 is not None and 30 <= rsi14 < 50:
            tech_confirmations.append(f"RSI偏低({rsi14:.0f})")

        if not tech_confirmations:
            return None  # 无技术确认, 不做激进

        # 量能确认
        volume_ok = volume_ratio > 1.5 or volume_ratio_level in ("放量", "巨量")
        if not volume_ok:
            return None  # 无量不追

        # --- 入场场景 ---
        # 场景1: 竞价高开2-5% + 量比>1.5
        auction_entry = False
        entry_detail = ""
        if volume_ratio > 1.5 and change_pct > 2 and change_pct < 7:
            auction_entry = True
            entry_detail = f"竞价高开{change_pct:.1f}%+量比{volume_ratio:.1f}"

        # 场景2: 突破前高 + 量价齐升
        breakout_entry = False
        if high_20d > 0 and price >= high_20d and price_volume_relation == "放量上涨":
            breakout_entry = True
            entry_detail = f"突破20日高{high_20d:.2f}+放量"

        # 场景3: 连板/强势 + 资金大举流入
        strong_entry = False
        if consecutive_days >= 2 and fund_5d_billion > 2:
            strong_entry = True
            entry_detail = f"{consecutive_days}连板+5日流入{fund_5d_billion:.1f}亿"

        if not (auction_entry or breakout_entry or strong_entry):
            # 即使不满足特定场景, A级+技术确认+放量也允许盘中低吸
            if bull_level == "S" and len(tech_confirmations) >= 2:
                entry_detail = f"A级+{'+'.join(tech_confirmations[:2])}+放量,盘中低吸"
            else:
                return None

        # --- 止损/目标价 ---
        # 激进止损更紧: 入场价×0.97
        stop_loss = round(price * 0.97, 2)
        stop_loss_pct = 3.0

        # 如果有突破场景, 止损=突破价×0.97
        if breakout_entry and high_20d > 0:
            stop_loss = round(high_20d * 0.97, 2)
            stop_loss_pct = round((price - stop_loss) / price * 100, 2)

        # 目标: 如果第二压力位>当前价则用, 否则第一压力×1.03, 否则+10%
        if sr.secondary_resistance > 0 and sr.secondary_resistance > price:
            target = sr.secondary_resistance
        elif sr.primary_resistance > 0 and sr.primary_resistance > price:
            target = round(sr.primary_resistance * 1.03, 2)
        else:
            target = round(price * 1.10, 2)
        target_pct = round((target - price) / price * 100, 2) if price > 0 else 0

        # 风险收益比
        risk = price - stop_loss
        reward = target - price
        rr_ratio = round(reward / risk, 1) if risk > 0 else 0

        # 失效条件
        invalidations = [
            "高开低走(涨幅从+3%翻绿)",
            f"量能萎缩(量比<0.8, 当前{volume_ratio:.1f})",
        ]
        if rsi14 is not None and rsi14 > 70:
            invalidations.append(f"RSI突破80(当前{rsi14:.0f})")

        # 信心等级
        if bull_level == "S" and len(tech_confirmations) >= 2:
            confidence = "高"
        elif bull_level == "A" and len(tech_confirmations) >= 2:
            confidence = "中"
        else:
            confidence = "低"

        return StrategyPlan(
            strategy_type="aggressive",
            strategy_label="🔴 激进买法",
            entry_condition=entry_detail,
            entry_price_hint=f"≈{price:.2f}(盘中)",
            position_ratio="1/4仓",
            stop_loss=stop_loss,
            stop_loss_pct=stop_loss_pct,
            target_price=round(target, 2),
            target_price_pct=target_pct,
            invalidation="; ".join(invalidations),
            risk_reward_ratio=rr_ratio,
            confidence=confidence,
        )


# 全局实例
next_day_plan_engine = NextDayPlanEngine()
