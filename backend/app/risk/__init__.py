"""风控中心 — 规则链引擎 + 解禁预警 + 情绪熔断

核心能力:
- 规则链引擎: 可组合的风控规则链(Pipeline模式)
- 止损规则: 个股止损/跟踪止损/时间止损
- 仓位控制: 单股仓位上限/总仓位限制/行业集中度
- 最大回撤: 账户级回撤熔断
- 黑名单过滤: ST/停牌/退市/解禁股自动拦截
- 解禁预警: 限售股解禁日历+风险评级
- 情绪熔断: 市场极端情绪下自动降仓
"""

from app.risk.engine import risk_engine, RiskEngine
from app.risk.rules import *
from app.risk.lockup import lockup_manager
from app.risk.circuit_breaker import sentiment_circuit_breaker

__all__ = [
    "risk_engine",
    "RiskEngine",
    "lockup_manager",
    "sentiment_circuit_breaker",
]
