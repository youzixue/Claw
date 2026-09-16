"""商品联动 API — 期股/现货/产业链映射."""

import asyncio
import math
import time as _time
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.sources.base import DataSourceBase
from app.db.session import get_db
from app.models.stock import StockSpot, StockTag

router = APIRouter()

_CACHE_TTL_SECONDS = 600.0
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = asyncio.Lock()

SPOT_CATEGORIES = ["能源", "化工", "塑料", "纺织", "有色", "钢铁", "建材", "农副"]

DOMESTIC_FUTURES = [
    ("RB0", "螺纹钢", "钢铁", "钢铁 ETF", "黑色链日内价格直接影响钢铁、建材和上游原料预期"),
    ("CU0", "铜", "铜", "有色金属 ETF", "铜价日内走强通常代表工业金属景气改善"),
    ("AL0", "铝", "铝", "有色金属 ETF", "铝价日内走强利好上游资源和冶炼弹性"),
    ("ZN0", "锌", "铅锌", "有色金属 ETF", "锌价联动铅锌资源和冶炼板块"),
    ("AU0", "黄金", "贵金属", "黄金 ETF", "黄金日内走强偏避险和贵金属弹性"),
    ("AG0", "白银", "贵金属", "有色金属 ETF", "白银兼具贵金属和工业属性"),
    ("SC0", "原油", "油气开采", "原油 ETF / 能源 ETF", "原油上行利好上游油气，压制下游成本"),
    ("I0", "铁矿石", "钢铁", "钢铁 ETF", "铁矿石上行利好资源端，但抬升钢厂成本"),
    ("J0", "焦炭", "煤炭加工", "煤炭 ETF", "焦炭日内走强反映黑色链成本和需求"),
    ("JM0", "焦煤", "煤炭开采", "煤炭 ETF", "焦煤日内走强利好煤炭资源端"),
    ("TA0", "PTA", "化纤", "化工 ETF", "PTA联动化纤、涤纶和下游纺服成本"),
    ("MA0", "甲醇", "煤化工", "化工 ETF", "甲醇联动煤化工和基础化工链"),
    ("RU0", "天然橡胶", "橡胶制品", "化工 ETF", "橡胶上行利好资源端，抬升轮胎成本"),
    ("FG0", "玻璃", "玻璃玻纤", "建材 ETF", "玻璃联动地产竣工、光伏玻璃和建材链"),
    ("M0", "豆粕", "饲料", "农业 ETF", "豆粕上行抬升养殖饲料成本"),
    ("A0", "大豆", "农产品加工", "农业 ETF", "大豆上行联动油脂油料和农产品链"),
]

SPOT_SYMBOL_MAP = {
    "螺纹钢": "RB",
    "不锈钢板": "SS",
    "铁矿石(澳)": "I",
    "铁矿石": "I",
    "焦煤": "JM",
    "炼焦煤": "JM",
    "焦炭": "J",
    "燃料油": "FU",
    "原油": "SC",
    "LPG": "PG",
    "液化气": "PG",
    "硫磺": "FU",
    "乙二醇": "EG",
    "PTA": "TA",
    "甲醇": "MA",
    "天然橡胶": "RU",
    "玻璃": "FG",
    "硅铁": "SF",
    "白银": "AG",
    "黄金": "AU",
    "铜": "CU",
    "铝": "AL",
    "锌": "ZN",
    "铅": "PB",
    "镍": "NI",
    "锡": "SN",
    "大豆": "A",
    "豆粕": "M",
    "豆油": "Y",
    "棕榈油": "P",
    "玉米": "C",
    "棉花": "CF",
    "皮棉": "CF",
    "白糖": "SR",
    "瓦楞原纸": "FG",
}

FUTURES_KEYWORD_MAP = [
    ("原油", "油气开采", "原油 ETF / 能源 ETF", "油价上行利好上游资源，压制部分下游成本"),
    ("布伦特", "油气开采", "原油 ETF / 能源 ETF", "油价上行利好上游资源，压制部分下游成本"),
    ("WTI", "油气开采", "原油 ETF / 能源 ETF", "油价上行利好上游资源，压制部分下游成本"),
    ("铜", "铜", "有色金属 ETF", "铜价上行通常指向有色景气和工业需求"),
    ("铝", "铝", "有色金属 ETF", "铝价上行利好上游冶炼和资源端"),
    ("锌", "铅锌", "有色金属 ETF", "锌价上行利好铅锌资源和冶炼"),
    ("镍", "小金属", "有色金属 ETF", "镍价上行关联不锈钢和新能源材料"),
    ("黄金", "贵金属", "黄金 ETF", "黄金上行反映避险或实际利率变化"),
    ("白银", "贵金属", "有色金属 ETF", "白银上行关联贵金属和工业金属双属性"),
    ("铁矿", "钢铁", "钢铁 ETF", "铁矿上行利好资源端，但抬升钢厂成本"),
    ("螺纹", "钢铁", "钢铁 ETF", "黑色链上行反映地产基建和制造需求"),
    ("焦煤", "煤炭开采", "煤炭 ETF", "焦煤上行利好煤炭资源，压制焦化/钢铁利润"),
    ("焦炭", "煤炭加工", "煤炭 ETF", "焦炭上行反映黑色链成本和需求共振"),
    ("天然气", "燃气", "能源 ETF", "天然气上行利好燃气资源和部分替代能源"),
    ("玉米", "种植业", "农业 ETF", "农产品上行利好种植链，抬升饲料成本"),
    ("大豆", "农产品加工", "农业 ETF", "大豆上行关联油脂油料和饲料链"),
]

SOURCE_FUTURES_SPOT = "akshare:futures_zh_spot"
SOURCE_FUTURES_DAILY = "akshare:futures_zh_daily_sina"
SOURCE_SPOT_STOCK = "akshare:futures_spot_stock"
SOURCE_SPOT_PRICE_DAILY = "akshare:futures_spot_price_daily"


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(str(value).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def _split_names(value) -> list[str]:
    if not value or str(value).strip() == "-":
        return []
    return [item.strip() for item in str(value).replace("，", ",").split(",") if item.strip()]


def _daily_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None or previous == 0:
        return None
    return round((current - previous) / previous * 100, 2)


def _effective_change_fields(
    daily_change_pct: Optional[float],
    half_year_change_pct: Optional[float],
) -> tuple[Optional[float], str, str]:
    """Return the price signal used for trading interpretation."""
    if daily_change_pct is not None:
        return daily_change_pct, "日频", "今日价格"
    if half_year_change_pct is not None:
        return half_year_change_pct, "近半年", "中期趋势"
    return None, "无价格", "价格信号不足"


def _impact_fields(change_pct: Optional[float]) -> dict[str, str]:
    if change_pct is None:
        return {
            "impact_direction": "方向待确认",
            "upstream_impact": "上游待确认",
            "downstream_impact": "下游待确认",
            "trend_label": "无价格信号",
            "trend_side": "neutral",
        }
    if change_pct > 0:
        return {
            "impact_direction": "上游受益 / 下游承压",
            "upstream_impact": "利润改善",
            "downstream_impact": "成本承压",
            "trend_label": "商品上涨",
            "trend_side": "up",
        }
    if change_pct < 0:
        return {
            "impact_direction": "上游承压 / 下游受益",
            "upstream_impact": "利润承压",
            "downstream_impact": "成本改善",
            "trend_label": "商品下跌",
            "trend_side": "down",
        }
    return {
        "impact_direction": "价格持平",
        "upstream_impact": "利润中性",
        "downstream_impact": "成本中性",
        "trend_label": "商品持平",
        "trend_side": "neutral",
    }


def _stock_with_impact(stock: dict[str, Any], role: str, change_pct: Optional[float]) -> dict[str, Any]:
    if change_pct is None or change_pct == 0:
        impact_side = "neutral"
        impact_label = "观察"
    elif (role == "producer" and change_pct > 0) or (role == "downstream" and change_pct < 0):
        impact_side = "benefit"
        impact_label = "受益"
    else:
        impact_side = "pressure"
        impact_label = "承压"
    return {
        **stock,
        "role": role,
        "role_label": "上游" if role == "producer" else "下游",
        "impact_side": impact_side,
        "impact_label": impact_label,
    }


def _stock_risk_labels(stock: dict[str, Any]) -> list[str]:
    labels = []
    board_tag = stock.get("board_tag")
    if board_tag == "observe_only":
        labels.append("观察")
    elif board_tag in {"blocked", "suspended"}:
        labels.append("屏蔽")
    elif board_tag and board_tag != "tradeable":
        labels.append(str(board_tag))
    if stock.get("is_st"):
        labels.append("ST")
    if stock.get("is_suspended"):
        labels.append("停牌")
    if stock.get("is_delisting"):
        labels.append("退市")
    if stock.get("is_ipo_recent"):
        labels.append("次新")
    return labels


def _stock_tradeable(stock: dict[str, Any]) -> bool:
    return (
        stock.get("board_tag") == "tradeable"
        and not stock.get("is_st")
        and not stock.get("is_suspended")
        and not stock.get("is_delisting")
    )


def _attach_signal_fields(item: dict[str, Any]) -> dict[str, Any]:
    effective_change, signal_horizon, signal_basis = _effective_change_fields(
        item.get("daily_change_pct"),
        item.get("half_year_change_pct"),
    )
    item["effective_change_pct"] = effective_change
    item["signal_horizon"] = signal_horizon
    item["signal_basis"] = signal_basis
    item.update(_impact_fields(effective_change))

    producer_stocks = [
        _stock_with_impact(stock, "producer", effective_change)
        for stock in item.get("producer_stocks", [])
    ]
    downstream_stocks = [
        _stock_with_impact(stock, "downstream", effective_change)
        for stock in item.get("downstream_stocks", [])
    ]
    affected_stocks = [*producer_stocks, *downstream_stocks]
    item["producer_stocks"] = producer_stocks
    item["downstream_stocks"] = downstream_stocks
    item["affected_stocks"] = affected_stocks[:12]
    item["benefit_stocks"] = [stock for stock in affected_stocks if stock["impact_side"] == "benefit"][:8]
    item["pressure_stocks"] = [stock for stock in affected_stocks if stock["impact_side"] == "pressure"][:8]
    item["neutral_stocks"] = [stock for stock in affected_stocks if stock["impact_side"] == "neutral"][:8]
    return item


def _series_from_daily(rows: list[dict[str, Any]], value_field: str) -> list[dict[str, Any]]:
    series = []
    previous = None
    for item in rows:
        value = _to_float(item.get(value_field))
        change_pct = _daily_change(value, previous)
        series.append({
            "date": str(item.get("date")),
            "value": value,
            "change_pct": change_pct,
        })
        if value is not None:
            previous = value
    return series


async def _ak_call(func, *args, **kwargs):
    async with DataSourceBase._akshare_call_lock:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


def _mark_source(source_status: dict[str, str], source: str, status: str) -> None:
    previous = source_status.get(source)
    if previous == "error":
        return
    if previous == "partial" and status == "ok":
        return
    source_status[source] = status


def _warn_source(warnings: list[dict[str, str]], source: str, message: str) -> None:
    warnings.append({"source": source, "message": message})


def _finalize_source_status(source_status: dict[str, str]) -> dict[str, str]:
    return {
        source: ("skipped" if status == "pending" else status)
        for source, status in source_status.items()
    }


async def _load_spot_price_history(
    warnings: Optional[list[dict[str, str]]] = None,
    source_status: Optional[dict[str, str]] = None,
) -> dict[str, list[dict[str, Any]]]:
    import akshare as ak

    end = datetime.now().date()
    start = end - timedelta(days=16)
    try:
        df = await _ak_call(
            ak.futures_spot_price_daily,
            start_day=start.strftime("%Y%m%d"),
            end_day=end.strftime("%Y%m%d"),
        )
    except Exception as exc:
        if source_status is not None:
            _mark_source(source_status, SOURCE_SPOT_PRICE_DAILY, "error")
        if warnings is not None:
            _warn_source(warnings, SOURCE_SPOT_PRICE_DAILY, f"现货日频价格获取失败: {exc}")
        return {}
    history: dict[str, list[dict[str, Any]]] = {}
    if df is None or len(df) == 0:
        if source_status is not None:
            _mark_source(source_status, SOURCE_SPOT_PRICE_DAILY, "empty")
        return history
    if source_status is not None:
        _mark_source(source_status, SOURCE_SPOT_PRICE_DAILY, "ok")
    for _, row in df.iterrows():
        symbol = str(row.get("symbol") or "")
        if not symbol:
            continue
        history.setdefault(symbol, []).append({
            "date": str(row.get("date") or ""),
            "spot_price": _to_float(row.get("spot_price")),
            "dominant_contract": str(row.get("dominant_contract") or ""),
            "dominant_contract_price": _to_float(row.get("dominant_contract_price")),
            "basis_rate": _to_float(row.get("dom_basis_rate")),
        })
    for symbol, items in history.items():
        items.sort(key=lambda item: item["date"])
    return history


def _futures_target(name: str) -> tuple[str, str, str]:
    for keyword, sector, etf, reason in FUTURES_KEYWORD_MAP:
        if keyword.lower() in name.lower():
            return sector, etf, reason
    return "周期资源", "商品 ETF", "商品价格异动，需要结合产业链方向确认"


async def _stock_lookup(db: AsyncSession, names: list[str]) -> dict[str, dict[str, Any]]:
    unique_names = sorted({name for name in names if name})
    if not unique_names:
        return {}

    tag_result = await db.execute(
        select(
            StockTag.code,
            StockTag.name,
            StockTag.board_type,
            StockTag.board_tag,
            StockTag.is_st,
            StockTag.is_suspended,
            StockTag.is_limit_up,
            StockTag.is_limit_down,
            StockTag.is_ipo_recent,
            StockTag.is_delisting,
        ).where(StockTag.name.in_(unique_names))
    )
    tags_by_code = {}
    for row in tag_result.mappings().all():
        stock = dict(row)
        stock["risk_labels"] = _stock_risk_labels(stock)
        stock["is_tradeable"] = _stock_tradeable(stock)
        stock["trade_status_label"] = "可交易" if stock["is_tradeable"] else "观察/屏蔽"
        tags_by_code[stock["code"]] = stock
    if not tags_by_code:
        return {}

    spot_result = await db.execute(
        select(StockSpot.code, StockSpot.change_pct).where(StockSpot.code.in_(tags_by_code.keys()))
    )
    change_by_code = {code: change_pct for code, change_pct in spot_result.all()}
    return {
        stock["name"]: {
            **stock,
            "change_pct": change_by_code.get(code),
        }
        for code, stock in tags_by_code.items()
    }


def _stock_items(names: list[str], lookup: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    items = []
    for name in names:
        stock = lookup.get(name)
        if not stock or stock["code"] in seen:
            continue
        seen.add(stock["code"])
        items.append(stock)
    return items


async def _build_spot_rows(
    db: AsyncSession,
    warnings: Optional[list[dict[str, str]]] = None,
    source_status: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    import akshare as ak

    spot_history = await _load_spot_price_history(warnings, source_status)
    frames = []
    failed_categories = []
    for category in SPOT_CATEGORIES:
        try:
            df = await _ak_call(ak.futures_spot_stock, symbol=category)
            if df is not None and len(df) > 0:
                df = df.copy()
                df["__category"] = category
                frames.append(df)
        except Exception as exc:
            failed_categories.append(category)
            if warnings is not None:
                _warn_source(warnings, SOURCE_SPOT_STOCK, f"{category}现货映射获取失败: {exc}")
            continue
    if source_status is not None:
        if frames and failed_categories:
            _mark_source(source_status, SOURCE_SPOT_STOCK, "partial")
        elif frames:
            _mark_source(source_status, SOURCE_SPOT_STOCK, "ok")
        elif failed_categories:
            _mark_source(source_status, SOURCE_SPOT_STOCK, "error")
        else:
            _mark_source(source_status, SOURCE_SPOT_STOCK, "empty")

    rows = []
    all_names: list[str] = []
    for df in frames:
        for _, row in df.iterrows():
            producer_names = _split_names(row.get("生产商"))
            downstream_names = _split_names(row.get("下游用户"))
            all_names.extend(producer_names[:8])
            all_names.extend(downstream_names[:8])
            rows.append({
                "category": "spot",
                "source": "akshare:futures_spot_stock",
                "indicator_name": str(row.get("商品名称") or ""),
                "indicator_type": str(row.get("__category") or ""),
                "change_pct": None,
                "half_year_change_pct": _to_float(row.get("近半年涨跌幅")),
                "latest_value": _to_float(row.get("最新价格")),
                "period": "近半年",
                "target_sector": str(row.get("__category") or ""),
                "target_etf": f"{row.get('__category')} ETF",
                "reason": "现货价格上行优先看生产商利润弹性；价格下行关注下游成本改善",
                "producer_names": producer_names[:8],
                "downstream_names": downstream_names[:8],
                "producer_stocks": [],
                "downstream_stocks": [],
            })

    lookup = await _stock_lookup(db, all_names)
    for item in rows:
        symbol = SPOT_SYMBOL_MAP.get(item["indicator_name"])
        history = spot_history.get(symbol or "", [])
        recent = history[-10:]
        latest_history = recent[-1] if recent else None
        previous_history = recent[-2] if len(recent) >= 2 else None
        if latest_history:
            item["latest_value"] = latest_history.get("spot_price") or item["latest_value"]
            item["daily_change_pct"] = _daily_change(
                latest_history.get("spot_price"),
                previous_history.get("spot_price") if previous_history else None,
            )
            item["basis_rate"] = latest_history.get("basis_rate")
            item["dominant_contract"] = latest_history.get("dominant_contract")
            item["period"] = "日频"
        else:
            item["daily_change_pct"] = None
            item["basis_rate"] = None
            item["dominant_contract"] = None
        item["history"] = _series_from_daily(recent, "spot_price")
        item["symbol"] = symbol
        item["change_pct"] = item["daily_change_pct"]
        item["producer_stocks"] = _stock_items(item["producer_names"], lookup)[:8]
        item["downstream_stocks"] = _stock_items(item["downstream_names"], lookup)[:8]
        _attach_signal_fields(item)
        ranking_change = item.get("effective_change_pct") or 0
        item["linkage_strength"] = round(
            min(100, abs(ranking_change) * 4 + len(item["affected_stocks"]) * 3),
            1,
        )
    rows.sort(
        key=lambda item: (
            item.get("daily_change_pct") is not None,
            abs(item.get("daily_change_pct") if item.get("daily_change_pct") is not None else item.get("half_year_change_pct") or 0),
        ),
        reverse=True,
    )
    return rows


def _spot_counterpart(spot_rows: list[dict[str, Any]], symbol_code: str, display_name: str) -> Optional[dict[str, Any]]:
    spot_symbol = symbol_code.rstrip("0")
    for item in spot_rows:
        if item.get("symbol") == spot_symbol:
            return item
    for item in spot_rows:
        name = str(item.get("indicator_name") or "")
        if display_name in name or name in display_name:
            return item
    return None


async def _build_futures_rows(
    db: AsyncSession,
    spot_rows: Optional[list[dict[str, Any]]] = None,
    warnings: Optional[list[dict[str, str]]] = None,
    source_status: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    import akshare as ak

    futures_symbols = ",".join(symbol for symbol, *_rest in DOMESTIC_FUTURES)
    try:
        df = await _ak_call(ak.futures_zh_spot, symbol=futures_symbols, market="CF", adjust="0")
    except Exception as exc:
        if source_status is not None:
            _mark_source(source_status, SOURCE_FUTURES_SPOT, "error")
        if warnings is not None:
            _warn_source(warnings, SOURCE_FUTURES_SPOT, f"期货实时行情获取失败: {exc}")
        return []
    rows = []
    if df is None or len(df) == 0:
        if source_status is not None:
            _mark_source(source_status, SOURCE_FUTURES_SPOT, "empty")
        return rows
    if source_status is not None:
        _mark_source(source_status, SOURCE_FUTURES_SPOT, "ok")

    config_by_name = {item[1]: item for item in DOMESTIC_FUTURES}
    daily_error_count = 0
    for idx, row in df.iterrows():
        name = str(row.get("symbol") or "")
        base_name = name.replace("连续", "")
        config = config_by_name.get(base_name)
        if not config:
            continue
        symbol_code, display_name, sector, etf, reason = config
        current = _to_float(row.get("current_price"))
        last_settle = _to_float(row.get("last_settle_price"))
        change_pct = _daily_change(current, last_settle)
        counterpart = _spot_counterpart(spot_rows or [], symbol_code, display_name)
        producer_names = list(counterpart.get("producer_names") or []) if counterpart else []
        downstream_names = list(counterpart.get("downstream_names") or []) if counterpart else []
        producer_stocks = [dict(stock) for stock in (counterpart.get("producer_stocks") or [])] if counterpart else []
        downstream_stocks = [dict(stock) for stock in (counterpart.get("downstream_stocks") or [])] if counterpart else []
        daily_history = []
        try:
            daily_df = await _ak_call(ak.futures_zh_daily_sina, symbol=symbol_code)
            if daily_df is not None and len(daily_df) > 0:
                tail = daily_df.tail(10).to_dict("records")
                daily_history = _series_from_daily(tail, "close")
        except Exception as exc:
            daily_error_count += 1
            if warnings is not None and daily_error_count <= 3:
                _warn_source(warnings, SOURCE_FUTURES_DAILY, f"{display_name}期货日线获取失败: {exc}")
            daily_history = []
        item = {
            "category": "futures",
            "source": "akshare:futures_zh_spot",
            "indicator_name": display_name,
            "indicator_type": symbol_code,
            "change_pct": change_pct,
            "daily_change_pct": change_pct,
            "half_year_change_pct": None,
            "latest_value": current,
            "open": _to_float(row.get("open")),
            "high": _to_float(row.get("high")),
            "low": _to_float(row.get("low")),
            "volume": _to_float(row.get("volume")),
            "hold": _to_float(row.get("hold")),
            "last_settle_price": last_settle,
            "period": "实时/日频",
            "target_sector": sector,
            "target_etf": etf,
            "reason": reason,
            "producer_names": producer_names,
            "downstream_names": downstream_names,
            "producer_stocks": producer_stocks,
            "downstream_stocks": downstream_stocks,
            "history": daily_history,
            "symbol": symbol_code,
        }
        _attach_signal_fields(item)
        item["linkage_strength"] = round(
            min(100, abs(change_pct or 0) * 12 + len(item["affected_stocks"]) * 2),
            1,
        )
        rows.append(item)
    if source_status is not None:
        if rows and daily_error_count:
            _mark_source(source_status, SOURCE_FUTURES_DAILY, "partial")
        elif rows:
            _mark_source(source_status, SOURCE_FUTURES_DAILY, "ok")
    rows.sort(key=lambda item: abs(item.get("effective_change_pct") or 0), reverse=True)
    return rows


async def _build_industry_rows(db: AsyncSession) -> list[dict[str, Any]]:
    spot_rows = await _build_spot_rows(db)
    rows = []
    for item in spot_rows:
        producer_count = len(item.get("producer_names") or [])
        downstream_count = len(item.get("downstream_names") or [])
        industry_item = {
            **item,
            "category": "industry",
            "source": "akshare:futures_spot_stock",
            "indicator_name": f"{item['indicator_name']}:产业链",
            "target_sector": item["target_sector"],
            "target_etf": item["target_etf"],
            "reason": f"映射生产商{producer_count}家、下游用户{downstream_count}家；后续可接付费产业数据库补库存/开工率",
        }
        _attach_signal_fields(industry_item)
        industry_item["linkage_strength"] = round(
            min(100, item["linkage_strength"] + producer_count + downstream_count),
            1,
        )
        rows.append(industry_item)
    rows.sort(key=lambda item: item["linkage_strength"], reverse=True)
    return rows


async def _build_summary(db: AsyncSession) -> dict[str, Any]:
    warnings: list[dict[str, str]] = []
    source_status: dict[str, str] = {
        SOURCE_FUTURES_SPOT: "pending",
        SOURCE_FUTURES_DAILY: "pending",
        SOURCE_SPOT_STOCK: "pending",
        SOURCE_SPOT_PRICE_DAILY: "pending",
    }
    spot_rows = await _build_spot_rows(db, warnings, source_status)
    futures_rows = await _build_futures_rows(db, spot_rows, warnings, source_status)
    industry_rows = []
    for item in spot_rows:
        producer_count = len(item.get("producer_names") or [])
        downstream_count = len(item.get("downstream_names") or [])
        industry_item = {
            **item,
            "category": "industry",
            "indicator_name": f"{item['indicator_name']}:产业链",
            "reason": f"映射生产商{producer_count}家、下游用户{downstream_count}家；后续可接付费产业数据库补库存/开工率",
        }
        _attach_signal_fields(industry_item)
        industry_item["linkage_strength"] = round(
            min(100, item["linkage_strength"] + producer_count + downstream_count),
            1,
        )
        industry_rows.append(industry_item)
    industry_rows.sort(key=lambda item: item["linkage_strength"], reverse=True)

    return {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "ttl_seconds": int(_CACHE_TTL_SECONDS),
        "sources": [
            SOURCE_FUTURES_SPOT,
            SOURCE_FUTURES_DAILY,
            SOURCE_SPOT_STOCK,
            SOURCE_SPOT_PRICE_DAILY,
        ],
        "source_status": _finalize_source_status(source_status),
        "warnings": warnings,
        "tabs": {
            "futures": futures_rows,
            "spot": spot_rows,
            "industry": industry_rows,
        },
    }


@router.get("/summary")
async def get_commodity_linkage_summary(
    refresh: bool = Query(False, description="是否强制刷新外部源缓存"),
    db: AsyncSession = Depends(get_db),
):
    """获取商品联动汇总，页面只读缓存，避免外部免费源被高频并发打爆."""
    cache_key = "summary"
    now = _time.monotonic()
    if not refresh and cache_key in _CACHE:
        expires_at, data = _CACHE[cache_key]
        if expires_at > now:
            return {**data, "cache": "hit"}

    async with _CACHE_LOCK:
        now = _time.monotonic()
        if not refresh and cache_key in _CACHE:
            expires_at, data = _CACHE[cache_key]
            if expires_at > now:
                return {**data, "cache": "hit"}

        data = await _build_summary(db)
        _CACHE[cache_key] = (_time.monotonic() + _CACHE_TTL_SECONDS, data)
        return {**data, "cache": "refresh"}
