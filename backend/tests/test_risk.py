"""风控引擎测试 — 规则链+决策逻辑"""

import pytest

from app.risk.engine import RiskEngine, RiskRule, RiskContext, RiskDecision, RiskLevel, RiskCategory
from app.risk.rules import MaxDrawdownRule, SentimentCircuitBreakerRule


class TestRiskEngine:
    """风控引擎核心测试"""

    def _make_engine_with_rules(self):
        """创建含测试规则的风控引擎"""
        engine = RiskEngine()
        engine.register(STMockRule())
        engine.register(PositionLimitRule())
        engine.register(DrawdownRule())
        return engine

    def test_register_rule(self):
        """规则注册"""
        engine = RiskEngine()
        engine.register(STMockRule())
        assert len(engine._rules) == 1

    def test_register_multiple_rules_sorted(self):
        """多规则注册后按优先级排序"""
        engine = RiskEngine()
        engine.register(PositionLimitRule())   # priority=50
        engine.register(STMockRule())           # priority=10
        engine.register(DrawdownRule())         # priority=30
        assert engine._rules[0].rule_name == "st_mock"
        assert engine._rules[1].rule_name == "drawdown"
        assert engine._rules[2].rule_name == "position_limit"

    def test_toggle_rule(self):
        """规则开关"""
        engine = RiskEngine()
        engine.register(STMockRule())
        engine.toggle_rule("st_mock", False)
        assert engine._rules[0].enabled is False
        engine.toggle_rule("st_mock", True)
        assert engine._rules[0].enabled is True

    def test_check_pass(self):
        """全部通过"""
        engine = self._make_engine_with_rules()
        ctx = RiskContext(
            code="000001", action="buy", price=10.0, amount=100,
            is_st=False, board_tag="tradeable",
            total_assets=1_000_000, cash=500_000,
            max_drawdown=5.0,
        )
        result = engine.check(ctx)
        assert result["final_level"] == "pass"

    def test_check_block_by_st(self):
        """ST股拦截"""
        engine = self._make_engine_with_rules()
        ctx = RiskContext(
            code="000001", action="buy", price=10.0, amount=100,
            is_st=True, board_tag="blocked",
        )
        result = engine.check(ctx)
        assert result["final_level"] == "block"
        assert any(b["rule"] == "st_mock" for b in result["block_reasons"])

    def test_check_warn_by_position(self):
        """仓位过大警告"""
        engine = self._make_engine_with_rules()
        # 单股市值 > 总资产50%
        ctx = RiskContext(
            code="000001", action="buy", price=10.0, amount=60000,
            is_st=False, board_tag="tradeable",
            total_assets=1_000_000, cash=500_000,
            current_positions={"000001": {"shares": 60000, "cost": 10, "market_value": 600000}},
        )
        result = engine.check(ctx)
        assert result["final_level"] in ("warn", "block")

    def test_check_block_by_drawdown(self):
        """回撤过大拦截"""
        engine = self._make_engine_with_rules()
        ctx = RiskContext(
            code="000001", action="buy", price=10.0, amount=100,
            is_st=False, board_tag="tradeable",
            max_drawdown=20.0,  # 超过15%阈值
        )
        result = engine.check(ctx)
        assert result["final_level"] == "block"

    def test_standard_drawdown_rule_still_blocks_without_recovery_marker(self):
        decision = MaxDrawdownRule().check(RiskContext(
            code="000001",
            action="buy",
            max_drawdown=15.9,
        ))

        assert decision.level == RiskLevel.BLOCK
        assert "15.0%" in decision.message

    def test_drawdown_recovery_probe_uses_explicit_twenty_percent_limit(self):
        decision = MaxDrawdownRule().check(RiskContext(
            code="000001",
            action="buy",
            max_drawdown=15.9,
            is_drawdown_recovery_probe=True,
            drawdown_recovery_limit_pct=20.0,
        ))

        assert decision.level == RiskLevel.WARN
        assert decision.detail["recovery_drawdown_limit"] == 20.0

    def test_drawdown_recovery_probe_blocks_at_hard_limit(self):
        decision = MaxDrawdownRule().check(RiskContext(
            code="000001",
            action="buy",
            max_drawdown=20.0,
            is_drawdown_recovery_probe=True,
            drawdown_recovery_limit_pct=20.0,
        ))

        assert decision.level == RiskLevel.BLOCK
        assert "恢复模式硬上限20.0%" in decision.message

    def test_internal_drawdown_recovery_probe_can_disable_permanent_limit(self):
        decision = MaxDrawdownRule().check(RiskContext(
            code="000001",
            action="buy",
            max_drawdown=25.1,
            is_drawdown_recovery_probe=True,
            drawdown_recovery_limit_pct=0,
        ))

        assert decision.level == RiskLevel.WARN
        assert decision.detail["recovery_drawdown_limit"] is None

    @pytest.mark.parametrize("quality_status", ["missing", "degraded", "stale"])
    def test_sentiment_data_quality_fail_closes_new_buys(self, quality_status):
        decision = SentimentCircuitBreakerRule().check(RiskContext(
            code="000001",
            action="buy",
            sentiment_cycle="recovery",
            sentiment_score=60,
            sentiment_quality_status=quality_status,
            sentiment_quality_reason="当日快照不可用",
        ))

        assert decision.level == RiskLevel.BLOCK
        assert decision.detail["quality_status"] == quality_status

    def test_sentiment_healthy_recovery_allows_buy(self):
        decision = SentimentCircuitBreakerRule().check(RiskContext(
            code="000001",
            action="buy",
            sentiment_cycle="recovery",
            sentiment_score=60,
            sentiment_quality_status="ok",
        ))

        assert decision.level == RiskLevel.PASS

    def test_sentiment_healthy_climax_warns_buy(self):
        decision = SentimentCircuitBreakerRule().check(RiskContext(
            code="000001",
            action="buy",
            sentiment_cycle="climax",
            sentiment_score=90,
            sentiment_quality_status="ok",
        ))

        assert decision.level == RiskLevel.WARN

    def test_sentiment_quality_never_blocks_sell(self):
        decision = SentimentCircuitBreakerRule().check(RiskContext(
            code="000001",
            action="sell",
            sentiment_cycle="freezing",
            sentiment_quality_status="missing",
        ))

        assert decision.level == RiskLevel.PASS

    def test_disabled_rule_skipped(self):
        """禁用规则不执行"""
        engine = self._make_engine_with_rules()
        engine.toggle_rule("st_mock", False)
        ctx = RiskContext(
            code="000001", action="buy", price=10.0, amount=100,
            is_st=True, board_tag="blocked",
        )
        result = engine.check(ctx)
        # ST规则被禁用，可能不会block
        assert result["final_level"] != "block" or not any(
            b["rule"] == "st_mock" for b in result["block_reasons"]
        )

    def test_quick_check(self):
        """快捷风控检查"""
        engine = self._make_engine_with_rules()
        result = engine.quick_check("000001", "buy", 10.0, 100)
        assert "final_level" in result
        assert "decisions" in result

    def test_get_rules(self):
        """获取规则列表"""
        engine = self._make_engine_with_rules()
        rules = engine.get_rules()
        assert len(rules) == 3
        for r in rules:
            assert "name" in r
            assert "category" in r
            assert "enabled" in r
            assert "priority" in r

    def test_check_all_block_over_warn(self):
        """block优先于warn"""
        engine = self._make_engine_with_rules()
        ctx = RiskContext(
            code="000001", action="buy",
            is_st=True, board_tag="blocked",
            max_drawdown=20.0,
        )
        result = engine.check(ctx)
        assert result["final_level"] == "block"


# ========== 测试用风控规则 ==========

class STMockRule(RiskRule):
    """ST/黑名单规则(测试用)"""
    rule_name = "st_mock"
    category = RiskCategory.BLACKLIST
    priority = 10

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.is_st:
            return self.block_decision(ctx, "ST股禁止买入", "不要碰ST股")
        if ctx.board_tag == "blocked":
            return self.block_decision(ctx, "黑名单股票禁止交易")
        if ctx.board_tag == "suspended":
            return self.block_decision(ctx, "停牌股无法交易")
        if ctx.is_delisting:
            return self.block_decision(ctx, "退市风险股禁止交易")
        return self.pass_decision(ctx)


class PositionLimitRule(RiskRule):
    """仓位限制规则(测试用)"""
    rule_name = "position_limit"
    category = RiskCategory.POSITION
    priority = 50

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.total_assets <= 0:
            return self.pass_decision(ctx)

        # 计算单股市值占比
        single_value = ctx.price * ctx.amount
        total_position = sum(
            p.get("market_value", 0) for p in ctx.current_positions.values()
        )
        new_total = total_position + single_value
        position_pct = new_total / ctx.total_assets * 100 if ctx.total_assets > 0 else 0

        if position_pct > 50:
            return self.block_decision(
                ctx,
                f"单股仓位{position_pct:.1f}%超过50%上限",
                "减仓至50%以下",
            )
        elif position_pct > 30:
            return self.warn_decision(
                ctx,
                f"单股仓位{position_pct:.1f}%偏高",
                "注意仓位控制",
            )
        return self.pass_decision(ctx)


class DrawdownRule(RiskRule):
    """回撤控制规则(测试用)"""
    rule_name = "drawdown"
    category = RiskCategory.DRAWDOWN
    priority = 30

    def check(self, ctx: RiskContext) -> RiskDecision:
        if ctx.max_drawdown >= 15:
            return self.block_decision(
                ctx,
                f"账户回撤{ctx.max_drawdown:.1f}%超过15%上限",
                "立即减仓，降低风险敞口",
            )
        elif ctx.max_drawdown >= 10:
            return self.warn_decision(
                ctx,
                f"账户回撤{ctx.max_drawdown:.1f}%接近上限",
                "控制仓位，谨慎操作",
            )
        return self.pass_decision(ctx)
