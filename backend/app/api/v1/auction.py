"""竞价分析 API — 9:15-9:25集合竞价异动"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.data_date import resolve_latest_trade_date
from app.db.session import get_db
from app.models.stock import AuctionData

router = APIRouter()


@router.get("/signals")
async def auction_signals(
    trade_date: Optional[str] = Query(None, description="交易日期 YYYY-MM-DD"),
    min_score: int = Query(30, ge=0, le=100, description="最低强度评分"),
    tradeable_only: bool = Query(True, description="仅可交易股票"),
    db: AsyncSession = Depends(get_db),
):
    """获取竞价异动信号"""
    from app.strategy.auction import auction_analyzer

    d = date.fromisoformat(trade_date) if trade_date else None
    resolved_date = await resolve_latest_trade_date(db, AuctionData.trade_date, requested=d)
    signals = await auction_analyzer.get_strong_auctions(db, d, min_score, tradeable_only)

    return {
        "trade_date": str(resolved_date),
        "total": len(signals),
        "signals": [
            {
                "code": s.code,
                "name": s.name,
                "auction_price": s.auction_price,
                "prev_close": s.prev_close,
                "open_change": s.open_change,
                "volume_ratio": s.volume_ratio,
                "auction_amount": s.auction_amount,
                "strength_score": s.strength_score,
                "anomalies": [a.value for a in s.anomalies],
                "is_tradeable": s.is_tradeable,
                "evidence_status": s.evidence_status,
                "tag": "✅" if s.is_tradeable else "👁️",
            }
            for s in signals[:50]
        ],
    }


@router.get("/summary")
async def auction_summary(
    trade_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """竞价分析汇总"""
    from app.strategy.auction import auction_analyzer, AuctionAnomaly

    d = date.fromisoformat(trade_date) if trade_date else None
    resolved_date = await resolve_latest_trade_date(db, AuctionData.trade_date, requested=d)
    signals = await auction_analyzer.analyze(db, d)

    # 统计
    total = len(signals)
    high_open = [s for s in signals if s.open_change > 3]
    ultra_high = [s for s in signals if s.open_change > 7]
    limit_up_bid = [s for s in signals if AuctionAnomaly.LIMIT_UP_BID in s.anomalies]
    volume_spike = [s for s in signals if AuctionAnomaly.VOLUME_SPIKE in s.anomalies]
    low_open = [s for s in signals if s.open_change < -3]
    tradeable = [s for s in signals if s.is_tradeable]

    return {
        "trade_date": str(resolved_date),
        "total_anomalies": total,
        "high_open_count": len(high_open),
        "ultra_high_open_count": len(ultra_high),
        "limit_up_bid_count": len(limit_up_bid),
        "volume_spike_count": len(volume_spike),
        "low_open_count": len(low_open),
        "tradeable_count": len(tradeable),
        "top_high_open": [
            {"code": s.code, "open_change": s.open_change, "score": s.strength_score}
            for s in sorted(high_open, key=lambda x: x.open_change, reverse=True)[:10]
        ],
    }


@router.post("/collect")
async def auction_collect(
    trade_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """手动触发竞价数据采集"""
    from app.strategy.auction import auction_scheduler

    d = date.fromisoformat(trade_date) if trade_date else None
    result = await auction_scheduler.run_auction_phase(db, d)
    return result


@router.get("/{code}/factors")
async def auction_factors(
    code: str,
    trade_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """获取个股竞价因子"""
    from app.strategy.auction import auction_analyzer

    d = date.fromisoformat(trade_date) if trade_date else None
    factors = await auction_analyzer.get_auction_factors(code, db, d)
    return {"code": code, "factors": factors}
