"""风控规则链引擎 — Pipeline模式，可组合的风控规则

设计:
- 每个规则(Rule)是独立的风控检查单元
- 规则链(Chain)按优先级依次执行
- 任何一个规则触发BLOCK→立即返回拦截
- 规则返回RiskDecision(pass/warn/block)
- 支持规则开关、参数动态调整
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from loguru import logger


# ========== 数据结构 ==========

class RiskLevel(str, Enum):
    """风控等级"""
    PASS = "pass"           # 通过
    WARN = "warn"           # 警告(可继续但需注意)
    BLOCK = "block"         # 拦截(不可交易)


class RiskCategory(str, Enum):
    """风控规则分类"""
    STOP_LOSS = "stop_loss"               # 止损
    POSITION = "position"                 # 仓位控制
    DRAWDOWN = "drawdown"                 # 回撤控制
    BLACKLIST = "blacklist"               # 黑名单
    LOCKUP = "lockup"                     # 解禁预警
    SENTIMENT = "sentiment"               # 情绪熔断
    CONCENTRATION = "concentration"       # 集中度
    TIME = "time"                         # 时间规则
    ENGINE = "engine"                     # 风控链自身不可用/契约异常


@dataclass
class RiskDecision:
    """风控决策"""
    rule_name: str
    category: RiskCategory
    level: RiskLevel
    message: str = ""
    detail: dict = field(default_factory=dict)
    suggestion: str = ""         # 建议操作


@dataclass
class RiskContext:
    """风控上下文 — 包含本次风控检查的全部信息"""
    # 交易信息
    code: str = ""                       # 股票代码
    name: str = ""                       # 股票名称
    action: str = "buy"                  # buy/sell/hold
    price: float = 0                     # 交易价格
    amount: int = 0                      # 交易数量(股)

    # 账户信息
    total_assets: float = 0              # 总资产
    cash: float = 0                      # 可用资金
    position_value: float = 0            # 持仓市值
    current_positions: dict = field(default_factory=dict)  # {code: {shares, cost, market_value}}
    max_drawdown: float = 0              # 当前回撤%
    is_paper_experiment: bool = False     # 仅可信模拟账户边界设置，非订单API参数
    sentiment_required: bool = True      # 策略是否需要市场情绪/资金质量作为入场证据
    is_drawdown_recovery_probe: bool = False  # 仅内部模拟盘高分恢复单可设置
    drawdown_recovery_limit_pct: float = 0    # 恢复试错硬上限%；0表示内部恢复单不设永久上限

    # 个股信息
    board_tag: str = "tradeable"         # tradeable/observe_only/blocked/suspended
    is_st: bool = False
    is_suspended: bool = False
    is_delisting: bool = False
    is_ipo_recent: bool = False

    # 市场信息
    sentiment_cycle: str = "recovery"    # freezing/recovery/divergence/climax
    sentiment_score: float = 50.0         # 统一0~100评分
    sentiment_quality_status: str = "unchecked"  # ok/missing/degraded/stale/unchecked
    sentiment_quality_reason: str = ""
    market_drawdown: float = 0           # 大盘回撤%

    # 解禁信息
    has_lockup_soon: bool = False        # 近期是否有解禁
    lockup_ratio: float = 0              # 解禁占流通比%

    # 行业集中度
    sector_exposure: dict = field(default_factory=dict)  # {sector: weight%}

    # 持仓天数(卖出时)
    hold_days: int = 0
    profit_pct: float = 0               # 当前盈亏%


# ========== 风控规则基类 ==========

class RiskRule:
    """风控规则基类"""

    rule_name: str = ""
    category: RiskCategory = RiskCategory.STOP_LOSS
    enabled: bool = True
    priority: int = 100                  # 优先级，越小越先执行

    def check(self, ctx: RiskContext) -> RiskDecision:
        """检查规则，返回决策"""
        raise NotImplementedError

    def pass_decision(self, ctx: RiskContext) -> RiskDecision:
        """快捷: 通过"""
        return RiskDecision(
            rule_name=self.rule_name,
            category=self.category,
            level=RiskLevel.PASS,
        )

    def warn_decision(self, ctx: RiskContext, message: str,
                       suggestion: str = "", detail: dict = None) -> RiskDecision:
        """快捷: 警告"""
        return RiskDecision(
            rule_name=self.rule_name,
            category=self.category,
            level=RiskLevel.WARN,
            message=message,
            detail=detail or {},
            suggestion=suggestion,
        )

    def block_decision(self, ctx: RiskContext, message: str,
                        suggestion: str = "", detail: dict = None) -> RiskDecision:
        """快捷: 拦截"""
        return RiskDecision(
            rule_name=self.rule_name,
            category=self.category,
            level=RiskLevel.BLOCK,
            message=message,
            detail=detail or {},
            suggestion=suggestion,
        )


# ========== 风控引擎 ==========

class RiskEngine:
    """风控引擎 — 管理规则链并执行风控检查

    使用方式:
    1. 注册规则(自动按priority排序)
    2. 调用check()执行完整规则链
    3. 获取决策结果
    """

    def __init__(self):
        self._rules: list[RiskRule] = []

    def register(self, rule: RiskRule):
        """注册规则"""
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority)
        logger.debug(f"风控规则注册: {rule.rule_name} (优先级={rule.priority})")

    def unregister(self, rule_name: str):
        """移除规则"""
        self._rules = [r for r in self._rules if r.rule_name != rule_name]

    def get_rules(self) -> list[dict]:
        """获取所有规则"""
        return [
            {
                "name": r.rule_name,
                "category": r.category.value,
                "enabled": r.enabled,
                "priority": r.priority,
            }
            for r in self._rules
        ]

    def toggle_rule(self, rule_name: str, enabled: bool):
        """开关规则"""
        for r in self._rules:
            if r.rule_name == rule_name:
                r.enabled = enabled
                logger.info(f"风控规则 {rule_name}: {'启用' if enabled else '禁用'}")
                return

    def check(self, ctx: RiskContext) -> dict:
        """执行完整规则链

        Returns:
            {
                "final_level": "pass"/"warn"/"block",
                "decisions": [...],
                "block_reasons": [...],
                "warnings": [...],
                "suggestions": [...],
            }
        """
        decisions = []
        block_reasons = []
        warnings = []
        suggestions = []
        evaluation_errors = []
        checked_rule_count = 0

        if not isinstance(ctx.action, str) or ctx.action not in {"buy", "sell", "hold"}:
            evaluation_errors.append({
                "rule": "risk_context", "reason_code": "invalid_risk_action",
                "error_type": "RiskContextInvalid",
            })
            decision = RiskDecision(
                rule_name="risk_context", category=RiskCategory.ENGINE, level=RiskLevel.BLOCK,
                message="风控交易方向无效，不能跳过买卖规则",
                suggestion="使用明确的buy/sell/hold后重新评估",
            )
            decisions.append(decision)
            block_reasons.append({
                "rule": decision.rule_name, "category": decision.category.value,
                "message": decision.message, "suggestion": decision.suggestion,
            })
            suggestions.append(decision.suggestion)

        for rule in self._rules:
            if not rule.enabled:
                continue

            checked_rule_count += 1
            try:
                decision = rule.check(ctx)
                # 不先append坏结果；否则最终序列化本身还会再次抛异常。
                if (
                    not isinstance(decision, RiskDecision)
                    or not isinstance(decision.level, RiskLevel)
                    or not isinstance(decision.category, RiskCategory)
                    or decision.rule_name != rule.rule_name
                    or not isinstance(decision.rule_name, str) or not decision.rule_name
                    or not isinstance(decision.message, str)
                    or not isinstance(decision.suggestion, str)
                ):
                    raise ValueError("invalid_risk_decision_contract")
                decisions.append(decision)

                if decision.level == RiskLevel.BLOCK:
                    block_reasons.append({
                        "rule": decision.rule_name,
                        "category": decision.category.value,
                        "message": decision.message,
                        "suggestion": decision.suggestion,
                    })
                    suggestions.append(decision.suggestion)

                elif decision.level == RiskLevel.WARN:
                    warnings.append({
                        "rule": decision.rule_name,
                        "category": decision.category.value,
                        "message": decision.message,
                        "suggestion": decision.suggestion,
                    })

            except Exception as e:
                logger.error(f"风控规则异常 [{rule.rule_name}]: {e}")
                message = f"风控规则{rule.rule_name}执行异常，未完成检查"
                evaluation_errors.append({
                    "rule": rule.rule_name, "error_type": type(e).__name__,
                    "reason_code": "risk_rule_evaluation_failed",
                })
                # 不把“想要退出”当成允许绕过不可用的执行检查。买/卖均失败关闭；
                # 非交易hold只警告，不产生任何执行授权。
                level = RiskLevel.WARN if ctx.action == "hold" else RiskLevel.BLOCK
                decision = RiskDecision(
                    rule_name=rule.rule_name, category=RiskCategory.ENGINE,
                    level=level, message=message, suggestion="检查规则异常后重新评估，不自动放行委托",
                )
                decisions.append(decision)
                entry = {
                    "rule": decision.rule_name, "category": decision.category.value,
                    "message": decision.message, "suggestion": decision.suggestion,
                }
                (warnings if level == RiskLevel.WARN else block_reasons).append(entry)
                suggestions.append(decision.suggestion)

        if not checked_rule_count:
            evaluation_errors.append({
                "rule": "risk_chain", "reason_code": "no_enabled_risk_rules",
                "error_type": "RiskChainUnavailable",
            })
            level = RiskLevel.WARN if ctx.action == "hold" else RiskLevel.BLOCK
            decision = RiskDecision(
                rule_name="risk_chain", category=RiskCategory.ENGINE, level=level,
                message="没有可执行的风控规则，不能证明委托通过检查",
                suggestion="恢复已配置风控链后重试",
            )
            decisions.append(decision)
            entry = {
                "rule": decision.rule_name, "category": decision.category.value,
                "message": decision.message, "suggestion": decision.suggestion,
            }
            (warnings if level == RiskLevel.WARN else block_reasons).append(entry)
            suggestions.append(decision.suggestion)

        # 最终决策: 有任何BLOCK则BLOCK，否则有WARN则WARN
        if block_reasons:
            final_level = RiskLevel.BLOCK.value
        elif warnings:
            final_level = RiskLevel.WARN.value
        else:
            final_level = RiskLevel.PASS.value

        return {
            "code": ctx.code,
            "action": ctx.action,
            "final_level": final_level,
            "decisions": [
                {
                    "rule": d.rule_name,
                    "category": d.category.value,
                    "level": d.level.value,
                    "message": d.message,
                    "suggestion": d.suggestion,
                }
                for d in decisions
            ],
            "block_reasons": block_reasons,
            "warnings": warnings,
            "suggestions": suggestions,
            "total_rules": len(self._rules),
            "checked_rules": checked_rule_count,
            "evaluation_status": "incomplete" if evaluation_errors else "complete",
            "evaluation_errors": evaluation_errors,
        }

    def quick_check(self, code: str, action: str = "buy",
                     price: float = 0, amount: int = 0,
                     **kwargs) -> dict:
        """快捷风控检查"""
        ctx = RiskContext(
            code=code,
            action=action,
            price=price,
            amount=amount,
            **kwargs,
        )
        return self.check(ctx)


# 全局引擎
risk_engine = RiskEngine()
