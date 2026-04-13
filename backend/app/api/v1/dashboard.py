"""行情总览 API — 大盘指数+情绪+大盘资金流"""

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.stock import StockDaily, LimitUpPool
from app.risk.circuit_breaker import sentiment_circuit_breaker
from app.data.sources.eastmoney_source import EastMoneySource
from app.dashboard2.service import dashboard2_service

router = APIRouter()


def _empty_index(index_code: str, name: str) -> dict:
    """空指数数据模板"""
    return {
        "index": index_code,
        "name": name,
        "price": 0,
        "change_pct": 0,
        "volume": 0,
        "amount": 0,
        "trade_date": None,
    }


# 指数代码映射 — 同时支持纯数字和带后缀格式
# StockDaily.code 可能存储: "000001" / "000001.SH" / "sh000001"
INDEX_CODE_MAP = {
    "shanghai": {"code": "000001", "name": "上证指数", "aliases": ["000001", "000001.SH", "sh000001", "SH000001"]},
    "shenzhen": {"code": "399001", "name": "深证成指", "aliases": ["399001", "399001.SZ", "sz399001", "SZ399001"]},
    "chinext":  {"code": "399006", "name": "创业板指", "aliases": ["399006", "399006.SZ", "sz399006", "SZ399006"]},
}


async def _get_latest_trade_date(db: AsyncSession) -> date | None:
    """获取三大指数在库中的最近可信交易日期"""
    result = await db.execute(
        select(func.max(StockDaily.trade_date)).where(
            StockDaily.code.in_(["000001", "399001", "399006"])
        )
    )
    return result.scalar_one_or_none()


@router.get("/overview")
async def dashboard_overview(db: AsyncSession = Depends(get_db)):
    """行情总览 — 大盘+情绪+大盘资金流"""
    latest_date = await _get_latest_trade_date(db)
    trade_date = latest_date or date.today()

    # 优先使用情绪表最近日期作为总览口径，确保指数/情绪/资金流尽量对齐到最新交易快照
    sentiment_state = await sentiment_circuit_breaker.get_current_state(db, trade_date)
    sentiment_trade_date = getattr(sentiment_state, "trade_date", None) or trade_date

    # === 大盘指数 ===
    indices = {}
    for key, info in INDEX_CODE_MAP.items():
        idx_data = _empty_index(info["code"], info["name"])

        daily = None
        for alias in info["aliases"]:
            result = await db.execute(
                select(StockDaily)
                .where(
                    StockDaily.code == alias,
                    StockDaily.trade_date <= sentiment_trade_date,
                )
                .order_by(desc(StockDaily.trade_date))
                .limit(1)
            )
            daily = result.scalar_one_or_none()
            if daily:
                break

        if not daily:
            result = await db.execute(
                select(StockDaily)
                .where(
                    StockDaily.code.like(f"%{info['code']}%"),
                    StockDaily.trade_date <= sentiment_trade_date,
                )
                .order_by(desc(StockDaily.trade_date))
                .limit(1)
            )
            daily = result.scalar_one_or_none()

        if daily:
            idx_data["price"] = daily.close or 0
            idx_data["change_pct"] = daily.change_pct or 0
            idx_data["volume"] = daily.volume or 0
            idx_data["amount"] = daily.amount or 0
            idx_data["trade_date"] = str(daily.trade_date) if daily.trade_date else None

        indices[key] = idx_data

    # === 市场情绪 ===
    # 行情总览的情绪模块保持单一口径: 全部以 market_sentiment 为准
    lu_count = sentiment_state.limit_up_count or 0

    # === 大盘资金流 ===
    market_fund_flow = {
        "trade_date": str(sentiment_trade_date) if sentiment_trade_date else None,
        "main_net_inflow": 0,
        "main_net_inflow_pct": 0,
        "super_net_inflow": 0,
        "large_net_inflow": 0,
        "mid_net_inflow": 0,
        "small_net_inflow": 0,
    }
    try:
        em = EastMoneySource()
        df = await em.get_market_fund_flow()
        if df is not None and len(df) > 0:
            row = None
            target_date = str(sentiment_trade_date) if sentiment_trade_date else None
            if target_date and "日期" in df.columns:
                matched = df[df["日期"].astype(str) == target_date]
                if len(matched) > 0:
                    row = matched.iloc[-1]
            if row is None:
                row = df.iloc[-1]
            market_fund_flow = {
                "trade_date": str(row.get("日期") or "") or None,
                "main_net_inflow": float(row.get("主力净流入-净额") or 0),
                "main_net_inflow_pct": float(row.get("主力净流入-净占比") or 0),
                "super_net_inflow": float(row.get("超大单净流入-净额") or 0),
                "large_net_inflow": float(row.get("大单净流入-净额") or 0),
                "mid_net_inflow": float(row.get("中单净流入-净额") or 0),
                "small_net_inflow": float(row.get("小单净流入-净额") or 0),
            }
    except Exception:
        pass

    return {
        "market": indices,
        "sentiment": {
            "trade_date": str(sentiment_trade_date) if sentiment_trade_date else None,
            "cycle": sentiment_state.phase,
            "score": sentiment_state.score,
            "limit_up_count": lu_count,
            "limit_down_count": sentiment_state.limit_down_count,
            "seal_rate": sentiment_state.seal_rate,
            "board_height": sentiment_state.board_height,
            "max_position_pct": sentiment_state.max_position_pct,
            "should_block_buy": sentiment_state.should_block_buy,
        },
        "market_fund_flow": market_fund_flow,
    }


@router.get("/overview-v2")
async def dashboard_overview_v2():
    """总览 2.0 聚合接口"""
    return dashboard2_service.build_snapshot().model_dump()
