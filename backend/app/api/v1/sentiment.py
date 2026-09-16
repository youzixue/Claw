"""情绪面 API — 市场情绪周期+统计+融资融券"""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.data_date import resolve_latest_trade_date_any
from app.db.session import get_db
from app.models.stock import (
    BrokenLimitPool,
    LimitDownPool,
    LimitUpPool,
    MarginData,
    MarketSentiment,
    StockTag,
)
from app.risk.circuit_breaker import sentiment_circuit_breaker

router = APIRouter()


@router.get("/cycle")
async def sentiment_cycle(db: AsyncSession = Depends(get_db)):
    """市场情绪周期 — 当前阶段+评分+仓位建议"""
    state = await sentiment_circuit_breaker.get_current_state(db)
    advice = await sentiment_circuit_breaker.get_position_advice(db)

    return {
        "cycle": state.phase,
        "score": state.score,
        "limit_up_count": state.limit_up_count,
        "limit_down_count": state.limit_down_count,
        "seal_rate": state.seal_rate,
        "board_height": state.board_height,
        "advance_decline_ratio": state.advance_decline_ratio,
        "turnover_total": state.turnover_total,
        "turnover_unit": "trillion_cny",
        "main_net_inflow": state.main_net_inflow,
        "index_avg_change_pct": state.index_avg_change_pct,
        "quality_status": state.quality_status,
        "quality_reason": state.quality_reason,
        "calculation_version": state.calculation_version,
        "should_block_buy": state.should_block_buy,
        "should_reduce_position": state.should_reduce_position,
        "max_position_pct": state.max_position_pct,
        "suggestion": advice.get("advice", ""),
        "position_advice": f"建议仓位≤{state.max_position_pct}%",
    }


@router.get("/stats")
async def sentiment_stats(
    trade_date: str = None,
    db: AsyncSession = Depends(get_db),
):
    """涨跌统计 — 涨停/跌停/炸板/封板率等"""
    if trade_date:
        q_date = date.fromisoformat(trade_date)
    else:
        q_date = await resolve_latest_trade_date_any(
            db,
            [
                LimitUpPool.trade_date,
                LimitDownPool.trade_date,
                MarketSentiment.trade_date,
            ],
        )

    # 涨停统计
    limit_up_result = await db.execute(
        select(func.count(LimitUpPool.id)).where(LimitUpPool.trade_date == q_date)
    )
    limit_up_count = limit_up_result.scalar() or 0

    # 跌停统计
    limit_down_result = await db.execute(
        select(func.count(LimitDownPool.id)).where(LimitDownPool.trade_date == q_date)
    )
    limit_down_count = limit_down_result.scalar() or 0

    pool_rows = (
        await db.execute(
            select(
                LimitUpPool.code,
                StockTag.board_tag,
                StockTag.board_type,
                StockTag.is_st,
            )
            .outerjoin(StockTag, StockTag.code == LimitUpPool.code)
            .where(LimitUpPool.trade_date == q_date)
        )
    ).all()
    pool_scope = {
        "tradeable": 0,
        "observe_only": 0,
        "bse": 0,
        "st": 0,
        "unknown": 0,
    }
    for _code, board_tag, board_type, is_st in pool_rows:
        if is_st:
            pool_scope["st"] += 1
        elif board_type == "bse":
            pool_scope["bse"] += 1
        elif board_tag == "tradeable":
            pool_scope["tradeable"] += 1
        elif board_tag == "observe_only":
            pool_scope["observe_only"] += 1
        else:
            pool_scope["unknown"] += 1
    overlap_count = int(
        await db.scalar(
            select(func.count())
            .select_from(LimitUpPool)
            .join(
                BrokenLimitPool,
                (BrokenLimitPool.code == LimitUpPool.code)
                & (BrokenLimitPool.trade_date == LimitUpPool.trade_date),
            )
            .where(LimitUpPool.trade_date == q_date)
        )
        or 0
    )

    # 情绪数据
    sentiment_result = await db.execute(
        select(MarketSentiment).where(MarketSentiment.trade_date == q_date)
    )
    sentiment = sentiment_result.scalar_one_or_none()

    if sentiment:
        stats = {
            "trade_date": str(q_date),
            "limit_up_count": sentiment.limit_up_count or limit_up_count,
            "limit_down_count": sentiment.limit_down_count or limit_down_count,
            "broken_limit_count": sentiment.broken_limit_count or 0,
            "seal_rate": sentiment.seal_rate or 0,
            "board_height": sentiment.board_height or 0,
            "advance_decline_ratio": sentiment.advance_decline_ratio or 1.0,
            "turnover_total": sentiment.turnover_total or 0,
            "main_net_inflow": sentiment.main_net_inflow,
            "sentiment_cycle": sentiment.sentiment_cycle or "pending",
            "sentiment_score": getattr(sentiment, "sentiment_score", None),
            "quality_status": getattr(sentiment, "quality_status", None) or "missing",
            "quality_reason": getattr(sentiment, "quality_reason", None) or "",
            "breadth_sample_count": getattr(sentiment, "breadth_sample_count", None) or 0,
            "breadth_coverage": getattr(sentiment, "breadth_coverage", None) or 0,
            "index_avg_change_pct": getattr(sentiment, "index_avg_change_pct", None),
            "calculation_version": getattr(sentiment, "calculation_version", None),
        }
    else:
        stats = {
            "trade_date": str(q_date),
            "limit_up_count": limit_up_count,
            "limit_down_count": limit_down_count,
            "broken_limit_count": 0,
            "seal_rate": 0,
            "board_height": 0,
            "advance_decline_ratio": 1.0,
            "turnover_total": 0,
            "main_net_inflow": None,
            "sentiment_cycle": "pending",
            "sentiment_score": None,
            "quality_status": "missing",
            "quality_reason": "缺少市场情绪快照",
            "breadth_sample_count": 0,
            "breadth_coverage": 0,
            "index_avg_change_pct": None,
            "calculation_version": None,
        }

    stats["turnover_unit"] = "trillion_cny"
    stats["limit_pool_scope"] = pool_scope
    stats["limit_broken_overlap_count"] = overlap_count

    # 近30日情绪趋势
    history = await sentiment_circuit_breaker.get_history(db, days=30)
    stats["trend"] = history

    return {"stats": stats}


@router.get("/margin")
async def margin_overview(
    days: int = Query(default=10, ge=1, le=60),
    db: AsyncSession = Depends(get_db),
):
    """融资融券概览 — 近N日大盘融资融券趋势"""
    # 取最近的融资融券数据
    result = await db.execute(
        select(MarginData)
        .order_by(MarginData.trade_date.desc())
        .limit(days * 100)  # 多取一些，按日期去重
    )
    records = result.scalars().all()

    # 按日期聚合
    daily_map: dict[str, dict] = {}
    for r in records:
        d = str(r.trade_date)
        if d not in daily_map:
            daily_map[d] = {
                "trade_date": d,
                "margin_buy": 0,
                "margin_balance": 0,
                "short_balance": 0,
                "total_balance": 0,
                "count": 0,
            }
        daily_map[d]["margin_buy"] += r.margin_buy or 0
        daily_map[d]["margin_balance"] += r.margin_balance or 0
        daily_map[d]["short_balance"] += r.short_balance or 0
        daily_map[d]["total_balance"] += r.total_balance or 0
        daily_map[d]["count"] += 1

    trend = sorted(daily_map.values(), key=lambda x: x["trade_date"], reverse=True)[:days]

    return {
        "margin": {
            "total_balance": trend[0]["total_balance"] if trend else 0,
            "margin_buy_today": trend[0]["margin_buy"] if trend else 0,
            "stock_count": trend[0]["count"] if trend else 0,
            "trend": trend,
        }
    }
