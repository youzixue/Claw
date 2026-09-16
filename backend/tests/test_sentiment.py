"""情绪熔断器测试"""

import pytest
from datetime import date

import app.risk.circuit_breaker as circuit_breaker_module
from app.risk.circuit_breaker import (
    SentimentCircuitBreaker, SentimentPhase, SentimentState,
)


class TestSentimentPhase:
    """情绪周期识别测试"""

    def test_freezing_phase(self):
        """冰点阶段 — 涨停极少"""
        breaker = SentimentCircuitBreaker()
        from app.models.stock import MarketSentiment

        sentiment = MarketSentiment(
            trade_date=date.today(),
            limit_up_count=10,
            limit_down_count=60,
            seal_rate=30,
            board_height=1,
            advance_decline_ratio=0.2,
        )
        phase = breaker._determine_phase(sentiment, 20)
        assert phase == SentimentPhase.FREEZING

    def test_climax_phase(self):
        """亢奋阶段 — 涨停极多+封板率高"""
        breaker = SentimentCircuitBreaker()
        from app.models.stock import MarketSentiment

        sentiment = MarketSentiment(
            trade_date=date.today(),
            limit_up_count=100,
            limit_down_count=5,
            seal_rate=85,
            board_height=10,
            advance_decline_ratio=5.0,
        )
        phase = breaker._determine_phase(sentiment, 90)
        assert phase == SentimentPhase.CLIMAX

    def test_divergence_phase(self):
        """分歧阶段 — 涨停多但封板率一般"""
        breaker = SentimentCircuitBreaker()
        from app.models.stock import MarketSentiment

        sentiment = MarketSentiment(
            trade_date=date.today(),
            limit_up_count=60,
            limit_down_count=30,
            seal_rate=55,
            board_height=4,
            advance_decline_ratio=1.5,
        )
        phase = breaker._determine_phase(sentiment, 60)
        assert phase == SentimentPhase.DIVERGENCE

    def test_recovery_phase(self):
        """修复阶段 — 中等水平"""
        breaker = SentimentCircuitBreaker()
        from app.models.stock import MarketSentiment

        sentiment = MarketSentiment(
            trade_date=date.today(),
            limit_up_count=35,
            limit_down_count=20,
            seal_rate=60,
            board_height=3,
            advance_decline_ratio=1.2,
        )
        phase = breaker._determine_phase(sentiment, 55)
        assert phase == SentimentPhase.RECOVERY

    @pytest.mark.asyncio
    async def test_persisted_snapshot_cycle_is_authoritative_for_risk_state(
        self,
        monkeypatch,
    ):
        """快照任务的统一周期不能被熔断器用另一套阈值改写。"""
        from app.models.stock import MarketSentiment

        target_date = date(2026, 9, 2)
        sentiment = MarketSentiment(
            trade_date=target_date,
            sentiment_cycle=SentimentPhase.DIVERGENCE,
            limit_up_count=35,
            limit_down_count=20,
            broken_limit_count=16,
            seal_rate=60,
            board_height=3,
            advance_decline_ratio=1.2,
            turnover_total=2.8,
            quality_status="ok",
            calculation_version="breadth_index_quality_v3",
        )

        async def resolved_date(*_args, **_kwargs):
            return target_date

        class Result:
            def scalar_one_or_none(self):
                return sentiment

        class Session:
            async def execute(self, _statement):
                return Result()

        monkeypatch.setattr(
            circuit_breaker_module,
            "resolve_latest_trade_date",
            resolved_date,
        )
        state = await SentimentCircuitBreaker().get_current_state(Session())

        assert state.phase == SentimentPhase.DIVERGENCE
        assert state.broken_limit_count == 16
        assert state.max_position_pct == 80


class TestSentimentScore:
    """情绪评分测试"""

    def test_score_range(self):
        """评分在0-100"""
        breaker = SentimentCircuitBreaker()
        from app.models.stock import MarketSentiment

        # 高亢奋
        high = MarketSentiment(
            trade_date=date.today(),
            limit_up_count=100, seal_rate=90, board_height=10,
            advance_decline_ratio=5.0,
        )
        score = breaker._calc_sentiment_score(high)
        assert 0 <= score <= 100

        # 低冰点
        low = MarketSentiment(
            trade_date=date.today(),
            limit_up_count=5, seal_rate=20, board_height=1,
            advance_decline_ratio=0.1,
        )
        score = breaker._calc_sentiment_score(low)
        assert 0 <= score <= 100

    def test_higher_limit_up_higher_score(self):
        """涨停越多评分越高"""
        breaker = SentimentCircuitBreaker()
        from app.models.stock import MarketSentiment

        low_lu = MarketSentiment(
            trade_date=date.today(),
            limit_up_count=10, seal_rate=50, board_height=2,
            advance_decline_ratio=1.0,
        )
        high_lu = MarketSentiment(
            trade_date=date.today(),
            limit_up_count=90, seal_rate=50, board_height=2,
            advance_decline_ratio=1.0,
        )
        assert breaker._calc_sentiment_score(high_lu) > breaker._calc_sentiment_score(low_lu)


class TestPositionControl:
    """仓位控制测试"""

    def test_freezing_blocks_buy(self):
        """冰点阶段禁止开仓"""
        state = SentimentState(
            phase=SentimentPhase.FREEZING,
            should_block_buy=True,
            max_position_pct=20,
        )
        assert state.should_block_buy is True
        assert state.max_position_pct == 20

    def test_climax_reduce_position(self):
        """亢奋阶段建议减仓"""
        state = SentimentState(
            phase=SentimentPhase.CLIMAX,
            should_reduce_position=True,
            max_position_pct=50,
        )
        assert state.should_reduce_position is True

    def test_recovery_no_restrictions(self):
        """修复阶段无特殊限制"""
        state = SentimentState(
            phase=SentimentPhase.RECOVERY,
            should_block_buy=False,
            should_reduce_position=False,
            max_position_pct=80,
        )
        assert state.should_block_buy is False
        assert state.should_reduce_position is False
