"""个股详情 API — 概况/因子/资金流/新闻/综合评分"""

import re
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.stock import (
    StockDaily, FundFlow, StockTag, StockSectorMapping,
    SectorInfo, LimitUpPool, MarginData, StockSpot,
)
from app.models.factor import FactorValue
from app.models.signal import SignalPerformance
from app.factors import factor_engine
from app.news.engine import news_engine
from app.core.stock_tagger import stock_tagger

router = APIRouter()


def _normalize_stock_keyword(keyword: str) -> str:
    """兼容 600519、sh600519、600519.SH 等常见输入。"""
    compact = re.sub(r"\s+", "", keyword or "")
    prefixed = re.fullmatch(r"(?:sh|sz|bj)(\d{6})", compact, flags=re.IGNORECASE)
    if prefixed:
        return prefixed.group(1)
    suffixed = re.fullmatch(r"(\d{6})\.(?:sh|sz|bj)", compact, flags=re.IGNORECASE)
    if suffixed:
        return suffixed.group(1)
    return compact


def _escape_like(keyword: str) -> str:
    return keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/search")
async def search_stocks(
    keyword: str = Query(..., min_length=1, max_length=30, description="股票代码或名称"),
    limit: int = Query(default=12, ge=1, le=30),
    db: AsyncSession = Depends(get_db),
):
    """按股票代码或名称搜索，用于个股中心跳转。"""
    normalized = _normalize_stock_keyword(keyword)
    if not normalized:
        return {"keyword": "", "stocks": [], "count": 0}

    escaped = _escape_like(normalized)
    contains_pattern = f"%{escaped}%"
    prefix_pattern = f"{escaped}%"
    match_rank = case(
        (StockTag.code == normalized, 0),
        (StockTag.name == normalized, 1),
        (StockTag.code.ilike(prefix_pattern, escape="\\"), 2),
        (StockTag.name.ilike(prefix_pattern, escape="\\"), 3),
        else_=4,
    )

    result = await db.execute(
        select(StockTag, StockSpot)
        .outerjoin(StockSpot, StockSpot.code == StockTag.code)
        .where(
            or_(
                StockTag.code.ilike(contains_pattern, escape="\\"),
                StockTag.name.ilike(contains_pattern, escape="\\"),
            )
        )
        .order_by(match_rank, StockTag.code.asc())
        .limit(limit)
    )

    stocks = []
    for tag, spot in result.all():
        stocks.append({
            "code": tag.code,
            "name": tag.name or (spot.name if spot else ""),
            "board_type": tag.board_type,
            "board_tag": tag.board_tag,
            "is_st": bool(tag.is_st),
            "is_suspended": bool(tag.is_suspended),
            "is_delisting": bool(tag.is_delisting),
            "price": spot.price if spot else None,
            "change_pct": spot.change_pct if spot else None,
            "updated_at": spot.updated_at.isoformat(sep=" ") if spot and spot.updated_at else None,
        })

    return {
        "keyword": normalized,
        "stocks": stocks,
        "count": len(stocks),
    }


@router.get("/{code}/profile")
async def stock_profile(code: str, db: AsyncSession = Depends(get_db)):
    """个股概况 — 基本面+最新行情+板块归属+标记"""
    # 最新行情
    daily_result = await db.execute(
        select(StockDaily)
        .where(StockDaily.code == code)
        .order_by(desc(StockDaily.trade_date))
        .limit(1)
    )
    daily = daily_result.scalar_one_or_none()

    # 标记
    tag_result = await db.execute(
        select(StockTag).where(StockTag.code == code)
    )
    tag = tag_result.scalar_one_or_none()

    # 板块归属
    sector_result = await db.execute(
        select(StockSectorMapping, SectorInfo.sector_name, SectorInfo.sector_type)
        .join(SectorInfo, StockSectorMapping.sector_code == SectorInfo.sector_code, isouter=True)
        .where(StockSectorMapping.code == code)
        .limit(20)
    )
    sector_rows = sector_result.all()

    sectors = []
    for mapping, s_name, s_type in sector_rows:
        sectors.append({
            "sector_code": mapping.sector_code,
            "sector_name": s_name or mapping.sector_name,
            "sector_type": s_type or mapping.sector_type,
            "source": mapping.source,
        })

    # 近20日涨跌幅
    recent_result = await db.execute(
        select(StockDaily.trade_date, StockDaily.close, StockDaily.change_pct,
               StockDaily.volume, StockDaily.amount, StockDaily.turnover)
        .where(StockDaily.code == code)
        .order_by(desc(StockDaily.trade_date))
        .limit(20)
    )
    recent_rows = recent_result.all()
    recent_kline = [
        {
            "date": str(r.trade_date),
            "close": r.close,
            "change_pct": r.change_pct,
            "volume": r.volume,
            "amount": r.amount,
            "turnover": r.turnover,
        }
        for r in recent_rows
    ]

    profile = {
        "code": code,
        "name": tag.name if tag else "",
        "board_type": tag.board_type if tag else "unknown",
        "board_tag": tag.board_tag if tag else "unknown",
        "is_st": tag.is_st if tag else False,
        "is_suspended": tag.is_suspended if tag else False,
        "is_delisting": tag.is_delisting if tag else False,
        "is_ipo_recent": tag.is_ipo_recent if tag else False,
        "latest_daily": {
            "trade_date": str(daily.trade_date) if daily else "",
            "open": daily.open if daily else None,
            "high": daily.high if daily else None,
            "low": daily.low if daily else None,
            "close": daily.close if daily else None,
            "volume": daily.volume if daily else None,
            "amount": daily.amount if daily else None,
            "change_pct": daily.change_pct if daily else None,
            "turnover": daily.turnover if daily else None,
        } if daily else None,
        "sectors": sectors,
        "recent_kline": recent_kline,
    }

    return {"code": code, "profile": profile}


@router.get("/{code}/factors")
async def stock_factors(
    code: str,
    trade_date: str = None,
    db: AsyncSession = Depends(get_db),
):
    """个股因子 — 最新因子值+分类汇总"""
    if trade_date:
        q_date = date.fromisoformat(trade_date)
    else:
        # 取最新有数据的日期
        latest_result = await db.execute(
            select(func.max(FactorValue.trade_date))
            .where(FactorValue.stock_code == code)
        )
        q_date = latest_result.scalar() or date.today()

    # 查因子值
    result = await db.execute(
        select(FactorValue)
        .where(FactorValue.stock_code == code, FactorValue.trade_date == q_date)
        .order_by(FactorValue.factor_name)
    )
    factor_values = result.scalars().all()

    # 按分类组织
    factors_by_category = {}
    factor_list = []
    for fv in factor_values:
        # 从因子注册表获取分类信息
        reg_factor = factor_engine.registry.get(fv.factor_name)
        category = reg_factor.category.value if reg_factor else "other"
        description = reg_factor.description if reg_factor else ""

        item = {
            "name": fv.factor_name,
            "value": fv.factor_value,
            "rank": fv.factor_rank,
            "pct": fv.factor_pct,
            "category": category,
            "description": description,
        }
        factor_list.append(item)

        if category not in factors_by_category:
            factors_by_category[category] = []
        factors_by_category[category].append(item)

    # 因子注册表信息(全部可用因子)
    all_factors_info = {}
    for name, f in factor_engine.registry.all_factors().items():
        all_factors_info[name] = {
            "category": f.category.value,
            "direction": f.direction,
            "description": f.description,
        }

    return {
        "code": code,
        "trade_date": str(q_date),
        "factors": factor_list,
        "by_category": factors_by_category,
        "total_computed": len(factor_list),
        "total_registered": factor_engine.registry.count(),
        "all_factors_info": all_factors_info,
    }


@router.get("/{code}/news")
async def stock_news(
    code: str,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """个股相关新闻"""
    from app.models.news import FinanceNews

    # 模糊匹配 related_codes 包含该股票代码
    result = await db.execute(
        select(FinanceNews)
        .where(FinanceNews.related_codes.contains(code))
        .order_by(desc(FinanceNews.publish_time))
        .limit(limit)
    )
    news_list = result.scalars().all()

    news_items = []
    for n in news_list:
        news_items.append({
            "id": n.id,
            "title": n.title,
            "source": n.source,
            "publish_time": n.publish_time.isoformat() if n.publish_time else None,
            "sentiment": n.sentiment,
            "importance": n.importance,
            "category": n.category,
            "bull_bear": n.bull_bear,
            "url": getattr(n, "url", ""),
        })

    # 如果DB无数据，尝试实时抓取
    if not news_items:
        try:
            live_items = await news_engine.fetch_by_code(code, limit=limit)
            news_items = [
                {
                    "title": item.title,
                    "source": item.source,
                    "publish_time": item.publish_time.isoformat() if item.publish_time else None,
                    "category": item.category,
                    "url": item.url,
                }
                for item in live_items[:limit]
            ]
        except Exception:
            pass

    return {"code": code, "news": news_items, "total": len(news_items)}


@router.get("/{code}/fund-flow")
async def stock_fund_flow(
    code: str,
    days: int = Query(default=20, ge=1, le=120),
    db: AsyncSession = Depends(get_db),
):
    """个股资金流 — 近N日资金流向"""
    result = await db.execute(
        select(FundFlow)
        .where(FundFlow.code == code)
        .order_by(desc(FundFlow.trade_date))
        .limit(days)
    )
    flows = result.scalars().all()

    flow_list = [
        {
            "trade_date": str(f.trade_date),
            "main_net_inflow": f.main_net_inflow,
            "main_net_inflow_pct": f.main_net_inflow_pct,
            "big_net_inflow": f.big_net_inflow,
            "mid_net_inflow": f.mid_net_inflow,
            "small_net_inflow": f.small_net_inflow,
        }
        for f in flows
    ]

    # 汇总统计
    if flows:
        total_main = sum(f.main_net_inflow or 0 for f in flows)
        positive_days = sum(1 for f in flows if (f.main_net_inflow or 0) > 0)
        summary = {
            "total_main_net_inflow": total_main,
            "avg_main_net_inflow": total_main / len(flows),
            "positive_days": positive_days,
            "negative_days": len(flows) - positive_days,
            "consecutive_inflow": _count_consecutive(flows, positive=True),
            "consecutive_outflow": _count_consecutive(flows, positive=False),
        }
    else:
        summary = {}

    return {
        "code": code,
        "fund_flow": flow_list,
        "summary": summary,
        "total": len(flow_list),
    }


@router.get("/{code}/score")
async def stock_composite_score(
    code: str,
    db: AsyncSession = Depends(get_db),
):
    """个股综合评分 — 牛股雷达评分"""
    from app.signal import BullScoreModel, DragonHeadScanner, LimitUpTracker
    from app.signal.capital_anomaly import CapitalAnomalyDetector
    from app.signal.chip_concentration import ChipConcentrationAnalyzer
    from app.signal.breakthrough import BreakthroughDetector

    # 标记检查
    tag_result = await db.execute(select(StockTag).where(StockTag.code == code))
    tag = tag_result.scalar_one_or_none()

    return {
        "code": code,
        "name": tag.name if tag else "",
        "board_tag": tag.board_tag if tag else "unknown",
        "score": None,  # 需要实时计算，暂返回框架
        "level": None,
        "message": "综合评分需因子数据支撑，请先运行因子计算",
    }


def _count_consecutive(flows: list, positive: bool) -> int:
    """计算连续净流入/流出天数"""
    count = 0
    for f in flows:
        val = f.main_net_inflow or 0
        if (positive and val > 0) or (not positive and val < 0):
            count += 1
        else:
            break
    return count
