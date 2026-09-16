"""融资融券 API — 杠杆资金监控"""

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.data_date import resolve_latest_trade_date
from app.db.session import get_db
from app.models.stock import MarginData

router = APIRouter()


@router.get("/anomalies")
async def margin_anomalies(
    trade_date: Optional[str] = Query(None, description="交易日期 YYYY-MM-DD"),
    db: AsyncSession = Depends(get_db),
):
    """融资融券异动列表"""
    from app.strategy.margin import margin_analyzer

    d = date.fromisoformat(trade_date) if trade_date else None
    resolved_date = await resolve_latest_trade_date(db, MarginData.trade_date, requested=d)
    anomalies = await margin_analyzer.analyze_anomalies(db, d)

    return {
        "trade_date": str(resolved_date),
        "total": len(anomalies),
        "anomalies": [
            {
                "code": a.code,
                "name": a.name,
                "type": a.anomaly_type,
                "margin_buy": a.margin_buy,
                "margin_balance": a.margin_balance,
                "margin_change_pct": a.margin_change_pct,
                "short_balance": a.short_balance,
                "detail": a.detail,
            }
            for a in anomalies[:50]
        ],
    }


@router.get("/index")
async def margin_index(
    trade_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """市场杠杆情绪指数"""
    from app.strategy.margin import margin_analyzer

    d = date.fromisoformat(trade_date) if trade_date else None
    resolved_date = await resolve_latest_trade_date(db, MarginData.trade_date, requested=d)
    index = await margin_analyzer.calc_margin_index(db, d)

    return {
        "trade_date": str(index.trade_date or resolved_date),
        "total_margin_balance_yi": index.total_margin_balance,
        "total_short_balance_yi": index.total_short_balance,
        "margin_change_pct": index.margin_change_pct,
        "leverage_sentiment": index.leverage_sentiment,
        "sentiment_label": _sentiment_label(index.leverage_sentiment),
        "margin_top": index.margin_top_codes[:10],
        "short_top": index.short_top_codes[:10],
    }


@router.get("/{code}/detail")
async def margin_detail(
    code: str,
    trade_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """个股融资融券详情"""
    from app.strategy.margin import margin_analyzer
    from app.core.stock_tagger import stock_tagger

    d = date.fromisoformat(trade_date) if trade_date else None
    factors = await margin_analyzer.get_margin_factors(code, db, d)

    return {
        "code": code,
        "tag": stock_tagger.get_tag_label(code),
        "is_tradeable": stock_tagger.is_tradeable(code),
        "factors": factors,
    }


@router.post("/collect")
async def margin_collect(
    trade_date: Optional[str] = Query(None),
    code: Optional[str] = Query(None, description="指定个股代码"),
    db: AsyncSession = Depends(get_db),
):
    """手动触发融资融券数据采集"""
    from app.strategy.margin import margin_collector

    d = date.fromisoformat(trade_date) if trade_date else None
    end_date_str = trade_date or date.today().strftime("%Y%m%d")

    df = await margin_collector.collect_margin_data(db, code, end_date=end_date_str)

    if df.empty:
        return {"status": "no_data", "code": code}

    if d is None:
        d = date.today()
    saved = await margin_collector.save_margin_data(db, df, d)

    return {"status": "ok", "saved": saved, "trade_date": str(d)}


def _sentiment_label(sentiment: float) -> str:
    """杠杆情绪标签"""
    if sentiment >= 70:
        return "🔥 杠杆亢奋"
    elif sentiment >= 55:
        return "📈 杠杆偏多"
    elif sentiment >= 45:
        return "⚖️ 杠杆中性"
    elif sentiment >= 30:
        return "📉 杠杆偏空"
    else:
        return "❄️ 杠杆冰点"
