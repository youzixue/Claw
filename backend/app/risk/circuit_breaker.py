"""情绪熔断模块 — 市场极端情绪下自动降仓

核心能力:
- 市场情绪周期识别(冰点/修复/分歧/亢奋)
- 情绪熔断器: 极端情绪下限制交易
- 情绪评分(0-100)
- 情绪与仓位联动
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import numpy as np
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.data_date import resolve_latest_trade_date
from app.models.stock import MarketSentiment


# ========== 情绪周期 ==========

class SentimentPhase(str):
    """情绪周期"""
    FREEZING = "freezing"       # 冰点: 涨停<20, 跌停>50
    RECOVERY = "recovery"       # 修复: 涨停20-50, 开始回暖
    DIVERGENCE = "divergence"   # 分歧: 涨停50-80, 多空拉锯
    CLIMAX = "climax"           # 亢奋: 涨停>80, 封板率>80%


# ========== 情绪熔断器 ==========

@dataclass
class SentimentState:
    """情绪状态"""
    phase: str = "divergence"
    score: float = 50.0           # 情绪评分(0-100)
    trade_date: Optional[date] = None
    limit_up_count: int = 0
    limit_down_count: int = 0
    broken_limit_count: int = 0
    seal_rate: float = 0
    board_height: int = 0
    advance_decline_ratio: float = 1.0
    turnover_total: float = 0.0
    main_net_inflow: Optional[float] = None
    index_avg_change_pct: float = 0.0
    quality_status: str = "missing"
    quality_reason: str = ""
    calculation_version: str = ""
    observed_at: Optional[datetime] = None
    should_block_buy: bool = False    # 是否禁止开仓
    should_reduce_position: bool = False  # 是否建议减仓
    max_position_pct: float = 80.0    # 建议最大仓位


class SentimentCircuitBreaker:
    """情绪熔断器 — 基于市场情绪自动调整交易策略"""

    # 情绪阈值
    FREEZING_LIMIT_UP = 20          # 涨停<20=冰点
    FREEZING_SEAL_RATE = 40         # 封板率<40%=冰点
    CLIMAX_LIMIT_UP = 80            # 涨停>80=亢奋
    CLIMAX_SEAL_RATE = 80           # 封板率>80%=亢奋
    CLIMAX_BOARD_HEIGHT = 8         # 连板>8=亢奋

    # 仓位控制
    POSITION_MAP = {
        "freezing": {"max_pct": 20, "block_buy": True, "reduce": True},
        "recovery": {"max_pct": 60, "block_buy": False, "reduce": False},
        "divergence": {"max_pct": 80, "block_buy": False, "reduce": False},
        "climax": {"max_pct": 50, "block_buy": False, "reduce": True},
    }

    async def get_current_state(self, session: AsyncSession,
                                 trade_date: date = None, *, fresh: bool = False) -> SentimentState:
        """获取当前情绪状态"""
        trade_date = await resolve_latest_trade_date(
            session,
            MarketSentiment.trade_date,
            requested=trade_date,
        )

        # 从数据库读取市场情绪 — 指定日期
        result = await session.execute(
            select(MarketSentiment).where(
                MarketSentiment.trade_date == trade_date
            ).execution_options(populate_existing=fresh)
        )
        sentiment = result.scalar_one_or_none()

        # 指定日期无数据时，回退到最近有数据的交易日
        if not sentiment:
            latest_result = await session.execute(
                select(MarketSentiment)
                .order_by(MarketSentiment.trade_date.desc())
                .limit(1)
                .execution_options(populate_existing=fresh)
            )
            sentiment = latest_result.scalar_one_or_none()

        if not sentiment:
            # 完全无数据时失败关闭，绝不把缺失数据解释为修复。
            return SentimentState(
                phase=SentimentPhase.DIVERGENCE,
                score=0.0,
                trade_date=None,
                quality_status="missing",
                quality_reason="缺少市场情绪快照",
                should_block_buy=True,
                should_reduce_position=False,
                max_position_pct=0.0,
            )

        # 计算情绪评分
        persisted_score = getattr(sentiment, "sentiment_score", None)
        score = max(
            0.0,
            min(
                100.0,
                (
                    float(persisted_score)
                    if persisted_score is not None
                    else self._calc_sentiment_score(sentiment)
                ),
            ),
        )

        # MarketSentiment.sentiment_cycle 由统一快照任务结合涨停、炸板、
        # 跌停和资金流计算，是系统唯一周期口径。这里只在旧数据缺失或非法时
        # 回退到兼容算法，避免同一时刻看板与熔断器给出不同阶段。
        persisted_phase = str(sentiment.sentiment_cycle or "").strip().lower()
        phase = (
            persisted_phase
            if persisted_phase in self.POSITION_MAP
            else self._determine_phase(sentiment, score)
        )

        # 仓位建议
        pos_config = dict(self.POSITION_MAP.get(phase, self.POSITION_MAP["divergence"]))
        quality_status = str(getattr(sentiment, "quality_status", "") or "missing")
        quality_reason = str(getattr(sentiment, "quality_reason", "") or "")
        turnover_total = float(getattr(sentiment, "turnover_total", 0) or 0)
        index_avg_change_pct = float(
            getattr(sentiment, "index_avg_change_pct", 0) or 0
        )
        ad_ratio = float(sentiment.advance_decline_ratio or 0)
        if quality_status != "ok" or turnover_total <= 0:
            pos_config.update({"max_pct": 0.0, "block_buy": True})
            if not quality_reason:
                quality_reason = "情绪数据质量降级或成交额缺失"
        elif ad_ratio < 0.5 or index_avg_change_pct <= -1.0:
            pos_config["max_pct"] = min(float(pos_config["max_pct"]), 30.0)

        return SentimentState(
            phase=phase,
            score=round(score, 1),
            trade_date=sentiment.trade_date,
            limit_up_count=sentiment.limit_up_count or 0,
            limit_down_count=sentiment.limit_down_count or 0,
            broken_limit_count=sentiment.broken_limit_count or 0,
            seal_rate=sentiment.seal_rate or 0,
            board_height=sentiment.board_height or 0,
            advance_decline_ratio=ad_ratio,
            turnover_total=turnover_total,
            main_net_inflow=(float(sentiment.main_net_inflow)
                             if sentiment.main_net_inflow is not None
                             and np.isfinite(sentiment.main_net_inflow) else None),
            index_avg_change_pct=index_avg_change_pct,
            quality_status=quality_status,
            quality_reason=quality_reason,
            calculation_version=str(getattr(sentiment, "calculation_version", "") or ""),
            observed_at=getattr(sentiment, "observed_at", None),
            should_block_buy=pos_config["block_buy"],
            should_reduce_position=pos_config["reduce"],
            max_position_pct=pos_config["max_pct"],
        )

    def _calc_sentiment_score(self, sentiment: MarketSentiment) -> float:
        """计算情绪评分(0-100)

        0=极度冰点, 50=中性, 100=极度亢奋
        """
        score = 50.0  # 基准

        # 涨停家数贡献(-20 ~ +20)
        limit_up = sentiment.limit_up_count or 0
        if limit_up >= 80:
            score += 20
        elif limit_up >= 50:
            score += 10
        elif limit_up >= 20:
            score += 0
        else:
            score -= 20

        # 封板率贡献(-15 ~ +15)
        seal_rate = sentiment.seal_rate or 50
        score += (seal_rate - 50) * 0.3

        # 连板高度贡献(-10 ~ +10)
        board_height = sentiment.board_height or 1
        if board_height >= 8:
            score += 10
        elif board_height >= 5:
            score += 5
        elif board_height <= 2:
            score -= 10

        # 涨跌比贡献(-10 ~ +10)
        ad_ratio = sentiment.advance_decline_ratio or 1.0
        if ad_ratio >= 3:
            score += 10
        elif ad_ratio >= 1.5:
            score += 5
        elif ad_ratio <= 0.3:
            score -= 10
        elif ad_ratio <= 0.5:
            score -= 5

        return max(0, min(100, score))

    def _determine_phase(self, sentiment: MarketSentiment,
                          score: float) -> str:
        """判断情绪周期"""
        limit_up = sentiment.limit_up_count or 0
        seal_rate = sentiment.seal_rate or 50
        board_height = sentiment.board_height or 1

        # 冰点: 涨停极少+封板率低
        if (limit_up < self.FREEZING_LIMIT_UP
                or seal_rate < self.FREEZING_SEAL_RATE and limit_up < 30):
            return SentimentPhase.FREEZING

        # 亢奋: 涨停极多+封板率高+高连板
        if (limit_up >= self.CLIMAX_LIMIT_UP
                and seal_rate >= self.CLIMAX_SEAL_RATE):
            return SentimentPhase.CLIMAX

        if board_height >= self.CLIMAX_BOARD_HEIGHT:
            return SentimentPhase.CLIMAX

        # 分歧: 涨停较多但封板率一般
        if limit_up >= 50 and seal_rate < 70:
            return SentimentPhase.DIVERGENCE

        # 修复: 中等涨停+封板率回升
        return SentimentPhase.RECOVERY

    async def get_position_advice(self, session: AsyncSession,
                                   trade_date: date = None) -> dict:
        """获取仓位建议"""
        state = await self.get_current_state(session, trade_date)

        return {
            "phase": state.phase,
            "score": state.score,
            "limit_up_count": state.limit_up_count,
            "broken_limit_count": state.broken_limit_count,
            "seal_rate": state.seal_rate,
            "board_height": state.board_height,
            "ad_ratio": state.advance_decline_ratio,
            "turnover_total": state.turnover_total,
            "main_net_inflow": state.main_net_inflow,
            "index_avg_change_pct": state.index_avg_change_pct,
            "quality_status": state.quality_status,
            "quality_reason": state.quality_reason,
            "calculation_version": state.calculation_version,
            "max_position_pct": state.max_position_pct,
            "should_block_buy": state.should_block_buy,
            "should_reduce_position": state.should_reduce_position,
            "advice": self._get_advice_text(state),
        }

    def _get_advice_text(self, state: SentimentState) -> str:
        """生成仓位建议文案"""
        if state.quality_status != "ok":
            return (
                f"⛔ 市场数据质量{state.quality_status}：{state.quality_reason or '关键字段缺失'}。"
                "保留持仓卖出管理，禁止新开仓。"
            )
        if state.phase == "freezing":
            return (
                f"🥶 市场冰点(情绪{state.score}分)，涨停仅{state.limit_up_count}只。"
                f"建议仓位≤{state.max_position_pct}%，禁止追高开仓，以观望为主。"
            )
        elif state.phase == "recovery":
            return (
                f"🌱 市场修复中(情绪{state.score}分)，涨停{state.limit_up_count}只。"
                f"可适度参与，建议仓位≤{state.max_position_pct}%。"
            )
        elif state.phase == "divergence":
            return (
                f"⚡ 市场分歧(情绪{state.score}分)，涨停{state.limit_up_count}只，封板率{state.seal_rate}%。"
                f"短线活跃，建议仓位≤{state.max_position_pct}%，注意去弱留强。"
            )
        elif state.phase == "climax":
            return (
                f"🔥 市场亢奋(情绪{state.score}分)，涨停{state.limit_up_count}只，"
                f"最高{state.board_height}连板！"
                f"建议仓位≤{state.max_position_pct}%，警惕见顶，不追高。"
            )
        return "市场情绪中性，可正常交易。"

    async def get_history(self, session: AsyncSession,
                           days: int = 30) -> list[dict]:
        """获取情绪历史"""
        result = await session.execute(
            select(MarketSentiment).order_by(
                MarketSentiment.trade_date.desc()
            ).limit(days)
        )
        records = result.scalars().all()

        return [
            {
                "trade_date": str(r.trade_date),
                "sentiment_cycle": r.sentiment_cycle,
                "limit_up_count": r.limit_up_count,
                "limit_down_count": r.limit_down_count,
                "seal_rate": r.seal_rate,
                "board_height": r.board_height,
                "advance_decline_ratio": r.advance_decline_ratio,
                "turnover_total": r.turnover_total,
                "main_net_inflow": r.main_net_inflow,
                "sentiment_score": getattr(r, "sentiment_score", None),
                "quality_status": getattr(r, "quality_status", None),
                "quality_reason": getattr(r, "quality_reason", None),
                "breadth_sample_count": getattr(r, "breadth_sample_count", None),
                "breadth_coverage": getattr(r, "breadth_coverage", None),
                "index_avg_change_pct": getattr(r, "index_avg_change_pct", None),
                "calculation_version": getattr(r, "calculation_version", None),
            }
            for r in records
        ]


# 全局
sentiment_circuit_breaker = SentimentCircuitBreaker()
