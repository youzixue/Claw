"""行情总览 API — 大盘指数+情绪+大盘资金流"""

from datetime import date, datetime
import json

from fastapi import APIRouter, Depends
from loguru import logger
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.stock import StockDaily, LimitUpPool
from app.risk.circuit_breaker import sentiment_circuit_breaker
from app.data.sources.eastmoney_source import EastMoneySource
from app.dashboard2.service import dashboard2_service
from app.dashboard2.cache import dashboard_snapshot_cache

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


def _dashboard_v2_fallback_snapshot() -> dict:
    """overview-v2 兜底响应，保障前端在异常时可稳定渲染。"""
    return {
        "trade_date": None,
        "snapshot_time": None,
        "summary_text": "暂无总评数据，系统正在分析市场状态...",
        "conclusions": [],
        "focus_strips": [],
        "a_share_core": {
            "indices": [],
            "sentiment_cycle": None,
            "limit_up_count": 0,
            "limit_down_count": 0,
            "seal_rate": 0,
            "board_height": 0,
            "main_net_inflow": 0,
        },
        "external_factors": [],
        "mapping_insights": [],
    }


def _normalize_json_payload(payload: dict) -> dict:
    """确保返回 payload 为标准 JSON 数值，避免 NaN/Inf 引发 500。"""
    try:
        return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    except Exception:
        return payload


def _is_incomplete_dashboard_v2_snapshot(payload: dict) -> bool:
    """识别明显异常/陈旧快照，触发实时重建。"""
    if not payload:
        return True
    indices = ((payload.get("a_share_core") or {}).get("indices")) or []
    if not indices:
        return True
    prices = [float(item.get("price") or 0) for item in indices if isinstance(item, dict)]
    if not prices:
        return True
    if (payload.get("trade_date") in (None, "")) and all(v == 0 for v in prices):
        return True

    # 外部关键因子异常值识别：命中则强制走实时重建
    factors = payload.get("external_factors") or []
    factor_map = {
        str(x.get("key")): x for x in factors if isinstance(x, dict) and x.get("key")
    }
    rates_10y = factor_map.get("rates_us10y")
    if rates_10y:
        try:
            if float(rates_10y.get("price") or 0) <= 0:
                return True
        except Exception:
            return True

    # 外部关键指数时间过旧也触发重建，避免长期命中陈旧快照
    for k in ["us_nasdaq", "us_sp500", "china_adr", "a50"]:
        factor = factor_map.get(k)
        if not factor:
            continue
        t = str(factor.get("trade_time") or "")
        if not t:
            return True
        try:
            d = datetime.fromisoformat(t.replace("T", " ")).date()
        except Exception:
            try:
                d = date.fromisoformat(t[:10])
            except Exception:
                return True
        if (date.today() - d).days > 3:
            return True

    # 旧版快照会把情绪周期写成英文枚举，命中则强制重建
    conclusions = payload.get("conclusions") or []
    mood_card = next(
        (item for item in conclusions if isinstance(item, dict) and item.get("key") == "a_share_mood"),
        None,
    )
    mood_note = str((mood_card or {}).get("note") or "")
    if any(token in mood_note for token in ["recovery", "climax", "divergence", "freezing", "pending"]):
        return True

    return False


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
            "broken_limit_count": sentiment_state.broken_limit_count,
            "seal_rate": sentiment_state.seal_rate,
            "board_height": sentiment_state.board_height,
            "max_position_pct": sentiment_state.max_position_pct,
            "should_block_buy": sentiment_state.should_block_buy,
        },
        "market_fund_flow": market_fund_flow,
    }


@router.get("/overview-v2")
async def dashboard_overview_v2(db: AsyncSession = Depends(get_db)):
    """总览 2.0 聚合接口，优先读取后台快照"""
    try:
        cached = await dashboard_snapshot_cache.latest(db)
        if cached and not _is_incomplete_dashboard_v2_snapshot(cached):
            return cached
        if cached:
            logger.warning("overview-v2 检测到不完整缓存，切换实时重建")
    except Exception as e:
        logger.exception(f"overview-v2 读取快照失败，降级实时构建: {e}")

    try:
        snapshot = (await dashboard2_service.build_snapshot(db)).model_dump()
        return _normalize_json_payload(snapshot)
    except Exception as e:
        logger.exception(f"overview-v2 实时构建失败，返回兜底结构: {e}")
        return _dashboard_v2_fallback_snapshot()
