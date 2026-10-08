"""风控规则集 — 7类核心风控规则

规则列表:
1. BlacklistRule - 黑名单拦截(ST/停牌/退市)
2. StopLossRule - 止损规则
3. PositionLimitRule - 单股仓位上限
4. TotalPositionRule - 总仓位限制
5. MaxDrawdownRule - 最大回撤熔断
6. LockupWarningRule - 解禁预警
7. SentimentCircuitBreakerRule - 情绪熔断
8. ConcentrationRule - 行业集中度
9. TimeStopRule - 时间止损
10. ObserveOnlyRule - 观察标的限制
"""

from app.risk.engine import (
    RiskRule, RiskContext, RiskDecision,
    RiskLevel, RiskCategory,
)
from app.config.settings import settings


# ========== 1. 黑名单拦截 ==========

class BlacklistRule(RiskRule):
    """黑名单拦截 — ST/停牌/退市/次新不允许买入"""

    rule_name = "blacklist"
    category = RiskCategory.BLACKLIST
    enabled = True
    priority = 10  # 最高优先级

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.action != "buy":
            return self.pass_decision(ctx)

        # ST拦截
        if ctx.is_st:
            return self.block_decision(
                ctx,
                message=f"{ctx.code} 为ST股，不允许买入",
                suggestion="ST股风险极高，已自动拦截",
            )

        # 停牌拦截
        if ctx.is_suspended:
            return self.block_decision(
                ctx,
                message=f"{ctx.code} 停牌中，无法交易",
                suggestion="等待复牌后再考虑",
            )

        # 退市拦截
        if ctx.is_delisting:
            return self.block_decision(
                ctx,
                message=f"{ctx.code} 退市风险，不允许买入",
                suggestion="退市股禁止买入",
            )

        # 次新警告(不拦截但警告)
        if ctx.is_ipo_recent:
            return self.warn_decision(
                ctx,
                message=f"{ctx.code} 为次新股(上市<60天)，波动风险大",
                suggestion="次新股流动性差、波动大，注意控制仓位",
            )

        return self.pass_decision(ctx)


# ========== 2. 止损规则 ==========

class StopLossRule(RiskRule):
    """止损规则 — 个股亏损达到止损线必须卖出"""

    rule_name = "stop_loss"
    category = RiskCategory.STOP_LOSS
    enabled = True
    priority = 20

    # 可配置参数
    stop_loss_pct: float = settings.DEFAULT_STOP_LOSS_PCT  # 默认7%
    trailing_stop_pct: float = 10.0  # 跟踪止损(盈利回撤)
    trailing_trigger_pct: float = 15.0  # 盈利超15%启动跟踪止损

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.action != "sell" and ctx.action != "hold":
            return self.pass_decision(ctx)

        profit = ctx.profit_pct

        # 固定止损
        if profit <= -self.stop_loss_pct:
            return self.block_decision(
                ctx,
                message=f"{ctx.code} 亏损{abs(profit):.1f}%，超过止损线{self.stop_loss_pct}%",
                suggestion="立即止损卖出",
                detail={"profit_pct": profit, "stop_line": -self.stop_loss_pct},
            )

        # 跟踪止损(盈利回撤)
        if profit >= self.trailing_trigger_pct:
            # 简化: 如果从最高点回撤超过trailing_stop_pct
            # 完整版需要记录最高盈利
            pass

        # 时间止损(持有超过N天且亏损)
        if ctx.hold_days > 5 and profit < -3:
            return self.warn_decision(
                ctx,
                message=f"{ctx.code} 持有{ctx.hold_days}天仍亏损{abs(profit):.1f}%",
                suggestion="考虑止损，避免深套",
                detail={"hold_days": ctx.hold_days, "profit_pct": profit},
            )

        return self.pass_decision(ctx)


# ========== 3. 单股仓位限制 ==========

class PositionLimitRule(RiskRule):
    """单股仓位上限 — 单只股票持仓不超过总资产的N%"""

    rule_name = "position_limit"
    category = RiskCategory.POSITION
    enabled = True
    priority = 30

    max_position_pct: float = settings.POSITION_LIMIT_PCT  # 30%

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.action != "buy":
            return self.pass_decision(ctx)

        if ctx.total_assets <= 0:
            return self.pass_decision(ctx)

        # 计算本次交易后该股仓位
        trade_value = ctx.price * ctx.amount
        current_value = ctx.current_positions.get(ctx.code, {}).get("market_value", 0)
        total_after = current_value + trade_value
        position_pct = total_after / ctx.total_assets * 100

        if position_pct > self.max_position_pct:
            return self.block_decision(
                ctx,
                message=f"{ctx.code} 买入后仓位将达{position_pct:.1f}%，超过上限{self.max_position_pct}%",
                suggestion=f"减少买入量或降低仓位",
                detail={
                    "current_pct": current_value / ctx.total_assets * 100 if ctx.total_assets > 0 else 0,
                    "after_pct": position_pct,
                    "limit_pct": self.max_position_pct,
                },
            )

        # 仓位>20%时警告
        if position_pct > self.max_position_pct * 0.67:
            return self.warn_decision(
                ctx,
                message=f"{ctx.code} 买入后仓位{position_pct:.1f}%，接近上限",
                suggestion="注意集中度风险",
            )

        return self.pass_decision(ctx)


# ========== 4. 总仓位限制 ==========

class TotalPositionRule(RiskRule):
    """总仓位限制 — 总持仓占比不超过N%"""

    rule_name = "total_position"
    category = RiskCategory.POSITION
    enabled = True
    priority = 35

    max_total_pct: float = settings.TOTAL_POSITION_LIMIT_PCT

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.action != "buy":
            return self.pass_decision(ctx)

        if ctx.total_assets <= 0:
            return self.pass_decision(ctx)

        # 计算买入后总仓位
        trade_value = ctx.price * ctx.amount
        total_position = ctx.position_value + trade_value
        position_pct = total_position / ctx.total_assets * 100

        if position_pct > self.max_total_pct:
            return self.block_decision(
                ctx,
                message=f"买入后总仓位{position_pct:.1f}%，超过上限{self.max_total_pct}%",
                suggestion="仓位过重，考虑减仓后再买",
                detail={
                    "current_total_pct": ctx.position_value / ctx.total_assets * 100,
                    "after_pct": position_pct,
                    "cash_remaining": ctx.cash - trade_value,
                },
            )

        # 仓位>70%时警告
        if position_pct > self.max_total_pct * 0.875:
            return self.warn_decision(
                ctx,
                message=f"买入后总仓位{position_pct:.1f}%，偏高",
                suggestion="预留现金应对风险",
            )

        return self.pass_decision(ctx)


# ========== 5. 最大回撤熔断 ==========

class MaxDrawdownRule(RiskRule):
    """最大回撤熔断 — 账户回撤超过阈值触发降仓"""

    rule_name = "max_drawdown"
    category = RiskCategory.DRAWDOWN
    enabled = True
    priority = 25

    max_drawdown_pct: float = settings.MAX_DRAWDOWN_PCT  # 15%
    warn_drawdown_pct: float = 10.0  # 警告线

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.action == "sell":
            return self.pass_decision(ctx)

        drawdown = ctx.max_drawdown
        if ctx.is_paper_experiment and ctx.action == "buy":
            return RiskDecision(
                rule_name=self.rule_name, category=self.category, level=RiskLevel.PASS,
                message=f"持续模拟实验：实际回撤{drawdown:.1f}%仅记录，不按历史绩效停用策略",
                detail={"current_drawdown": drawdown, "policy": "experiment_tag_only"},
            )

        configured_recovery_limit = float(ctx.drawdown_recovery_limit_pct or 0)
        if (
            ctx.action == "buy"
            and ctx.is_drawdown_recovery_probe
            and drawdown >= self.max_drawdown_pct
        ):
            if configured_recovery_limit <= 0:
                return self.warn_decision(
                    ctx,
                    message=f"账户回撤{drawdown:.1f}%，内部模拟盘高分恢复单继续检查",
                    suggestion="仅允许高分恢复单小仓试错，其他风控规则继续生效",
                    detail={
                        "current_drawdown": drawdown,
                        "standard_drawdown_limit": self.max_drawdown_pct,
                        "recovery_drawdown_limit": None,
                    },
                )

            recovery_limit = max(self.max_drawdown_pct, configured_recovery_limit)
            if recovery_limit <= self.max_drawdown_pct:
                return self.block_decision(
                    ctx,
                    message=f"账户回撤{drawdown:.1f}%，超过熔断线{self.max_drawdown_pct}%",
                    suggestion="触发回撤熔断！禁止新建仓，建议减仓至50%以下",
                    detail={
                        "current_drawdown": drawdown,
                        "max_drawdown_limit": self.max_drawdown_pct,
                    },
                )
            if drawdown < recovery_limit:
                return self.warn_decision(
                    ctx,
                    message=(
                        f"账户回撤{drawdown:.1f}%，模拟盘恢复试错单按"
                        f"{recovery_limit:.1f}%硬上限继续检查"
                    ),
                    suggestion="仅允许高分恢复单小仓试错，其他风控规则继续生效",
                    detail={
                        "current_drawdown": drawdown,
                        "standard_drawdown_limit": self.max_drawdown_pct,
                        "recovery_drawdown_limit": recovery_limit,
                    },
                )
            return self.block_decision(
                ctx,
                message=(
                    f"账户回撤{drawdown:.1f}%，达到恢复模式硬上限"
                    f"{recovery_limit:.1f}%"
                ),
                suggestion="停止恢复试错，禁止新建仓",
                detail={
                    "current_drawdown": drawdown,
                    "recovery_drawdown_limit": recovery_limit,
                },
            )

        # 超过最大回撤 → 强制减仓
        if drawdown >= self.max_drawdown_pct:
            return self.block_decision(
                ctx,
                message=f"账户回撤{drawdown:.1f}%，超过熔断线{self.max_drawdown_pct}%",
                suggestion="触发回撤熔断！禁止新建仓，建议减仓至50%以下",
                detail={
                    "current_drawdown": drawdown,
                    "max_drawdown_limit": self.max_drawdown_pct,
                },
            )

        # 接近警告线
        if drawdown >= self.warn_drawdown_pct:
            return self.warn_decision(
                ctx,
                message=f"账户回撤{drawdown:.1f}%，接近熔断线",
                suggestion="谨慎开仓，控制仓位在60%以下",
            )

        return self.pass_decision(ctx)


# ========== 6. 解禁预警 ==========

class LockupWarningRule(RiskRule):
    """解禁预警 — 近期有限售股解禁的股票限制买入"""

    rule_name = "lockup_warning"
    category = RiskCategory.LOCKUP
    enabled = True
    priority = 40

    block_ratio: float = 10.0    # 解禁占比>10%拦截
    warn_ratio: float = 3.0      # 解禁占比>3%警告

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.action != "buy":
            return self.pass_decision(ctx)

        if not ctx.has_lockup_soon:
            return self.pass_decision(ctx)

        ratio = ctx.lockup_ratio

        # 大规模解禁 → 拦截
        if ratio >= self.block_ratio:
            return self.block_decision(
                ctx,
                message=f"{ctx.code} 近期有限售股解禁，占流通比{ratio:.1f}%，抛压风险极大",
                suggestion="解禁股不宜参与，等解禁完成后再看",
                detail={"lockup_ratio": ratio},
            )

        # 小规模解禁 → 警告
        if ratio >= self.warn_ratio:
            return self.warn_decision(
                ctx,
                message=f"{ctx.code} 近期有限售股解禁，占流通比{ratio:.1f}%",
                suggestion="注意解禁抛压，控制仓位",
                detail={"lockup_ratio": ratio},
            )

        return self.pass_decision(ctx)


# ========== 7. 情绪熔断 ==========

class SentimentCircuitBreakerRule(RiskRule):
    """情绪熔断 — 市场极端情绪下自动降仓

    规则:
    - 冰点期(freezing): 限制开仓，只允许卖出
    - 亢奋期(climax): 警告追高风险
    - 分歧期(divergence): 正常交易但提醒
    """

    rule_name = "sentiment_circuit_breaker"
    category = RiskCategory.SENTIMENT
    enabled = True
    priority = 15  # 仅次于黑名单

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.action == "sell":
            return self.pass_decision(ctx)  # 卖出永远允许

        quality_status = str(ctx.sentiment_quality_status or "missing").lower()
        quality_required = not ctx.is_paper_experiment or ctx.sentiment_required
        valid_quality = {"ok"} if ctx.is_paper_experiment else {"ok", "unchecked"}
        if quality_required and quality_status not in valid_quality:
            reason = str(ctx.sentiment_quality_reason or "市场情绪快照缺失或质量降级")
            return self.block_decision(
                ctx,
                message=f"市场情绪数据质量为{quality_status}：{reason}",
                suggestion="等待当日情绪快照恢复并通过质量门后再开仓",
                detail={
                    "sentiment_cycle": ctx.sentiment_cycle,
                    "sentiment_score": ctx.sentiment_score,
                    "quality_status": quality_status,
                    "quality_reason": reason,
                },
            )

        cycle = ctx.sentiment_cycle
        if ctx.is_paper_experiment and ctx.action == "buy":
            return RiskDecision(
                rule_name=self.rule_name, category=self.category, level=RiskLevel.PASS,
                message="持续模拟实验：市场状态仅分层记录，策略自身必需数据仍须有效",
                detail={
                    "sentiment_cycle": cycle, "sentiment_score": ctx.sentiment_score,
                    "quality_status": quality_status,
                    "quality_reason": ctx.sentiment_quality_reason,
                    "sentiment_required": ctx.sentiment_required,
                    "policy": "experiment_tag_only",
                },
            )

        # 冰点期 → 禁止开仓
        if cycle == "freezing":
            return self.block_decision(
                ctx,
                message="市场处于冰点期，涨停家数极少，不宜开仓",
                suggestion="等待情绪修复信号再入场",
                detail={"sentiment_cycle": cycle},
            )

        # 亢奋期 → 警告追高
        if cycle == "climax" and ctx.action == "buy":
            return self.warn_decision(
                ctx,
                message="市场处于亢奋期，追高风险大",
                suggestion="谨慎追高，控制仓位，注意见顶信号",
                detail={"sentiment_cycle": cycle},
            )

        return self.pass_decision(ctx)


# ========== 8. 行业集中度 ==========

class ConcentrationRule(RiskRule):
    """行业集中度 — 单一行业持仓不超过总资产N%"""

    rule_name = "concentration"
    category = RiskCategory.CONCENTRATION
    enabled = True
    priority = 50

    max_sector_pct: float = 40.0  # 单行业上限40%

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.action != "buy":
            return self.pass_decision(ctx)

        if not ctx.sector_exposure:
            return self.pass_decision(ctx)

        # 找出该股所属行业
        # 简化: 通过sector_exposure检查是否超标
        max_sector = max(ctx.sector_exposure.values()) if ctx.sector_exposure else 0

        if max_sector >= self.max_sector_pct:
            return self.block_decision(
                ctx,
                message=f"单一行业仓位已达{max_sector:.1f}%，超过上限{self.max_sector_pct}%",
                suggestion="分散投资，降低行业集中度",
                detail={"sector_exposure": ctx.sector_exposure},
            )

        if max_sector >= self.max_sector_pct * 0.75:
            return self.warn_decision(
                ctx,
                message=f"单一行业仓位{max_sector:.1f}%，偏高",
                suggestion="注意行业分散",
            )

        return self.pass_decision(ctx)


# ========== 9. 时间止损 ==========

class TimeStopRule(RiskRule):
    """时间止损 — 持有超过N天且不达预期自动卖出"""

    rule_name = "time_stop"
    category = RiskCategory.TIME
    enabled = True
    priority = 60

    max_hold_days: int = 15        # 最长持有15天
    min_profit_pct: float = 2.0    # 最少盈利2%否则止损

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.action not in ("sell", "hold"):
            return self.pass_decision(ctx)

        if ctx.hold_days <= 0:
            return self.pass_decision(ctx)

        # 超时且不达预期
        if ctx.hold_days >= self.max_hold_days and ctx.profit_pct < self.min_profit_pct:
            return self.warn_decision(
                ctx,
                message=f"{ctx.code} 持有{ctx.hold_days}天，盈利仅{ctx.profit_pct:.1f}%，低于预期",
                suggestion="时间止损，换股操作",
                detail={"hold_days": ctx.hold_days, "profit_pct": ctx.profit_pct},
            )

        return self.pass_decision(ctx)


# ========== 10. 观察标的限制 ==========

class ObserveOnlyRule(RiskRule):
    """观察标的限制 — 创业板/科创板/北交所仅观察不交易"""

    rule_name = "observe_only"
    category = RiskCategory.BLACKLIST
    enabled = True
    priority = 12  # 仅次于黑名单

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.action != "buy":
            return self.pass_decision(ctx)

        if ctx.board_tag == "observe_only":
            # observe_only也可能来自身份冲突或人工限制，不能把主板误称为观察板。
            # 前缀仅用于说明文案，不覆盖ctx的限制、不产生买入许可。
            from app.core.stock_tagger import stock_tagger
            board_label = {"gem": "创业板", "star": "科创板", "bse": "北交所"}.get(
                stock_tagger.get_board_type(ctx.code)
            )
            reason = f"{board_label}准入限制" if board_label else "证券身份待核验或观察限制"
            return self.block_decision(
                ctx,
                message=f"{ctx.code} 当前为仅观察状态（{reason}），不允许买入",
                suggestion="核查板块准入、证券身份一致性及人工观察限制；不得绕过身份风控",
                detail={"board_tag": ctx.board_tag},
            )

        if ctx.board_tag == "blocked":
            return self.block_decision(
                ctx,
                message=f"{ctx.code} 为禁止标的(ST/退市)，不允许买入",
                suggestion="该标的已被标记为禁止交易",
                detail={"board_tag": ctx.board_tag},
            )

        if ctx.board_tag == "suspended":
            return self.block_decision(
                ctx,
                message=f"{ctx.code} 停牌中，无法买入",
                suggestion="等待复牌",
                detail={"board_tag": ctx.board_tag},
            )

        return self.pass_decision(ctx)


# ========== 注册所有规则 ==========

def register_all_rules():
    """注册所有风控规则到引擎"""
    from app.risk.engine import risk_engine

    rules = [
        BlacklistRule(),                  # 10
        ObserveOnlyRule(),                # 12
        SentimentCircuitBreakerRule(),    # 15
        StopLossRule(),                   # 20
        MaxDrawdownRule(),                # 25
        PositionLimitRule(),              # 30
        TotalPositionRule(),              # 35
        LockupWarningRule(),              # 40
        ConcentrationRule(),              # 50
        TimeStopRule(),                   # 60
    ]

    for rule in rules:
        risk_engine.register(rule)

    logger = __import__("loguru").logger
    logger.info(f"风控规则链注册完成: {len(rules)}条规则")


# 延迟注册(避免循环导入)
import loguru
logger = loguru.logger
