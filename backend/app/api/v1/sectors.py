"""板块营地 API — 板块强弱/轮动/持续性

板块口径(统一pywencai):
- 行业板块: pywencai三级行业257个
- 概念板块: pywencai概念389个
- 申万行业: 不展示, 仅保留采集

API设计:
- /strength: 板块强弱排名(支持concept/industry筛选, 含资金流)
- /rotation: 板块轮动信号(直接读DB, 支持sector_type筛选)
- /persistence: 板块持续性分析(支持sector_type筛选)
- /count: 板块数量统计(pywencai口径)

性能优化:
- strength: 从SectorPersistence直接查, 不走calc_sector_strength全量计算
- rotation: 直接从SectorRotation表读, 不每次重新计算
- persistence: 直接从SectorPersistence表读
"""

from datetime import date, datetime, timedelta
from typing import Optional
import time

from fastapi import APIRouter, Depends, Query
from loguru import logger
from sqlalchemy import select, func, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.trade_calendar import trade_calendar
from app.db.session import get_db
from app.models.stock import SectorInfo, SectorPersistence
from app.models.sector import SectorRotation, SectorStrength, SectorKline
from app.models.governance import DashboardSnapshot
from app.sector.lifecycle import SectorLifecycleData, SectorLifecycleEngine, LifecycleState

router = APIRouter()
lifecycle_engine = SectorLifecycleEngine()
_lifecycle_snapshot_cache: dict[tuple[str, str], dict] = {}
LIFECYCLE_SNAPSHOT_VERSION = "v2"

# 热门阈值: 资金净流入>10亿 或 连续活跃>=3天
HOT_FUND_FLOW_THRESHOLD = 10.0
HOT_CONSECUTIVE_DAYS = 3


def _lifecycle_cache_ttl_seconds() -> int:
    return 600 if _is_intraday_session_now() else 900


def _get_cached_lifecycle_snapshot(trade_date: date, sector_type: Optional[str]):
    key = (str(trade_date), sector_type or "all")
    cached = _lifecycle_snapshot_cache.get(key)
    if not cached:
        return None
    if time.time() - cached["ts"] > _lifecycle_cache_ttl_seconds():
        _lifecycle_snapshot_cache.pop(key, None)
        return None
    return cached


def _set_cached_lifecycle_snapshot(
    trade_date: date,
    sector_type: Optional[str],
    items: list[dict],
    state_stats: dict,
):
    key = (str(trade_date), sector_type or "all")
    _lifecycle_snapshot_cache[key] = {
        "ts": time.time(),
        "items": items,
        "state_stats": state_stats,
    }


def _drop_cached_lifecycle_snapshot(trade_date: date, sector_type: Optional[str]):
    key = (str(trade_date), sector_type or "all")
    _lifecycle_snapshot_cache.pop(key, None)


def _lifecycle_snapshot_key(sector_type: Optional[str]) -> str:
    return f"sector-lifecycle:{LIFECYCLE_SNAPSHOT_VERSION}:{sector_type or 'all'}"


async def _expected_lifecycle_sector_count(
    db: AsyncSession,
    trade_date: date,
    sector_type: Optional[str],
) -> int:
    """按板块营地展示口径计算生命周期列表应覆盖的板块数量。"""
    query = (
        select(func.count(func.distinct(SectorPersistence.sector_code)))
        .join(SectorInfo, SectorInfo.sector_code == SectorPersistence.sector_code)
        .where(
            and_(
                SectorPersistence.trade_date == trade_date,
                SectorInfo.source == "pywencai",
                SectorInfo.sector_type.in_(["concept", "industry"]),
                SectorInfo.is_excluded == 0,
            )
        )
    )
    if sector_type:
        query = query.where(SectorInfo.sector_type == sector_type)

    result = await db.execute(query)
    return int(result.scalar() or 0)


def _lifecycle_snapshot_covers_scope(snapshot: dict, expected_count: int) -> bool:
    if expected_count <= 0:
        return True
    items = snapshot.get("items") or []
    codes = {item.get("sector_code") for item in items if item.get("sector_code")}
    return len(codes) >= expected_count


async def _get_persisted_lifecycle_snapshot(
    db: AsyncSession,
    trade_date: date,
    sector_type: Optional[str],
):
    import json

    result = await db.execute(
        select(DashboardSnapshot)
        .where(
            and_(
                DashboardSnapshot.snapshot_key == _lifecycle_snapshot_key(sector_type),
                DashboardSnapshot.trade_date == trade_date,
                DashboardSnapshot.status == "ok",
            )
        )
        .order_by(desc(DashboardSnapshot.snapshot_time))
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if not row:
        return None
    try:
        payload = json.loads(row.payload_json)
        items = payload.get("items") or []
        state_stats = payload.get("state_stats") or {}
        if not isinstance(items, list) or not isinstance(state_stats, dict):
            return None
        _set_cached_lifecycle_snapshot(trade_date, sector_type, items, state_stats)
        return {"items": items, "state_stats": state_stats}
    except Exception:
        return None


async def _persist_lifecycle_snapshot(
    db: AsyncSession,
    trade_date: date,
    sector_type: Optional[str],
    items: list[dict],
    state_stats: dict,
):
    import json

    payload_json = json.dumps(
        {"items": items, "state_stats": state_stats},
        ensure_ascii=False,
        allow_nan=False,
    )
    row = DashboardSnapshot(
        snapshot_key=_lifecycle_snapshot_key(sector_type),
        trade_date=trade_date,
        snapshot_time=datetime.now(),
        payload_json=payload_json,
        status="ok",
    )
    _set_cached_lifecycle_snapshot(trade_date, sector_type, items, state_stats)
    try:
        db.add(row)
        await db.flush()
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.warning(f"生命周期快照持久化失败，已降级为内存缓存: {exc}")


def _build_lifecycle_items_from_rows(rows: list) -> list[dict]:
    """从生命周期落表记录构建接口项，供快照缓存复用。"""
    import json

    items_all = []
    for row in rows:
        main_line_status = _derive_main_line_status_from_row(row)
        items_all.append({
            "sector_code": row.sector_code,
            "sector_name": row.sector_name,
            "sector_type": row.sector_type,
            "lifecycle_state": row.lifecycle_state,
            "state_label": _get_state_label(row.lifecycle_state),
            "state_score": round(row.state_score or 0, 1),
            "limit_up_count": row.limit_up_count or 0,
            "first_board_count": row.first_board_count or 0,
            "consecutive_board_count": row.consecutive_board_count or 0,
            "max_board_height": row.max_board_height or 0,
            "raw_limit_up_count": row.limit_up_count or 0,
            "raw_first_board_count": row.first_board_count or 0,
            "raw_consecutive_board_count": row.consecutive_board_count or 0,
            "raw_max_board_height": row.max_board_height or 0,
            "fund_flow": round(row.fund_flow or 0, 2),
            "active_days": row.active_days or 0,
            "quality_score": round(row.quality_score or 0, 1),
            "is_main_line": main_line_status != "none",
            "main_line_status": main_line_status,
            "main_line_label": _get_main_line_label(main_line_status),
            "leader_stocks": json.loads(row.leader_stocks) if row.leader_stocks else [],
            "ladder_stocks": json.loads(row.ladder_stocks) if row.ladder_stocks else [],
            "raw_leader_stocks": json.loads(row.leader_stocks) if row.leader_stocks else [],
            "raw_ladder_stocks": json.loads(row.ladder_stocks) if row.ladder_stocks else [],
            "attributed_reason_samples": [],
            "raw_reason_samples": [],
            "attribution_confidence": "primary" if row.limit_up_count else "none",
            "attribution_confidence_label": _get_attribution_confidence_label("primary" if row.limit_up_count else "none"),
            "kline_trend": row.kline_trend,
            "kline_vol_ratio": row.kline_vol_ratio,
            "kline_ma5": row.kline_ma5,
            "kline_ma20": row.kline_ma20,
            "kline_close": row.kline_close,
            "kline_support": row.kline_support,
            "kline_resistance": row.kline_resistance,
        })
    return items_all


async def _build_runtime_lifecycle_snapshot(
    db: AsyncSession,
    trade_date: date,
    sector_type: Optional[str],
) -> tuple[list[dict], dict]:
    """基于持续性/涨停/历史状态实时回放，构建生命周期快照。"""
    recent_dates_query = (
        select(SectorPersistence.trade_date)
        .join(SectorInfo, SectorInfo.sector_code == SectorPersistence.sector_code)
        .where(
            and_(
                SectorPersistence.trade_date <= trade_date,
                SectorInfo.source == "pywencai",
                SectorInfo.is_excluded == 0,
            )
        )
        .distinct()
        .order_by(desc(SectorPersistence.trade_date))
        .limit(4)
    )
    if sector_type:
        recent_dates_query = recent_dates_query.where(SectorInfo.sector_type == sector_type)

    recent_dates = [row[0] for row in (await db.execute(recent_dates_query)).all()]
    if not recent_dates and trade_date:
        recent_dates = [trade_date]

    persist_result = await db.execute(
        select(
            SectorPersistence.trade_date,
            SectorPersistence.sector_code,
            SectorPersistence.sector_name,
            SectorPersistence.strength_score,
            SectorPersistence.change_pct,
            SectorInfo.sector_type,
        )
        .join(SectorInfo, SectorInfo.sector_code == SectorPersistence.sector_code)
        .where(
            and_(
                SectorPersistence.trade_date.in_(recent_dates),
                SectorInfo.source == "pywencai",
                SectorInfo.sector_type.in_(["concept", "industry"]),
                SectorInfo.is_excluded == 0,
            )
        )
        .order_by(SectorPersistence.trade_date, desc(SectorPersistence.strength_score))
    )

    grouped_inputs = {}
    strength_map = {}
    change_map = {}
    for day_value, code, name, p_strength, p_change, stype in persist_result.all():
        if sector_type and stype != sector_type:
            continue
        grouped_inputs.setdefault(day_value, []).append({
            "sector_code": code,
            "sector_name": name,
            "sector_type": stype,
        })
        if day_value == trade_date:
            strength_map[code] = round(float(p_strength or 0), 1)
            change_map[code] = round(float(p_change or 0), 2)

    computed_history = {}
    today_batch = {}
    for day_value in sorted(grouped_inputs.keys()):
        prev_snapshot_map = {
            code: history[-1]
            for code, history in computed_history.items()
            if history
        }
        recent_history_map = {
            code: history[-5:]
            for code, history in computed_history.items()
            if history
        }
        batch_result = await lifecycle_engine.analyze_sectors_batch(
            db,
            grouped_inputs[day_value],
            day_value,
            prev_snapshot_map=prev_snapshot_map,
            recent_history_map=recent_history_map,
        )
        for item in grouped_inputs[day_value]:
            code = item["sector_code"]
            data = batch_result.get(code)
            if not data:
                continue
            computed_history.setdefault(code, []).append(data)
            if day_value == trade_date:
                today_batch[code] = (item, data)

    analyzed = []
    for code, pair in today_batch.items():
        input_item, data = pair
        lc_state = data.lifecycle_state.value
        analyzed.append((data.quality_score or 0, {
            "sector_code": code,
            "sector_name": input_item["sector_name"],
            "sector_type": input_item["sector_type"],
            "lifecycle_state": lc_state,
            "state_label": _get_state_label(lc_state),
            "state_score": lifecycle_engine.state_scores.get(data.lifecycle_state, 0),
            "strength_score": strength_map.get(code, 0),
            "change_pct": change_map.get(code, 0),
            "limit_up_count": data.limit_up_count,
            "first_board_count": data.first_board_count,
            "consecutive_board_count": data.consecutive_board_count,
            "max_board_height": data.max_board_height,
            "raw_limit_up_count": data.raw_limit_up_count,
            "raw_first_board_count": data.raw_first_board_count,
            "raw_consecutive_board_count": data.raw_consecutive_board_count,
            "raw_max_board_height": data.raw_max_board_height,
            "fund_flow": round(data.fund_flow or 0, 2),
            "active_days": data.active_days,
            "quality_score": round(data.quality_score or 0, 1),
            "is_main_line": data.is_main_line,
            "main_line_status": data.main_line_status,
            "main_line_label": _get_main_line_label(data.main_line_status),
            "leader_stocks": data.leader_stocks,
            "ladder_stocks": data.ladders,
            "raw_leader_stocks": data.raw_leader_stocks,
            "raw_ladder_stocks": data.raw_ladders,
            "attributed_reason_samples": data.attributed_reason_samples,
            "raw_reason_samples": data.raw_reason_samples,
            "attribution_confidence": data.attribution_confidence,
            "attribution_confidence_label": _get_attribution_confidence_label(data.attribution_confidence),
            "kline_trend": data.kline_trend,
            "kline_vol_ratio": data.kline_vol_ratio,
            "kline_ma5": data.kline_ma5,
            "kline_ma20": data.kline_ma20,
            "kline_close": data.kline_close,
            "kline_support": data.kline_support,
            "kline_resistance": data.kline_resistance,
        }))

    items_all = _sort_lifecycle_items([x[1] for x in analyzed])
    items_all = [_apply_display_state(it) for it in items_all]
    state_stats = _build_lifecycle_state_stats(items_all)
    return items_all, state_stats


async def prewarm_lifecycle_snapshot(
    db: AsyncSession,
    trade_date: Optional[date] = None,
    sector_types: Optional[list[str]] = None,
    force_refresh: bool = False,
) -> dict[str, int]:
    """供调度器预热生命周期快照，避免页面首开冷启动。"""
    target_date = trade_date or await _resolve_trade_date(db, SectorPersistence.trade_date, None)
    if not target_date:
        return {}

    sector_types = sector_types or ["concept", "industry"]
    warmed = {}
    for current_type in sector_types:
        if not force_refresh:
            cached = _get_cached_lifecycle_snapshot(target_date, current_type)
            if cached is not None:
                warmed[current_type] = len(cached["items"])
                continue
            persisted = await _get_persisted_lifecycle_snapshot(db, target_date, current_type)
            if persisted is not None:
                warmed[current_type] = len(persisted["items"])
                continue

        items, state_stats = await _build_runtime_lifecycle_snapshot(db, target_date, current_type)
        await _persist_lifecycle_snapshot(db, target_date, current_type, items, state_stats)
        warmed[current_type] = len(items)

    return warmed


def _is_intraday_session_now() -> bool:
    """是否处于盘中可视作实时数据时段(避免依赖外部交易日历IO)"""
    now = datetime.now()
    if now.weekday() >= 5:  # 周末
        return False
    return trade_calendar.get_trade_session(now) in ("pre_auction", "morning", "afternoon")


async def _resolve_trade_date(
    session: AsyncSession,
    date_col,
    trade_date: Optional[str] = None,
) -> date:
    """统一日期策略:
    - 指定trade_date: 用用户指定日期
    - 盘中: 优先今天(有数据则用今天), 否则回退最近一次交易数据
    - 盘后/非交易时段: 展示最近一次交易数据
    """
    if trade_date:
        return date.fromisoformat(trade_date)

    today = date.today()
    # 最近一次可用数据(不超过今天)
    result = await session.execute(
        select(func.max(date_col)).where(date_col <= today)
    )
    latest = result.scalar()
    if not latest:
        fallback_result = await session.execute(select(func.max(date_col)))
        latest = fallback_result.scalar()
    if not latest:
        return today

    # 盘中优先使用今日实时数据(若已落库)
    if _is_intraday_session_now():
        today_result = await session.execute(
            select(func.max(date_col)).where(date_col == today)
        )
        today_exists = today_result.scalar()
        if today_exists:
            return today

    return latest


def _derive_lifecycle_state(
    *,
    strength_score: float,
    limit_up_count: int,
    fund_flow: float,
    consecutive_days: int,
    max_board_height: int = 0,
    consecutive_board_count: int = 0,
) -> str:
    """生命周期简化兜底判定。

    仅用于缺少完整状态机结果时的历史/辅助场景，不应替代权威引擎。
    """
    if limit_up_count >= 10 and max_board_height >= 5:
        return "climax"
    if limit_up_count >= 10:
        if consecutive_board_count >= 3 and max_board_height >= 3:
            return "climax"
        return "accelerating"
    if limit_up_count >= 5 and max_board_height >= 3:
        return "accelerating"
    if limit_up_count >= 8:
        return "accelerating"
    if limit_up_count >= 5:
        if consecutive_board_count >= 1 and max_board_height >= 2:
            return "accelerating"
        return "emerging"
    if limit_up_count >= 2 and max_board_height >= 3:
        return "accelerating"
    if limit_up_count >= 2:
        return "emerging"
    if limit_up_count == 1 and fund_flow > 1:
        return "emerging"

    if fund_flow > 10 and consecutive_days >= 2:
        return "accelerating"
    if fund_flow > 3 and consecutive_days >= 1:
        return "emerging"

    if limit_up_count == 0 and fund_flow < -5 and consecutive_days <= 1:
        return "declining"
    if limit_up_count == 0 and fund_flow < 0 and consecutive_days <= 1:
        return "diverging"

    if limit_up_count == 0 and fund_flow <= 0 and consecutive_days == 0:
        return "dormant"

    if limit_up_count == 0 and fund_flow <= 0 and consecutive_days == 1:
        return "one_day"

    if fund_flow > 0 or consecutive_days >= 1:
        return "emerging"
    return "dormant"


def _state_score(state: str) -> float:
    mapping = {
        "dormant": 0.0,
        "one_day": 10.0,
        "declining": 20.0,
        "diverging": 40.0,
        "emerging": 60.0,
        "accelerating": 80.0,
        "climax": 100.0,
    }
    return mapping.get(state, 0.0)


def _build_lifecycle_item_from_persistence(sp, sector_type: str) -> dict:
    """从SectorPersistence构造生命周期项 — 仅作为最后兜底，涨停/连板数据不可用

    注意: Persistence表的consecutive_days是板块连续活跃天数，不是个股连板天数。
    因此此函数无法提供准确的涨停/首板/连板数据，相关字段默认0。
    实际数据应通过SectorLifecycleEngine实时计算获取。
    """
    score = float(sp.strength_score or 0.0)
    limit_up = int(sp.limit_up_count or 0)
    fund_flow = float(sp.fund_flow or 0.0)
    active_days = int(sp.consecutive_days or 0)
    state = _derive_lifecycle_state(
        strength_score=score,
        limit_up_count=limit_up,
        fund_flow=fund_flow,
        consecutive_days=active_days,
        max_board_height=0,
        consecutive_board_count=0,
    )
    # Persistence表无涨停明细，首板/连板/最高板均不可推算，默认0
    quality_score = round(
        min(
            100.0,
            score * 0.55 + limit_up * 4.0 + max(fund_flow, 0) * 2.0 + min(active_days, 10) * 2.0,
        ),
        1,
    )
    is_main_line = (
        state in {"emerging", "accelerating", "climax"}
        and active_days >= 3
        and limit_up >= 3
        and score >= 70
    )
    return {
        "sector_code": sp.sector_code,
        "sector_name": sp.sector_name,
        "sector_type": sector_type,
        "lifecycle_state": state,
        "state_label": _get_state_label(state),
        "state_score": _state_score(state),
        "limit_up_count": limit_up,
        "first_board_count": 0,
        "consecutive_board_count": 0,
        "max_board_height": 0,
        "fund_flow": round(fund_flow, 2),
        "active_days": active_days,
        "quality_score": quality_score,
        "is_main_line": is_main_line,
        "leader_stocks": [],
        "ladder_stocks": [],
        "kline_trend": None,
        "kline_vol_ratio": None,
        "kline_ma5": None,
        "kline_ma20": None,
        "kline_close": None,
        "kline_support": None,
        "kline_resistance": None,
    }


@router.get("/strength")
async def sector_strength(
    trade_date: Optional[str] = Query(None, description="交易日期 YYYY-MM-DD"),
    sector_type: Optional[str] = Query(None, description="板块类型: concept/industry"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页条数"),
    db: AsyncSession = Depends(get_db),
):
    """板块强弱排名(含资金流) — 直接从SectorPersistence+SectorStrength查

    返回字段:
    - rank: 排名(同类型内)
    - sector_code: 板块代码
    - sector_name: 板块名称
    - sector_type: concept/industry
    - strength_score: 综合强弱评分(0-100)
    - rank_change: 排名变化(正=上升)
    - change_pct: 板块涨跌幅%
    - fund_flow: 资金净流入(亿)
    - limit_up_count: 涨停家数
    - consecutive_days: 连续活跃天数
    - is_hot: 是否热门(资金>10亿 或 连续>=3天)
    - stock_count: 成分股数量
    """
    # 回退到最近有数据的交易日
    d = await _resolve_trade_date(db, SectorPersistence.trade_date, trade_date)

    # 从SectorPersistence获取所有板块数据(一次查询, 同时获取SectorInfo的sector_name作为权威名称)
    result = await db.execute(
        select(SectorPersistence, SectorInfo.sector_type, SectorInfo.stock_count,
               SectorInfo.sector_name)
        .outerjoin(SectorInfo, SectorInfo.sector_code == SectorPersistence.sector_code)
        .where(
            and_(
                SectorPersistence.trade_date == d,
                SectorInfo.source == "pywencai",
                SectorInfo.sector_type.in_(["concept", "industry"]),
                SectorInfo.is_excluded == 0,
            )
        )
    )
    rows = result.all()

    # 获取昨日排名(一次查询)
    prev_date_result = await db.execute(
        select(func.max(SectorStrength.trade_date)).where(SectorStrength.trade_date < d)
    )
    prev_date = prev_date_result.scalar()
    prev_ranks = {}
    if prev_date:
        prev_result = await db.execute(
            select(SectorStrength.sector_code, SectorStrength.rank)
            .where(SectorStrength.trade_date == prev_date)
        )
        prev_ranks = {r.sector_code: r.rank for r in prev_result.all()}

    # 构建列表
    items = []
    for row in rows:
        sp = row[0]  # SectorPersistence
        st = row[1]  # sector_type
        sc = row[2]  # stock_count
        si_name = row[3]  # SectorInfo.sector_name (pywencai口径, 防止kline_name泄漏)
        if not st:
            continue
        # sector_type筛选
        if sector_type and st != sector_type:
            continue
        items.append({
            "sector_code": sp.sector_code,
            "sector_name": si_name or sp.sector_name,  # 优先用SectorInfo权威名称
            "sector_type": st,
            "strength_score": round(sp.strength_score or 0, 1),
            "change_pct": round(sp.change_pct or 0, 2),
            "fund_flow": round(sp.fund_flow or 0, 2),
            "limit_up_count": sp.limit_up_count or 0,
            "consecutive_days": sp.consecutive_days or 0,
            "stock_count": sc or 0,
            "prev_rank": prev_ranks.get(sp.sector_code, 0),
        })

    # 按强度排序并排名
    items.sort(key=lambda x: x["strength_score"], reverse=True)
    for i, item in enumerate(items, 1):
        item["rank"] = i
        prev = item.pop("prev_rank")
        item["rank_change"] = prev - i if prev > 0 else 0
        item["is_hot"] = (
            item["fund_flow"] > HOT_FUND_FLOW_THRESHOLD
            or item["consecutive_days"] >= HOT_CONSECUTIVE_DAYS
        )

    # 分页
    total = len(items)
    start = (page - 1) * page_size
    page_items = items[start:start + page_size]

    # 板块总量采用 /count 同口径，确保跨API一致性
    count_result = await db.execute(
        select(SectorInfo.sector_type, func.count())
        .where(
            and_(
                SectorInfo.source == "pywencai",
                SectorInfo.sector_type.in_(["concept", "industry"]),
                SectorInfo.is_excluded == 0,
            )
        )
        .group_by(SectorInfo.sector_type)
    )
    count_map = {row[0]: row[1] for row in count_result.all()}

    # 按类型分组统计(全量)，始终返回 concept/industry 两个key
    type_stats = {
        "concept": {
            "total": int(count_map.get("concept", 0)),
            "hot": 0,
            "has_fund_in": 0,
            "total_fund_flow": 0.0,
        },
        "industry": {
            "total": int(count_map.get("industry", 0)),
            "hot": 0,
            "has_fund_in": 0,
            "total_fund_flow": 0.0,
        },
    }
    for item in items:
        st = item["sector_type"]
        if st not in type_stats:
            continue
        if item["is_hot"]:
            type_stats[st]["hot"] += 1
        if item["fund_flow"] > 0:
            type_stats[st]["has_fund_in"] += 1
        type_stats[st]["total_fund_flow"] += item["fund_flow"]

    for st in type_stats:
        type_stats[st]["total_fund_flow"] = round(type_stats[st]["total_fund_flow"], 2)

    return {
        "trade_date": str(d),
        "total": total,
        "page": page,
        "page_size": page_size,
        "type_stats": type_stats,
        "items": page_items,
    }


@router.get("/rotation")
async def sector_rotation(
    trade_date: Optional[str] = Query(None, description="交易日期"),
    sector_type: Optional[str] = Query(None, description="板块类型: concept/industry"),
    db: AsyncSession = Depends(get_db),
):
    """板块轮动信号 — 直接从SectorRotation表读取

    不再每次调用detect_rotation重新计算(1秒+),
    改为从DB直接读取已采集/计算的轮动信号
    """
    # 回退到最近有数据的交易日
    d = await _resolve_trade_date(db, SectorRotation.trade_date, trade_date)

    # 直接从SectorRotation表读
    result = await db.execute(
        select(SectorRotation).where(SectorRotation.trade_date == d)
        .order_by(desc(SectorRotation.flow_amount))
        .limit(50)
    )
    rotations = result.scalars().all()

    # 获取板块类型映射(一次查询)
    si_result = await db.execute(
        select(SectorInfo.sector_code, SectorInfo.sector_type, SectorInfo.sector_name, SectorInfo.is_excluded)
        .where(SectorInfo.source == "pywencai")
    )
    sector_info_map = {row.sector_code: row for row in si_result.all()}

    def _get_info(code):
        return sector_info_map.get(code)

    def _pretty_name(code):
        """从sector_code提取可读名称: pw_concept_光伏概念 → 光伏概念, pw_industry_医药生物 → 医药生物"""
        if code == "其他":
            return code
        info = _get_info(code)
        if info:
            return info.sector_name
        # fallback: 去掉pw_concept_/pw_industry_前缀
        for prefix in ("pw_concept_", "pw_industry_"):
            if code.startswith(prefix):
                return code[len(prefix):]
        return code

    def _get_type(code):
        info = _get_info(code)
        return info.sector_type if info else ("concept" if code.startswith("pw_concept_") else "industry" if code.startswith("pw_industry_") else "")

    # 构建信号列表
    signals = []
    for r in rotations:
        # 过滤排除板块(融资融券/沪股通等无业务关联板块)
        from_info = _get_info(r.from_sector) if r.from_sector != "其他" else None
        to_info = _get_info(r.to_sector) if r.to_sector != "其他" else None
        if from_info and from_info.is_excluded:
            continue
        if to_info and to_info.is_excluded:
            continue

        # sector_type筛选: 流出或流入板块匹配即可
        if sector_type:
            from_match = (r.from_sector == "其他") or (_get_type(r.from_sector) == sector_type)
            to_match = (r.to_sector == "其他") or (_get_type(r.to_sector) == sector_type)
            if not from_match and not to_match:
                continue

        signals.append({
            "from_sector": r.from_sector,
            "from_name": _pretty_name(r.from_sector),
            "from_type": _get_type(r.from_sector),
            "to_sector": r.to_sector,
            "to_name": _pretty_name(r.to_sector),
            "to_type": _get_type(r.to_sector),
            "flow_amount": round(r.flow_amount or 0, 2),
            "rotation_type": r.rotation_type or "gradual",
            "rotation_type_label": "突然切换" if r.rotation_type == "sudden" else "渐进轮动",
            "confidence": round(min((r.flow_amount or 0) / 50, 1.0), 2),
        })

    return {
        "trade_date": str(d),
        "total_signals": len(signals),
        "signals": signals[:30],
    }


@router.get("/persistence")
async def sector_persistence(
    trade_date: Optional[str] = Query(None, description="交易日期"),
    min_days: int = Query(2, description="最少连续天数"),
    sector_type: Optional[str] = Query(None, description="板块类型: concept/industry"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页条数"),
    db: AsyncSession = Depends(get_db),
):
    """板块持续性分析 — 直接从SectorPersistence查, 含资金流"""
    d = await _resolve_trade_date(db, SectorPersistence.trade_date, trade_date)

    # 直接从SectorPersistence+SectorInfo联查(含权威名称)
    result = await db.execute(
        select(SectorPersistence, SectorInfo.sector_type, SectorInfo.sector_name)
        .outerjoin(SectorInfo, SectorInfo.sector_code == SectorPersistence.sector_code)
        .where(
            and_(
                SectorPersistence.trade_date == d,
                SectorPersistence.consecutive_days >= min_days,
                SectorInfo.source == "pywencai",
                SectorInfo.sector_type.in_(["concept", "industry"]),
                SectorInfo.is_excluded == 0,
            )
        )
        .order_by(desc(SectorPersistence.consecutive_days))
    )
    rows = result.all()

    # 筛选
    enriched = []
    for row in rows:
        sp = row[0]
        st = row[1]
        si_name = row[2]  # SectorInfo权威名称
        if not st:
            continue
        if sector_type and st != sector_type:
            continue
        enriched.append({
            "sector_code": sp.sector_code,
            "sector_name": si_name or sp.sector_name,
            "sector_type": st,
            "consecutive_days": sp.consecutive_days or 0,
            "limit_up_count": sp.limit_up_count or 0,
            "fund_flow": round(sp.fund_flow or 0, 2),
            "change_pct": round(sp.change_pct or 0, 2),
            "strength_score": round(sp.strength_score or 0, 1),
            "is_declining": (sp.strength_score or 0) < 50,
        })

    # 分页
    total = len(enriched)
    start = (page - 1) * page_size
    page_items = enriched[start:start + page_size]

    # 分组统计
    type_stats = {}
    for item in enriched:
        st = item.get("sector_type", "")
        if st not in type_stats:
            type_stats[st] = {"total": 0, "hot": 0, "declining": 0}
        type_stats[st]["total"] += 1
        if item.get("consecutive_days", 0) >= HOT_CONSECUTIVE_DAYS:
            type_stats[st]["hot"] += 1
        if item.get("is_declining", False):
            type_stats[st]["declining"] += 1

    return {
        "trade_date": str(d),
        "total": total,
        "page": page,
        "page_size": page_size,
        "type_stats": type_stats,
        "items": page_items,
    }


@router.get("/count")
async def sector_count(
    db: AsyncSession = Depends(get_db),
):
    """板块数量统计(pywencai口径)

    返回:
    - industry: 行业板块数(pywencai三级257个)
    - concept: 概念板块数(pywencai 389个)
    """
    result = await db.execute(
        select(SectorInfo.sector_type, func.count())
        .where(and_(SectorInfo.source == "pywencai", SectorInfo.is_excluded == 0))
        .group_by(SectorInfo.sector_type)
    )
    counts = {row[0]: row[1] for row in result.all()}

    return {
        "industry": counts.get("industry", 0),
        "concept": counts.get("concept", 0),
    }


# ========== 新增：板块生命周期API ==========

@router.get("/lifecycle")
async def sector_lifecycle(
    trade_date: Optional[str] = Query(None, description="交易日期 YYYY-MM-DD"),
    sector_type: Optional[str] = Query(None, description="板块类型: concept/industry"),
    state: Optional[str] = Query(None, description="生命周期状态筛选"),
    force_refresh: bool = Query(False, description="是否绕过生命周期快照并实时重算"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页条数"),
    db: AsyncSession = Depends(get_db),
):
    """板块生命周期状态 — 识别刚启动/加速/高潮/分化/退潮/一日游

    状态说明:
    - emerging: 刚启动(首板2只+,资金初进)
    - accelerating: 加速(连板3只+,涨停5只+,资金3亿+)
    - climax: 高潮(涨停10只+,龙头5板+,资金5亿+)
    - diverging: 分化(涨停减少30%)
    - declining: 退潮(龙头断板,批量跌停)
    - one_day: 一日游(前日首板潮,今日无持续)
    - dormant: 休眠(无涨停/无资金)

    返回包含: 龙头股、连板梯队、质量分、是否主线
    """
    from app.models.sector import SectorLifecycle

    # 日期策略:
    # - 指定 trade_date 时严格使用指定日期
    # - 盘中若持续性表已有今天数据，即使生命周期表还未生成，也优先使用今天并实时计算
    # - 非盘中优先生命周期表最近日期；若无再回退持续性表
    if trade_date:
        d = date.fromisoformat(trade_date)
    else:
        persistence_latest = await _resolve_trade_date(db, SectorPersistence.trade_date, None)
        lifecycle_latest = await _resolve_trade_date(db, SectorLifecycle.trade_date, None)

        if (
            _is_intraday_session_now()
            and persistence_latest
            and (not lifecycle_latest or persistence_latest >= lifecycle_latest)
        ):
            d = persistence_latest
        else:
            d = lifecycle_latest or persistence_latest

    expected_count = await _expected_lifecycle_sector_count(db, d, sector_type)

    cached_snapshot = None if force_refresh else _get_cached_lifecycle_snapshot(d, sector_type)
    if cached_snapshot is None and not force_refresh:
        cached_snapshot = await _get_persisted_lifecycle_snapshot(db, d, sector_type)
    if cached_snapshot is not None and not _lifecycle_snapshot_covers_scope(cached_snapshot, expected_count):
        _drop_cached_lifecycle_snapshot(d, sector_type)
        cached_snapshot = None

    rows = []
    if cached_snapshot is None and not force_refresh:
        # 构建查询(生命周期主表)
        query = (
            select(SectorLifecycle)
            .join(SectorInfo, SectorInfo.sector_code == SectorLifecycle.sector_code)
            .where(
                and_(
                    SectorLifecycle.trade_date == d,
                    SectorInfo.source == "pywencai",
                    SectorInfo.sector_type.in_(["concept", "industry"]),
                    SectorInfo.is_excluded == 0,
                )
            )
        )

        if sector_type:
            query = query.where(SectorInfo.sector_type == sector_type)

        query = query.order_by(
            desc(SectorLifecycle.state_score),
            desc(SectorLifecycle.quality_score),
            desc(SectorLifecycle.fund_flow),
            SectorLifecycle.sector_name,
        )

        result = await db.execute(query)
        rows = result.scalars().all()
        if expected_count > 0 and len({row.sector_code for row in rows}) < expected_count:
            rows = []

    # 获取排除板块列表(主表路径过滤)
    excluded_codes = set()
    if rows:
        ec_result = await db.execute(
            select(SectorInfo.sector_code).where(SectorInfo.is_excluded == 1)
        )
        excluded_codes = {r[0] for r in ec_result.all()}

    items_all = []
    full_state_stats = None
    if rows:
        filtered_rows = [row for row in rows if row.sector_code not in excluded_codes]
        items_all = _build_lifecycle_items_from_rows(filtered_rows)
        items_all = [_apply_display_state(it) for it in items_all]
        full_state_stats = _build_lifecycle_state_stats(items_all)
        await _persist_lifecycle_snapshot(db, d, sector_type, items_all, full_state_stats)
    else:
        if cached_snapshot is not None:
            items_all = cached_snapshot["items"]
            full_state_stats = cached_snapshot["state_stats"]
        else:
            items_all, full_state_stats = await _build_runtime_lifecycle_snapshot(db, d, sector_type)

    if not items_all and not trade_date:
        fallback_prev_date_result = await db.execute(
            select(func.max(SectorPersistence.trade_date))
            .join(SectorInfo, SectorInfo.sector_code == SectorPersistence.sector_code)
            .where(
                and_(
                    SectorPersistence.trade_date < d,
                    SectorInfo.source == "pywencai",
                    SectorInfo.is_excluded == 0,
                    SectorInfo.sector_type.in_(["concept", "industry"]),
                )
            )
        )
        fallback_prev_date = fallback_prev_date_result.scalar()
        if fallback_prev_date:
            if sector_type:
                cached_snapshot = _get_cached_lifecycle_snapshot(fallback_prev_date, sector_type)
                if cached_snapshot is None:
                    cached_snapshot = await _get_persisted_lifecycle_snapshot(db, fallback_prev_date, sector_type)
                if cached_snapshot is not None:
                    items_all = cached_snapshot["items"]
                    full_state_stats = cached_snapshot["state_stats"]
                    d = fallback_prev_date
                else:
                    items_all, full_state_stats = await _build_runtime_lifecycle_snapshot(db, fallback_prev_date, sector_type)
                    await _persist_lifecycle_snapshot(db, fallback_prev_date, sector_type, items_all, full_state_stats)
                    d = fallback_prev_date
            else:
                items_all, full_state_stats = await _build_runtime_lifecycle_snapshot(db, fallback_prev_date, sector_type)
                await _persist_lifecycle_snapshot(db, fallback_prev_date, sector_type, items_all, full_state_stats)
                d = fallback_prev_date

    if not rows:
        items_all = [_apply_display_state(it) for it in items_all]
    if full_state_stats is None:
        full_state_stats = _build_lifecycle_state_stats(items_all)
        if not rows:
            await _persist_lifecycle_snapshot(db, d, sector_type, items_all, full_state_stats)

    filtered_items = items_all
    if state:
        filtered_items = [it for it in items_all if it["display_state"] == state]

    filtered_items = _sort_lifecycle_items(filtered_items)

    # 分页
    total = len(filtered_items)
    start = (page - 1) * page_size
    items = filtered_items[start:start + page_size]

    return {
        "trade_date": str(d),
        "total": total,
        "page": page,
        "page_size": page_size,
        "state_stats": full_state_stats,
        "items": items,
    }


def _get_state_label(state: str) -> str:
    """获取状态中文标签"""
    labels = {
        "emerging": "刚启动",
        "accelerating": "加速中",
        "climax": "高潮",
        "diverging": "分化",
        "declining": "退潮",
        "one_day": "一日游",
        "dormant": "休眠",
        "capital_probe": "资金试探",
    }
    return labels.get(state, state)


def _get_attribution_confidence_label(level: str) -> str:
    labels = {
        "primary": "主归因",
        "theme": "主题映射",
        "resonance": "龙头共振",
        "none": "未归因",
    }
    return labels.get(level, level)


def _get_main_line_label(status: str) -> str:
    labels = {
        "strengthening": "主线强化",
        "continuing": "主线延续",
        "diverging": "主线分化",
        "none": "",
    }
    return labels.get(status, "")


def _sort_lifecycle_items(items: list[dict]) -> list[dict]:
    """统一生命周期列表排序，避免预计算/实时回退口径不一致。"""
    return sorted(
        items,
        key=lambda it: (
            -(it.get("state_score") or 0),
            -(it.get("quality_score") or 0),
            -(it.get("strength_score") or 0),
            -(it.get("fund_flow") or 0),
            it.get("sector_name") or "",
        ),
    )


def _build_lifecycle_state_stats(items: list[dict]) -> dict:
    state_stats = {}
    for it in items:
        state = it.get("display_state") or it.get("lifecycle_state")
        if not state:
            continue
        if state not in state_stats:
            state_stats[state] = {"count": 0, "total_fund_flow": 0}
        state_stats[state]["count"] += 1
        state_stats[state]["total_fund_flow"] += it.get("fund_flow") or 0

    for state in state_stats:
        state_stats[state]["total_fund_flow"] = round(state_stats[state]["total_fund_flow"], 2)

    return state_stats


def _is_capital_probe_item(item: dict) -> bool:
    fund_flow = float(item.get("fund_flow") or 0)
    change_pct = float(item.get("change_pct") or 0)
    strength = float(item.get("strength_score") or item.get("state_score") or 0)
    trend = item.get("kline_trend")
    close = float(item.get("kline_close") or 0)
    ma5 = float(item.get("kline_ma5") or 0)
    ma20 = float(item.get("kline_ma20") or 0)
    raw_limit_up = int(item.get("raw_limit_up_count") or 0)
    weak_breakdown = (
        trend in {"down", "breakdown", "breakout_down"}
        or (close and ma20 and close < ma20 * 0.97)
    )
    trend_improving = (
        trend in {"up", "breakout_up"}
        or (close and ma5 and close >= ma5)
    )
    return (
        item.get("lifecycle_state") == "dormant"
        and int(item.get("limit_up_count") or 0) == 0
        and fund_flow > 5
        and (change_pct >= 1 or strength >= 45)
        and not weak_breakdown
        and (
            raw_limit_up > 0
            or trend_improving
            or (close and ma5 and ma20 and close >= ma5 and ma5 >= ma20)
        )
    )


def _apply_display_state(item: dict) -> dict:
    display_state = "capital_probe" if _is_capital_probe_item(item) else item.get("lifecycle_state")
    item["display_state"] = display_state
    item["display_state_label"] = _get_state_label(display_state)
    return item


def _derive_main_line_status_from_row(row) -> str:
    data = SectorLifecycleData(
        sector_code=row.sector_code,
        sector_name=row.sector_name,
        sector_type=row.sector_type,
        trade_date=row.trade_date,
        lifecycle_state=LifecycleState(row.lifecycle_state),
        limit_up_count=row.limit_up_count or 0,
        first_board_count=row.first_board_count or 0,
        consecutive_board_count=row.consecutive_board_count or 0,
        max_board_height=row.max_board_height or 0,
        fund_flow=row.fund_flow or 0,
        fund_flow_3d=row.fund_flow_3d or 0,
        active_days=row.active_days or 0,
        total_active_5d=row.total_active_5d or 0,
        quality_score=row.quality_score or 0,
    )
    return lifecycle_engine._main_line_status(data)


@router.get("/lifecycle/calendar")
async def sector_lifecycle_calendar(
    days: int = Query(10, ge=5, le=30, description="回看天数"),
    sector_type: Optional[str] = Query(None, description="板块类型: concept/industry"),
    db: AsyncSession = Depends(get_db),
):
    """板块生命周期日历 — 日线级别状态矩阵(取代桑基图)

    返回:
    - dates: 日期列表
    - sectors: 板块列表(带类型)
    - matrix: 状态矩阵 {sector_code: {date: state}}
    - leaders: 龙头股矩阵 {sector_code: {date: [leaders]}}
    """
    from app.models.sector import SectorLifecycle

    # 获取最近交易日(生命周期为空时回退持续性)
    latest_result = await db.execute(select(func.max(SectorLifecycle.trade_date)))
    latest_date = latest_result.scalar()
    if not latest_date:
        latest_date = await _resolve_trade_date(db, SectorPersistence.trade_date, None)
        if not latest_date:
            return {"dates": [], "sectors": [], "matrix": {}, "leaders": {}}

    # 计算日期范围
    start_date = latest_date - timedelta(days=days * 2)  # 预留非交易日

    # 查询数据(优先生命周期，缺失时回退持续性)
    query = select(SectorLifecycle).where(
        and_(
            SectorLifecycle.trade_date >= start_date,
            SectorLifecycle.trade_date <= latest_date,
        )
    )
    if sector_type:
        query = query.where(SectorLifecycle.sector_type == sector_type)

    result = await db.execute(query)
    rows = result.scalars().all()

    # 获取排除板块列表
    excluded_codes = set()
    ec_result = await db.execute(
        select(SectorInfo.sector_code).where(SectorInfo.is_excluded == 1)
    )
    excluded_codes = {r[0] for r in ec_result.all()}

    dates = []
    sectors = {}
    matrix = {}
    leaders = {}

    if rows:
        import json
        dates = sorted(set(r.trade_date for r in rows if r.sector_code not in excluded_codes))
        for r in rows:
            if r.sector_code in excluded_codes:
                continue
            code = r.sector_code
            if code not in sectors:
                sectors[code] = {
                    "code": code,
                    "name": r.sector_name,
                    "type": r.sector_type,
                }
                matrix[code] = {}
                leaders[code] = {}

            d = str(r.trade_date)
            matrix[code][d] = r.lifecycle_state
            leaders[code][d] = json.loads(r.leader_stocks) if r.leader_stocks else []
    else:
        persist_query = (
            select(SectorPersistence, SectorInfo.sector_type)
            .outerjoin(SectorInfo, SectorInfo.sector_code == SectorPersistence.sector_code)
            .where(
                and_(
                    SectorPersistence.trade_date >= start_date,
                    SectorPersistence.trade_date <= latest_date,
                    SectorInfo.source == "pywencai",
                    SectorInfo.sector_type.in_(["concept", "industry"]),
                    SectorInfo.is_excluded == 0,
                )
            )
            .order_by(SectorPersistence.trade_date)
        )
        if sector_type:
            persist_query = persist_query.where(SectorInfo.sector_type == sector_type)

        persist_rows = (await db.execute(persist_query)).all()
        dates = sorted(set(sp.trade_date for sp, _ in persist_rows))

        grouped_inputs = {}
        for sp, st in persist_rows:
            if not st:
                continue
            grouped_inputs.setdefault(sp.trade_date, []).append({
                "sector_code": sp.sector_code,
                "sector_name": sp.sector_name,
                "sector_type": st,
            })

        computed_history = {}
        for trade_day in sorted(grouped_inputs.keys()):
            prev_snapshot_map = {
                code: history[-1]
                for code, history in computed_history.items()
                if history
            }
            recent_history_map = {
                code: history[-5:]
                for code, history in computed_history.items()
                if history
            }
            batch_result = await lifecycle_engine.analyze_sectors_batch(
                db,
                grouped_inputs[trade_day],
                trade_day,
                prev_snapshot_map=prev_snapshot_map,
                recent_history_map=recent_history_map,
            )
            for item in grouped_inputs[trade_day]:
                code = item["sector_code"]
                if code not in sectors:
                    sectors[code] = {"code": code, "name": item["sector_name"], "type": item["sector_type"]}
                    matrix[code] = {}
                    leaders[code] = {}
                    computed_history[code] = []
                data = batch_result.get(code)
                if not data:
                    continue
                computed_history[code].append(data)
                matrix[code][str(trade_day)] = data.lifecycle_state.value
                leaders[code][str(trade_day)] = data.leader_stocks or []

    # 只保留最近活跃过的板块(避免太多)
    active_sectors = {
        code: info for code, info in sectors.items()
        if any(matrix[code].get(str(d)) != "dormant" for d in dates[-days:])
    }

    return {
        "dates": [str(d) for d in dates[-days:]],
        "sectors": list(active_sectors.values()),
        "matrix": {code: matrix[code] for code in active_sectors},
        "leaders": {code: leaders[code] for code in active_sectors},
    }


@router.get("/main-lines")
async def sector_main_lines(
    status: Optional[str] = Query(None, description="状态: active/ended"),
    sector_type: Optional[str] = Query(None, description="板块类型: concept/industry"),
    db: AsyncSession = Depends(get_db),
):
    """主线板块追踪 — 当前主线 + 近期结束主线

    主线判定:
    - 连续>=3天处于emerging/accelerating/climax状态
    - 完整连板梯队(>=3只连板,高度>=3)
    - 龙头高度>=3板
    - 近5天涨停总数>=20只
    """
    from app.models.sector import SectorLifecycle, SectorMainLine

    query = select(SectorMainLine)
    if status:
        query = query.where(SectorMainLine.status == status)
    if sector_type:
        query = query.where(SectorMainLine.sector_type == sector_type)

    query = query.order_by(desc(SectorMainLine.start_date))
    result = await db.execute(query.limit(20))
    rows = result.scalars().all()

    # 获取排除板块列表
    excluded_codes = set()
    ec_result = await db.execute(
        select(SectorInfo.sector_code).where(SectorInfo.is_excluded == 1)
    )
    excluded_codes = {r[0] for r in ec_result.all()}

    items = []
    if rows:
        lifecycle_date = await _resolve_trade_date(db, SectorLifecycle.trade_date, None)
        lifecycle_map = {}
        if lifecycle_date:
            lifecycle_rows = (
                await db.execute(
                    select(SectorLifecycle).where(
                        and_(
                            SectorLifecycle.trade_date == lifecycle_date,
                            SectorLifecycle.sector_code.in_([row.sector_code for row in rows]),
                        )
                    )
                )
            ).scalars().all()
            lifecycle_map = {row.sector_code: row for row in lifecycle_rows}

        for row in rows:
            if row.sector_code in excluded_codes:
                continue
            lifecycle_row = lifecycle_map.get(row.sector_code)
            main_line_status = _derive_main_line_status_from_row(lifecycle_row) if lifecycle_row else "none"
            items.append({
                "sector_code": row.sector_code,
                "sector_name": row.sector_name,
                "sector_type": row.sector_type,
                "start_date": str(row.start_date),
                "end_date": str(row.end_date) if row.end_date else None,
                "duration_days": row.duration_days,
                "max_height": row.max_height,
                "total_limit_up": row.total_limit_up,
                "avg_fund_flow": round(row.avg_fund_flow or 0, 2),
                "leader_stock": row.leader_stock,
                "leader_name": row.leader_name,
                "leader_max_height": row.leader_max_height,
                "status": row.status,
                "end_reason": row.end_reason,
                "main_line_status": main_line_status,
                "main_line_label": _get_main_line_label(main_line_status),
            })
    else:
        # 回退: 基于最新生命周期口径构造active主线，保证与列表页一致
        if status in (None, "active"):
            latest_d = await _resolve_trade_date(db, SectorLifecycle.trade_date, None)
            if not latest_d:
                latest_d = await _resolve_trade_date(db, SectorPersistence.trade_date, None)
            if not latest_d:
                latest_d = date.today()

            fallback_query = (
                select(SectorLifecycle)
                .where(
                    and_(
                        SectorLifecycle.trade_date == latest_d,
                    )
                )
                .order_by(
                    desc(SectorLifecycle.state_score),
                    desc(SectorLifecycle.quality_score),
                    desc(SectorLifecycle.fund_flow),
                )
                .limit(50)
            )
            lifecycle_rows = (await db.execute(fallback_query)).scalars().all()
            for row in lifecycle_rows:
                if row.sector_code in excluded_codes:
                    continue
                if sector_type and row.sector_type != sector_type:
                    continue
                main_line_status = _derive_main_line_status_from_row(row)
                if main_line_status == "none":
                    continue
                items.append({
                    "sector_code": row.sector_code,
                    "sector_name": row.sector_name,
                    "sector_type": row.sector_type,
                    "start_date": str(latest_d - timedelta(days=max((row.active_days or 1) - 1, 0))),
                    "end_date": None,
                    "duration_days": int(row.active_days or 1),
                    "max_height": int(row.max_board_height or 0),
                    "total_limit_up": int(row.limit_up_count or 0),
                    "avg_fund_flow": round(float(row.fund_flow or 0), 2),
                    "leader_stock": None,
                    "leader_name": None,
                    "leader_max_height": int(row.max_board_height or 0),
                    "status": "active",
                    "end_reason": None,
                    "main_line_status": main_line_status,
                    "main_line_label": _get_main_line_label(main_line_status),
                })

    active_count = sum(1 for r in rows if r.status == "active") if rows else sum(
        1 for it in items if it.get("status") == "active"
    )
    return {
        "active_count": active_count,
        "items": items,
    }


# ========== 板块K线API ==========

@router.get("/kline")
async def sector_kline(
    sector_code: str = Query(..., description="板块代码"),
    days: int = Query(60, ge=5, le=500, description="历史天数"),
    db: AsyncSession = Depends(get_db),
):
    """板块K线数据(OHLCV) — 趋势分析核心

    用途:
    - K线图展示(板块营地/个股详情)
    - 技术因子计算(均线/布林/MACD)
    - 趋势判断(支撑/压力/突破)
    - 生命周期辅助(高潮见顶/退潮破位)

    返回:
    - sector_info: 板块基本信息
    - kline: K线数据列表(trade_date/open/high/low/close/volume/amount/change_pct/amplitude)
    - ma5/ma10/ma20/ma60: 均线数据(前端可直接用)
    - latest: 最新一根K线摘要
    """
    from datetime import timedelta

    # 计算日期范围(预留非交易日)
    end_date = date.today()
    start_date = end_date - timedelta(days=int(days * 1.8))

    # 查询K线数据
    result = await db.execute(
        select(SectorKline)
        .where(
            and_(
                SectorKline.sector_code == sector_code,
                SectorKline.trade_date >= start_date,
                SectorKline.trade_date <= end_date,
            )
        )
        .order_by(SectorKline.trade_date)
    )
    rows = result.scalars().all()

    if not rows:
        return {
            "sector_code": sector_code,
            "sector_info": None,
            "kline": [],
            "ma": {},
            "latest": None,
        }

    # 构建K线列表
    kline_list = []
    for row in rows:
        kline_list.append({
            "trade_date": str(row.trade_date),
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
            "amount": row.amount,
            "change_pct": round(row.change_pct, 2) if row.change_pct else None,
            "amplitude": round(row.amplitude, 2) if row.amplitude else None,
        })

    # 计算均线(MA5/MA10/MA20/MA60)
    closes = [r.close for r in rows if r.close is not None]
    ma_dict = {"ma5": [], "ma10": [], "ma20": [], "ma60": []}
    ma_periods = {"ma5": 5, "ma10": 10, "ma20": 20, "ma60": 60}

    for key, period in ma_periods.items():
        for i in range(len(closes)):
            if i < period - 1:
                ma_dict[key].append(None)
            else:
                avg = sum(closes[i - period + 1:i + 1]) / period
                ma_dict[key].append(round(avg, 2))

    # 将均线与日期对齐
    ma_result = {}
    for key in ma_dict:
        ma_result[key] = [
            {"trade_date": str(rows[i].trade_date), "value": ma_dict[key][i]}
            for i in range(len(rows))
            if ma_dict[key][i] is not None
        ]

    # 最新K线摘要
    latest_row = rows[-1]
    prev_close = rows[-2].close if len(rows) >= 2 else None
    latest = {
        "trade_date": str(latest_row.trade_date),
        "close": latest_row.close,
        "change_pct": round(latest_row.change_pct, 2) if latest_row.change_pct else None,
        "volume": latest_row.volume,
        "amount": latest_row.amount,
        "high": latest_row.high,
        "low": latest_row.low,
        "open": latest_row.open,
    }
    if prev_close and latest_row.close:
        latest["change"] = round(latest_row.close - prev_close, 2)

    # 板块基本信息
    sector_info = {
        "sector_code": rows[-1].sector_code,
        "sector_name": rows[-1].sector_name,
        "sector_type": rows[-1].sector_type,
        "data_range": f"{rows[0].trade_date}~{rows[-1].trade_date}",
        "data_count": len(rows),
    }

    return {
        "sector_code": sector_code,
        "sector_info": sector_info,
        "kline": kline_list,
        "ma": ma_result,
        "latest": latest,
    }


@router.get("/kline/batch")
async def sector_kline_batch(
    sector_type: str = Query("concept", description="板块类型: concept/industry"),
    days: int = Query(5, ge=1, le=30, description="历史天数(轻量, 仅最近N天)"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=50, description="每页条数"),
    db: AsyncSession = Depends(get_db),
):
    """板块K线批量摘要 — 板块营地列表用

    每个板块仅返回最近N天的收盘价+涨跌幅, 用于列表展示迷你K线
    不返回完整OHLCV, 控制数据量
    """
    from datetime import timedelta

    end_date = date.today()
    start_date = end_date - timedelta(days=int(days * 1.8))

    # 获取指定类型的板块列表(排除无业务关联板块)
    si_result = await db.execute(
        select(SectorInfo.sector_code, SectorInfo.sector_name, SectorInfo.sector_type)
        .where(
            and_(
                SectorInfo.source == "pywencai",
                SectorInfo.sector_type == sector_type,
                SectorInfo.is_excluded == 0,
            )
        )
        .order_by(SectorInfo.sector_name)
    )
    sector_list = si_result.all()

    # 分页
    total = len(sector_list)
    start = (page - 1) * page_size
    page_sectors = sector_list[start:start + page_size]

    # 批量获取K线摘要
    sector_codes = [s.sector_code for s in page_sectors]

    result = await db.execute(
        select(SectorKline)
        .where(
            and_(
                SectorKline.sector_code.in_(sector_codes),
                SectorKline.sector_type == sector_type,
                SectorKline.trade_date >= start_date,
            )
        )
        .order_by(SectorKline.sector_code, SectorKline.trade_date)
    )
    kline_rows = result.scalars().all()

    # 按板块分组
    kline_map = {}
    for row in kline_rows:
        code = row.sector_code
        if code not in kline_map:
            kline_map[code] = []
        kline_map[code].append({
            "trade_date": str(row.trade_date),
            "close": row.close,
            "change_pct": round(row.change_pct, 2) if row.change_pct else None,
        })

    # 构建结果
    items = []
    for s in page_sectors:
        mini_kline = kline_map.get(s.sector_code, [])
        latest = mini_kline[-1] if mini_kline else None
        items.append({
            "sector_code": s.sector_code,
            "sector_name": s.sector_name,
            "sector_type": s.sector_type,
            "latest": latest,
            "mini_kline": mini_kline[-days:] if len(mini_kline) > days else mini_kline,
            "kline_count": len(mini_kline),
        })

    return {
        "sector_type": sector_type,
        "days": days,
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": items,
    }
