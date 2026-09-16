"""牛股雷达 API — 5Tab数据驱动

Tab 1: 异动监控 — 全市场异动扫描+分类
Tab 2: 强势排行 — 短线BullScore v2.0(7维) + 十倍潜力TenbaggerModel(5维)
Tab 3: 龙头追踪 — 板块龙头扫描
Tab 4: 共振分析 — 个股-板块共振
Tab 5: 明日预案 — 竞价+评分+技术位
"""

import asyncio
import copy
import hashlib
import json
import re
import time
import uuid
from collections import OrderedDict
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from weakref import WeakKeyDictionary
from fastapi import APIRouter, Depends, Query
from loguru import logger
from sqlalchemy import select, desc, func, and_, or_
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.db.session import get_db
from app.core.stock_tagger import stock_tagger
from app.core.data_date import resolve_latest_trade_date
from app.core.price_limit_rules import limit_up_change_threshold
from app.core.trade_calendar import trade_calendar, is_official_closed_day
from app.data.sources.eastmoney_source import EastMoneySource
from app.data.fund_flow_clock import evidence_clock, fund_clock_status, main_fund_values_valid, verified_fund_clocks
from app.data.main_fund_window import load_main_fund_window, fund_window_payload
from app.signal.anomaly_scanner import (
    anomaly_scanner,
    AnomalyEvent,
    _build_large_order_inflow_context,
    _is_causal_trade_driver_sector,
    _is_main_wave_core_sector_factor,
)
from app.signal.tenbagger_model import tenbagger_model
from app.signal.bull_score import BullScoreModel
from app.signal.b1_signal import analyze_b1_signal
from app.signal.next_day_plan import next_day_plan_engine
from app.signal.long_cycle_profile import build_long_cycle_profile
from app.models.stock import (
    StockSpot,
    StockKline,
    FundFlow,
    StockTag,
    SectorPersistence,
    StockSectorMapping,
    LimitUpPool,
    LimitDownPool,
)
from app.models.sector import SectorLifecycle
from app.models.governance import DashboardSnapshot
from app.models.signal import AnomalyCandidateRecord, SignalPerformance
from app.news.catalyst import load_direct_stock_catalyst_map
from app.push.channels.base import PushMessage
from app.push.channels import feishu_channel, ws_channel
from app.push.scheduler import push_scheduler
from app.push.templates.anomaly import (
    limit_up_alert,
    limit_down_alert,
    capital_anomaly_alert,
    breakthrough_alert,
    b1_state_alert,
)

router = APIRouter()

# BullScoreModel v2.0实例(直接从spot字段算，无需逐只6次DB查询)
_bull_model = BullScoreModel()
_ANOMALY_SNAPSHOT_CACHE: dict[str, dict] = {}
ANOMALY_SNAPSHOT_VERSION = "v20_fund_window_streak_v1"
ANOMALY_CACHE_TTL_SECONDS = 45
# Read-only monitor reuse, never the validity period of a signal or funding frame.
ANOMALY_MONITOR_CLOSED_CACHE_TTL_SECONDS = 600
_ANOMALY_SCAN_LOCKS = WeakKeyDictionary()
_B1_ANALYSIS_CACHE: OrderedDict = OrderedDict()
_B1_ANALYSIS_CACHE_MAX_ENTRIES = 2048
EASTMONEY_MAIN_FUND_SNAPSHOT_VERSION = "v2_source_clock"
EASTMONEY_MAIN_FUND_CACHE_TTL_SECONDS = 60
_EASTMONEY_MAIN_FUND_SNAPSHOT_CACHE: dict[str, dict] = {}

# 强势排行缓存
_RANK_CACHE: dict = {}
_DRAGON_CACHE: dict = {}
_PLAN_CACHE: dict = {}
_DYNAMIC_SECTOR_CORE_REFRESH_STATE: dict[str, float | str] = {
    "trade_date": "",
    "refreshed_monotonic": 0.0,
}
_DYNAMIC_SECTOR_CORE_REFRESH_INTERVAL_SECONDS = 60.0
RANK_SNAPSHOT_VERSION = "v6"  # Five confirmed closed fund sessions; not 7/10 calendar days.
DRAGON_SNAPSHOT_VERSION = "v6_strict_current_fund_v1"
PLAN_SNAPSHOT_VERSION = "v58"
_PLAN_COMPATIBLE_SNAPSHOT_VERSIONS = ("v58",)
_RANK_CACHE_TTL = 600  # 秒
_DRAGON_CACHE_TTL = 300  # 秒
_PLAN_CACHE_TTL = 600  # 秒
_last_rank_fetch: dict = {"bull": 0, "tenbagger": 0}

_MARKET_DIRECT_UP_RATIO = 0.88
_MARKET_DIRECT_AVG_CHANGE = 1.50
_MARKET_DIRECT_RET5 = 4.0
_MARKET_DIRECT_RET20_MIN = 2.0
_MARKET_DIRECT_RET20_MAX = 12.0

B1_STATUS_INTRADAY_PREVIEW = "intraday_preview"
B1_STATUS_CLOSE_CONFIRMED = "close_confirmed"
B1_STATUS_INVALIDATED = "invalidated"
B1_STATUS_LABELS = {
    B1_STATUS_INTRADAY_PREVIEW: "盘中预判",
    B1_STATUS_CLOSE_CONFIRMED: "收盘确认",
    B1_STATUS_INVALIDATED: "信号失效",
}


_MAIN_BOARD_CODE_RE = re.compile(r"^(?:600|601|603|605|000|001|002|003)\d{3}$")


def _is_main_board_code(code: str) -> bool:
    return bool(_MAIN_BOARD_CODE_RE.fullmatch(str(code or "").strip().zfill(6)))


def _is_st_name(name: str) -> bool:
    return str(name or "").strip().upper().startswith(("ST", "*ST"))


def _filter_main_board_items(items: list[dict], code_field: str = "code") -> list[dict]:
    """常规牛股雷达只返回可交易沪深主板；ST继续由风控链路屏蔽。"""
    return [
        item
        for item in items
        if _is_main_board_code(str(item.get(code_field) or ""))
        and stock_tagger.is_tradeable(str(item.get(code_field) or ""))
        and not str(item.get("name") or "").upper().startswith(("ST", "*ST", "退"))
    ]


def _filter_plan_main_board_items(items: list[dict], code_field: str = "code") -> list[dict]:
    """明日预案允许主板ST研究样本展示，但它们必须保持零仓位。"""
    result: list[dict] = []
    for item in items:
        code = str(item.get(code_field) or "").strip().zfill(6)
        name = str(item.get("name") or "").strip()
        if not _is_main_board_code(code) or name.upper().startswith("退"):
            continue
        if _is_st_name(name):
            if item.get("research_only") and item.get("is_tradeable") is False:
                result.append(item)
            continue
        if stock_tagger.is_tradeable(code) and item.get("is_tradeable") is not False:
            result.append(item)
    return result


def _anomaly_snapshot_key() -> str:
    return f"tenbagger-anomalies:{ANOMALY_SNAPSHOT_VERSION}"


def _eastmoney_main_fund_snapshot_key() -> str:
    return f"tenbagger-eastmoney-main-fund:{EASTMONEY_MAIN_FUND_SNAPSHOT_VERSION}"


def _rank_snapshot_key(mode: str, limit: int | None = None) -> str:
    return f"tenbagger-rank:{mode}:{RANK_SNAPSHOT_VERSION}"


def _dragon_snapshot_key(limit: int = 50) -> str:
    return f"tenbagger-dragon:{limit}:{DRAGON_SNAPSHOT_VERSION}"


def _plan_snapshot_key(limit: int = 20) -> str:
    return f"tenbagger-plan:{limit}:{PLAN_SNAPSHOT_VERSION}"


def _snapshot_cache_key(snapshot_key: str, trade_date: date) -> str:
    return f"{snapshot_key}:{trade_date}"


def _get_cached_payload(cache_store: dict, snapshot_key: str, trade_date: date, ttl_seconds: int):
    cache_id = _snapshot_cache_key(snapshot_key, trade_date)
    cached = cache_store.get(cache_id)
    if not cached:
        return None
    if time.time() - cached["ts"] > ttl_seconds:
        cache_store.pop(cache_id, None)
        return None
    return cached["payload"]


def _set_cached_payload(cache_store: dict, snapshot_key: str, trade_date: date, payload: dict):
    cache_store[_snapshot_cache_key(snapshot_key, trade_date)] = {
        "ts": time.time(),
        "payload": payload,
    }


async def _get_persisted_dashboard_snapshot(
    db: AsyncSession,
    snapshot_key: str,
    trade_date: date,
    ttl_seconds: int,
):
    result = await db.execute(
        select(DashboardSnapshot)
        .where(
            and_(
                DashboardSnapshot.snapshot_key == snapshot_key,
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
    if (datetime.now() - row.snapshot_time).total_seconds() > ttl_seconds:
        return None
    try:
        payload = json.loads(row.payload_json)
        if isinstance(payload, dict):
            return payload
    except Exception:
        return None
    return None


async def _get_latest_persisted_dashboard_snapshot(
    db: AsyncSession,
    snapshot_key: str,
    trade_date: date,
    *,
    as_of_at: datetime | None = None,
):
    result = await db.execute(
        select(DashboardSnapshot)
        .where(
            and_(
                DashboardSnapshot.snapshot_key == snapshot_key,
                DashboardSnapshot.trade_date == trade_date,
                DashboardSnapshot.status == "ok",
                DashboardSnapshot.snapshot_time <= as_of_at if as_of_at is not None else True,
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
        if isinstance(payload, dict):
            return payload
    except Exception:
        return None
    return None


async def _get_latest_compatible_plan_snapshot(
    db: AsyncSession,
    trade_date: date,
    limit: int,
) -> dict | None:
    """读取同版本、同交易日的最新预案快照，避免按 limit 重复全市场计算。"""
    # 使用确定键命中联合索引；SQLite 对 LIKE 前缀在当前排序规则下会退化为全表扫描。
    compatible_keys = list(dict.fromkeys(
        f"tenbagger-plan:{size}:{version}"
        for version in _PLAN_COMPATIBLE_SNAPSHOT_VERSIONS
        for size in (limit, 20, 100, 500)
    ))
    result = await db.execute(
        select(DashboardSnapshot)
        .where(
            and_(
                DashboardSnapshot.snapshot_key.in_(compatible_keys),
                DashboardSnapshot.trade_date == trade_date,
                DashboardSnapshot.status == "ok",
            )
        )
        .order_by(desc(DashboardSnapshot.snapshot_time))
        .limit(8)
    )
    for row in result.scalars().all():
        try:
            payload = json.loads(row.payload_json)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if not isinstance(payload.get("plans"), list):
            continue

        compatible = copy.deepcopy(payload)
        compatible["plans"] = compatible["plans"][:max(1, limit)]
        age_seconds = max(0.0, (datetime.now() - row.snapshot_time).total_seconds())
        compatible["served_from_snapshot"] = True
        compatible["source_snapshot_key"] = row.snapshot_key
        compatible["snapshot_age_seconds"] = round(age_seconds, 1)
        compatible["snapshot_stale"] = age_seconds > _PLAN_CACHE_TTL
        return compatible
    return None


async def _build_next_day_plan_replay(
    db: AsyncSession,
    actual_trade_date: date,
    *,
    limit: int = 20,
) -> dict:
    """复盘上一交易日明日预案，不把结果字段回灌到预测快照。"""
    previous_trade_date = (
        await db.execute(
            select(func.max(StockKline.trade_date)).where(
                StockKline.trade_date < actual_trade_date,
            )
        )
    ).scalar_one_or_none()
    if previous_trade_date is None:
        return {
            "status": "missing_previous_trade_date",
            "prediction_trade_date": None,
            "actual_trade_date": str(actual_trade_date),
            "predicted_count": 0,
        }

    snapshot = await _get_latest_compatible_plan_snapshot(
        db,
        previous_trade_date,
        max(20, limit),
    )
    plans = _filter_main_board_items(list((snapshot or {}).get("plans") or []))
    if not plans:
        return {
            "status": "missing_prediction_snapshot",
            "prediction_trade_date": str(previous_trade_date),
            "actual_trade_date": str(actual_trade_date),
            "predicted_count": 0,
        }

    codes = [str(item.get("code") or "").strip() for item in plans if item.get("code")]
    kline_result = await db.execute(
        select(StockKline.code, StockKline.change_pct).where(
            StockKline.trade_date == actual_trade_date,
            StockKline.code.in_(codes),
        )
    )
    close_change_map = {
        str(code or "").strip(): _safe_float(change_pct)
        for code, change_pct in kline_result.all()
        if str(code or "").strip()
    }
    limit_up_codes = {
        str(code or "").strip()
        for code in (
            await db.execute(
                select(LimitUpPool.code).where(
                    LimitUpPool.trade_date == actual_trade_date,
                )
            )
        ).scalars().all()
        if str(code or "").strip() and stock_tagger.is_tradeable(str(code or "").strip())
    }

    items: list[dict] = []
    for plan in plans:
        code = str(plan.get("code") or "").strip()
        close_change_pct = close_change_map.get(code)
        actionable = _next_day_plan_action_priority(plan) <= 3
        items.append(
            {
                "code": code,
                "name": str(plan.get("name") or ""),
                "candidate_source": str(plan.get("candidate_source") or ""),
                "candidate_source_label": str(plan.get("candidate_source_label") or ""),
                "recommended_action": str(plan.get("recommended_action") or ""),
                "actionable": actionable,
                "actual_close_change_pct": (
                    round(close_change_pct, 3)
                    if close_change_pct is not None
                    else None
                ),
                "rising_hit": bool(close_change_pct is not None and close_change_pct > 0),
                "strong_rising_hit": bool(close_change_pct is not None and close_change_pct >= 5.0),
                "limit_up_hit": code in limit_up_codes,
            }
        )

    evaluated_items = [item for item in items if item.get("actual_close_change_pct") is not None]
    actionable_items = [item for item in evaluated_items if item.get("actionable")]
    limit_up_hits = [item for item in evaluated_items if item.get("limit_up_hit")]
    rising_hits = [item for item in evaluated_items if item.get("rising_hit")]
    actionable_limit_up_hits = [item for item in actionable_items if item.get("limit_up_hit")]
    actionable_rising_hits = [item for item in actionable_items if item.get("rising_hit")]
    return {
        "status": "ok",
        "prediction_trade_date": str(previous_trade_date),
        "actual_trade_date": str(actual_trade_date),
        "snapshot_time": str((snapshot or {}).get("snapshot_time") or ""),
        "predicted_count": len(evaluated_items),
        "actionable_predicted_count": len(actionable_items),
        "actual_mainboard_limit_up_count": len(limit_up_codes),
        "limit_up_hit_count": len(limit_up_hits),
        "actionable_limit_up_hit_count": len(actionable_limit_up_hits),
        "rising_hit_count": len(rising_hits),
        "actionable_rising_hit_count": len(actionable_rising_hits),
        "limit_up_precision": round(len(limit_up_hits) / len(evaluated_items), 4) if evaluated_items else 0.0,
        "limit_up_recall": round(len(limit_up_hits) / len(limit_up_codes), 4) if limit_up_codes else 0.0,
        "directional_precision": round(len(rising_hits) / len(evaluated_items), 4) if evaluated_items else 0.0,
        "actionable_limit_up_precision": round(len(actionable_limit_up_hits) / len(actionable_items), 4) if actionable_items else 0.0,
        "actionable_directional_precision": round(len(actionable_rising_hits) / len(actionable_items), 4) if actionable_items else 0.0,
        "limit_up_hits": limit_up_hits,
        "items": items,
        "evaluation_note": "原始预案覆盖命中与可执行命中分开统计；结果只用于复盘，不回灌预测快照。",
    }


_PATTERN_POOL_STAGE_LABELS = {
    "armed_pullback": "回踩确认准备",
    "armed_main_wave_shape_pullback": "首阴/十字星回踩准备",
    "armed_breakout": "突破确认准备",
    "armed_second_wave_reset": "高标二波准备",
    "armed_sector_core_laggard": "主线补涨准备",
    "confirmed": "形态已确认",
}

_PATTERN_POOL_SOURCE_GROUPS = {
    "tenbagger_pullback_watch": ("潜力回踩", "高潜力评分只负责入池；回踩支撑后须有滚动60秒价升量增、VWAP修复及盘口/资金/主驱动至少双确认"),
    "main_wave_pullback_pattern": ("主升回踩", "缩量价稳后触及形态低点；有效产业板块、核心身份/盘口、主力与大单金额、滚动60秒增量成交全部确认"),
    "second_wave_pattern": ("二波跟踪", "回踩支撑不破后放量转强，或放量突破压力并获得盘口/资金确认"),
    "high_board_turnover_second_wave": ("二波跟踪", "高标换手充分后放量转强，且板块强度与盘口承接同步确认"),
    "second_wave_reset_watch": ("高标重置", "止跌急拉并出现真实增量成交，盘口、资金、板块至少双确认"),
    "sector_repair_reversal": ("超跌修复", "支撑企稳后放量修复，产业链强度和主动资金再次确认"),
    "old_hot_oversold_repair": ("超跌修复", "旧高标止跌后放量回收关键价，盘口承接和资金流同步改善"),
    "event_first_board": ("事件接力", "硬事件持续有效，竞价承接、换手、板块前排和盘口二次确认"),
    "event_high_board": ("事件接力", "事件逻辑未证伪，高标换手和封单质量通过次日确认"),
    "second_board_relay": ("冲二板", "首板质量入选；次日竞价、前板价承接、换手放量和题材前排二次确认"),
    "leader_linkage_follow": ("A→B联动", "龙头A保持强势，B同产业链且自身放量突破或支撑回收"),
    "sector_core_laggard": ("主线补涨", "只跟踪强主线的多重成分股；滚动60秒价升量增、VWAP和盘口/资金确认后才提示"),
}


def _pattern_pool_source_meta(source: str) -> tuple[str, str]:
    if source in _PATTERN_POOL_SOURCE_GROUPS:
        return _PATTERN_POOL_SOURCE_GROUPS[source]
    if source.startswith("pre_board_"):
        return "低位启动", "支撑回收或放量突破压力，且成交增量、盘口主动性和题材至少双确认"
    if source.startswith("high_board_"):
        return "高标启动", "换手充分后带量突破，封单质量、盘口承接和题材强度同步确认"
    if source in {"trend_driver_setup", "trend_main_wave_pattern", "main_wave_pattern", "platform_breakout_pattern"}:
        return "趋势驱动", "回踩趋势支撑不破后放量转强，或带量突破平台压力并站稳VWAP"
    return "形态跟踪", "支撑不破并出现真实增量成交，盘口、资金或板块至少双确认"


def _format_pattern_pool_rows(
    trend_pool: list[dict],
    spot_map: dict[str, dict] | None = None,
    context_map: dict[str, dict] | None = None,
) -> list[dict]:
    """把内部动态检测池转换为前端只读形态池；形态命中不等于可执行买点。"""
    spot_map = spot_map or {}
    context_map = context_map or {}
    rows: list[dict] = []
    seen_codes: set[str] = set()
    for item in trend_pool or []:
        code = str(item.get("code") or "").strip()
        if not code or code in seen_codes:
            continue
        seen_codes.add(code)
        spot = spot_map.get(code) or {}
        context = context_map.get(code) or {}
        stats = item.get("main_wave_stats") or {}
        source = str(item.get("candidate_source") or "")
        source_group, trigger_condition = _pattern_pool_source_meta(source)
        setup_state = str(item.get("setup_state") or stats.get("setup_state") or "")
        support = _safe_float(item.get("support"), _safe_float(stats.get("support")))
        resistance = _safe_float(item.get("resistance"), _safe_float(stats.get("resistance")))
        price = _safe_float(spot.get("price"), _safe_float(item.get("price")))
        limit_up = _safe_float(spot.get("limit_up"), _safe_float(item.get("limit_up")))
        is_limit_up = bool(item.get("is_limit_up")) or bool(
            price > 0 and limit_up > 0 and price >= limit_up * 0.995
        )
        strict_kline_ready = bool(
            source == "main_wave_pullback_pattern"
            and str(stats.get("pullback_quality_tier") or "") == "strict"
        )
        reclaim_kline_ready = bool(
            source == "main_wave_pullback_pattern"
            and str(stats.get("pullback_quality_tier") or "") == "reclaim"
        )
        strict_setup_ready = bool(
            strict_kline_ready
            and context.get("core_sector_confirmed")
            and context.get("core_identity_confirmed")
            and context.get("large_order_inflow_confirmed")
        )
        long_cycle_regime = str(stats.get("long_cycle_regime") or "")
        long_cycle_label = str(stats.get("long_cycle_regime_label") or "")
        long_cycle_score = _safe_float(stats.get("quality_setup_score"))
        long_cycle_shape_ready = bool(
            stats.get("pre_board_long_cycle_ready", stats.get("shape_ready"))
        )
        if is_limit_up:
            monitor_status = "形态命中·当前不可追"
        elif source == "tenbagger_pullback_watch":
            monitor_status = "高潜力·待低吸A2"
        elif source == "main_wave_pullback_pattern" and strict_setup_ready:
            monitor_status = "严格候选·待60秒A2"
        elif source == "main_wave_pullback_pattern" and strict_kline_ready:
            monitor_status = "缩量价稳·待核心/大单"
        elif source == "main_wave_pullback_pattern" and reclaim_kline_ready:
            monitor_status = "深洗首阴·待收回昨收"
        elif source == "main_wave_pullback_pattern":
            monitor_status = "普通首阴·仅观察"
        elif source == "sector_core_laggard":
            monitor_status = "主线扩散·待60秒A2"
        elif setup_state:
            monitor_status = "形态已入池·等待A2"
        else:
            monitor_status = "检测池观察·等待确认"
        invalidation = (
            f"跌破支撑 {support:.2f}，或急拉无真实增量成交"
            if support > 0
            else "跌破形态低点，或急拉无真实增量成交"
        )
        rows.append({
            "code": code,
            "name": spot.get("name") or item.get("name") or code,
            "price": round(price, 3) if price > 0 else None,
            "change_pct": round(_safe_float(spot.get("change_pct"), _safe_float(item.get("change_pct"))), 2),
            "volume_ratio": round(_safe_float(spot.get("volume_ratio"), _safe_float(item.get("volume_ratio"))), 2),
            "turnover": round(_safe_float(spot.get("turnover"), _safe_float(item.get("turnover"))), 2),
            "pattern_score": round(_safe_float(item.get("score")), 1),
            "pattern_label": item.get("label") or source_group,
            "candidate_source": source,
            "source_group": source_group,
            "setup_state": setup_state,
            "stage_label": _PATTERN_POOL_STAGE_LABELS.get(setup_state, "等待盘中确认"),
            "monitor_status": monitor_status,
            "pattern_quality_tier": (
                "potential"
                if source == "tenbagger_pullback_watch"
                else str(stats.get("pullback_quality_tier") or "watch")
                if source == "main_wave_pullback_pattern"
                else "strict"
                if long_cycle_shape_ready and source.startswith("pre_board_")
                else "standard"
            ),
            "pattern_quality_label": (
                f"潜力{_safe_float(stats.get('tenbagger_score')):.0f}分·低吸等待"
                if source == "tenbagger_pullback_watch"
                else str(stats.get("pullback_quality_label") or "普通形态观察")
                if source == "main_wave_pullback_pattern"
                else f"长周期{long_cycle_score:.0f}分·待量价确认"
                if source.startswith("pre_board_") and long_cycle_label
                else "标准形态观察"
            ),
            "long_cycle_regime": long_cycle_regime,
            "long_cycle_regime_label": long_cycle_label,
            "long_cycle_quality_score": round(long_cycle_score, 1),
            "long_cycle_shape_ready": long_cycle_shape_ready,
            "long_cycle_setup_phase": str(stats.get("setup_phase") or ""),
            "long_cycle_setup_phase_label": str(stats.get("setup_phase_label") or ""),
            "position_120": _safe_float(stats.get("position_120"), _safe_float(stats.get("pos_120"))),
            "position_250": _safe_float(stats.get("position_250")),
            "probe_count_20": _safe_int(stats.get("probe_count_20")),
            "board_like_count_120": _safe_int(stats.get("board_like_count_120")),
            "days_since_last_board_like": _safe_int(stats.get("days_since_last_board_like"), 999),
            "pullback_day_change_pct": _safe_float(stats.get("pullback_day_change_pct")),
            "pullback_volume_contraction": _safe_float(stats.get("pullback_volume_contraction")),
            "core_sector_confirmed": bool(context.get("core_sector_confirmed")),
            "core_sector_name": str(context.get("core_sector_name") or ""),
            "sector_leader_confirmed": bool(context.get("sector_leader_confirmed")),
            "core_identity_confirmed": bool(context.get("core_identity_confirmed")),
            "main_net_inflow": _safe_float(context.get("main_net_inflow")),
            "main_net_inflow_pct": _safe_float(context.get("main_net_inflow_pct")),
            "large_order_net_inflow": _safe_float(context.get("large_order_net_inflow")),
            "large_order_inflow_confirmed": bool(context.get("large_order_inflow_confirmed")),
            "strict_setup_ready": strict_setup_ready,
            "support": round(support, 3) if support > 0 else None,
            "resistance": round(resistance, 3) if resistance > 0 else None,
            "trigger_condition": trigger_condition,
            "invalidation": invalidation,
            "position_ratio": "0",
            "feishu_monitoring": True,
            "push_policy": (
                "支撑回收+60秒增量成交确认后推送"
                if source == "tenbagger_pullback_watch"
                else
                "核心板块+大单+60秒确认后推送"
                if source == "main_wave_pullback_pattern" and not reclaim_kline_ready
                else "收回首阴收盘价+60秒增量成交+板块/盘口确认后推送"
                if source == "main_wave_pullback_pattern"
                else "60秒量价+VWAP+产业/盘口确认后推送"
                if source.startswith("pre_board_")
                else "到达A2买点后飞书提醒"
            ),
            "is_limit_up": is_limit_up,
            "main_wave_stats": stats,
        })

    source_priority = {
        "tenbagger_pullback_watch": 10,
        "second_board_relay": 11,
        "second_wave_reset_watch": 10,
        "second_wave_pattern": 9,
        "high_board_turnover_second_wave": 9,
        "pre_board_momentum_shakeout": 9,
        "pre_board_probe_wash": 8,
        "platform_breakout_pattern": 7,
        "trend_driver_setup": 6,
        "sector_repair_reversal": 5,
        "old_hot_oversold_repair": 5,
    }
    rows.sort(
        key=lambda item: (
            source_priority.get(str(item.get("candidate_source") or ""), 0),
            _safe_float(item.get("pattern_score")),
        ),
        reverse=True,
    )
    return rows


async def _build_pattern_pool_rows(
    db: AsyncSession,
    trend_pool: list[dict],
    target_date: date | None = None,
) -> list[dict]:
    codes = list(dict.fromkeys(
        str(item.get("code") or "") for item in (trend_pool or []) if item.get("code")
    ))
    if not codes:
        return []
    result = await db.execute(
        select(
            StockSpot.code,
            StockSpot.name,
            StockSpot.price,
            StockSpot.change_pct,
            StockSpot.volume_ratio,
            StockSpot.turnover,
            StockSpot.limit_up,
            StockSpot.amount,
        ).where(StockSpot.code.in_(codes))
    )
    spot_map = {
        str(row[0]): {
            "name": row[1],
            "price": row[2],
            "change_pct": row[3],
            "volume_ratio": row[4],
            "turnover": row[5],
            "limit_up": row[6],
            "amount": row[7],
        }
        for row in result.all()
    }
    effective_date = target_date or await db.scalar(
        select(func.max(SectorPersistence.trade_date))
    )
    context_map: dict[str, dict] = {code: {} for code in codes}
    if effective_date:
        from app.data.main_fund import load_current_main_fund_map
        current_funds = await load_current_main_fund_map(
            db, trade_date=effective_date, decision_at=datetime.now(), codes=codes,
        )
        for code, row in current_funds.items():
            fund = {
                **row,
                "provider_source": row.get("source"),
                "source": "fund_flow" if row.get("source") == "tencent" else "eastmoney_main_fund",
                "is_stale": False,
                "clock_status": "ok",
                "as_of": row.get("source_quote_at"),
            }
            context_map[str(code)].update({
                "main_net_inflow": row.get("main_net_inflow"),
                "main_net_inflow_pct": row.get("main_net_inflow_pct"),
                **_build_large_order_inflow_context(
                    fund,
                    traded_amount=_safe_float((spot_map.get(str(code)) or {}).get("amount")),
                ),
            })

        mapping_result = await db.execute(
            select(
                StockSectorMapping.code,
                StockSectorMapping.sector_code,
                StockSectorMapping.sector_name,
                StockSectorMapping.sector_type,
                StockSectorMapping.source,
            ).where(StockSectorMapping.code.in_(codes))
        )
        mappings = mapping_result.all()
        sector_codes = list(dict.fromkeys(row[1] for row in mappings if row[1]))
        persistence_map: dict[str, SectorPersistence] = {}
        lifecycle_map: dict[str, SectorLifecycle] = {}
        if sector_codes:
            persistence_result = await db.execute(
                select(SectorPersistence).where(
                    SectorPersistence.trade_date == effective_date,
                    SectorPersistence.sector_code.in_(sector_codes),
                )
            )
            persistence_map = {
                str(item.sector_code): item
                for item in persistence_result.scalars().all()
            }
            lifecycle_result = await db.execute(
                select(SectorLifecycle).where(
                    SectorLifecycle.trade_date == effective_date,
                    SectorLifecycle.sector_code.in_(sector_codes),
                )
            )
            lifecycle_map = {
                str(item.sector_code): item
                for item in lifecycle_result.scalars().all()
            }

        for code, sector_code, sector_name, sector_type, source in mappings:
            persistence = persistence_map.get(str(sector_code))
            lifecycle = lifecycle_map.get(str(sector_code))
            leader_codes: set[str] = set()
            try:
                leaders = json.loads(getattr(lifecycle, "leader_stocks", "") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                leaders = []
            for leader in leaders if isinstance(leaders, list) else []:
                leader_code = str((leader or {}).get("code") or "")
                if leader_code:
                    leader_codes.add(leader_code)
            factor = {
                "sector_name": sector_name or sector_code,
                "sector_type": sector_type or "",
                "source": source or "",
                "change_pct": _safe_float(getattr(persistence, "change_pct", 0)),
                "fund_flow": _safe_float(getattr(persistence, "fund_flow", 0)),
                "strength_score": _safe_float(getattr(persistence, "strength_score", 0)),
                "limit_up_count": _safe_int(getattr(persistence, "limit_up_count", 0)),
                "lifecycle_state": str(getattr(lifecycle, "lifecycle_state", "") or ""),
                "lifecycle_score": _safe_float(getattr(lifecycle, "state_score", 0)),
                "active_days": _safe_int(getattr(lifecycle, "active_days", 0)),
                "is_sector_leader": str(code) in leader_codes,
            }
            if not _is_main_wave_core_sector_factor(factor):
                continue
            current = context_map[str(code)].get("core_sector_factor") or {}
            rank = (
                bool(factor["is_sector_leader"]),
                factor["lifecycle_score"],
                factor["strength_score"],
                factor["limit_up_count"],
            )
            current_rank = (
                bool(current.get("is_sector_leader")),
                _safe_float(current.get("lifecycle_score")),
                _safe_float(current.get("strength_score")),
                _safe_int(current.get("limit_up_count")),
            )
            if not current or rank > current_rank:
                context_map[str(code)].update({
                    "core_sector_confirmed": True,
                    "core_sector_name": str(factor["sector_name"]),
                    "sector_leader_confirmed": bool(factor["is_sector_leader"]),
                    "core_identity_confirmed": bool(factor["is_sector_leader"]),
                    "core_sector_factor": factor,
                })

    return _format_pattern_pool_rows(trend_pool, spot_map, context_map)


async def _persist_dashboard_snapshot(
    db: AsyncSession,
    snapshot_key: str,
    trade_date: date,
    payload: dict,
) -> bool:
    payload = _normalize_json_payload(payload)
    payload_json = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str)
    result = await db.execute(
        select(DashboardSnapshot)
        .where(
            and_(
                DashboardSnapshot.snapshot_key == snapshot_key,
                DashboardSnapshot.trade_date == trade_date,
            )
        )
        .order_by(desc(DashboardSnapshot.snapshot_time))
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = DashboardSnapshot(
            snapshot_key=snapshot_key,
            trade_date=trade_date,
            snapshot_time=datetime.now(),
            payload_json=payload_json,
            status="ok",
        )
        db.add(row)
    else:
        row.snapshot_time = datetime.now()
        row.payload_json = payload_json
        row.status = "ok"
    try:
        await db.flush()
        await db.commit()
        return True
    except OperationalError as exc:
        await db.rollback()
        logger.warning(f"dashboard_snapshot 持久化跳过，数据库正忙: {snapshot_key} {trade_date} {exc}")
        return False


def _normalize_fund_code(value: str | None) -> str:
    return str(value or "").strip().zfill(6)


def _build_eastmoney_main_fund_items(df, *, trade_date: date | None = None,
                                    observed_at: datetime | None = None) -> dict[str, dict]:
    """Compatibility parser; the scheduler owns the sole ingestion contract."""
    from app.data.scheduler import DataScheduler

    if df is None or getattr(df, "empty", True):
        return {}
    current = observed_at if observed_at is not None else datetime.now()
    target_date = trade_date if trade_date is not None else current.date()
    records = DataScheduler._parse_individual_fund_flow_df(
        df, target_date, observed_at=current,
    )
    items = {}
    for row in records:
        item = dict(row)
        item["date"] = item.pop("trade_date").isoformat()
        for key in ("source_quote_at", "received_at", "observed_at"):
            item[key] = item[key].isoformat()
        item.update({
            "provider_source": row["source"],
            "source": "eastmoney_main_fund",
            "as_of": item["source_quote_at"],
            "clock_basis": "provider_quote_update_f124",
            "clock_status": "ok",
            "is_stale": False,
        })
        items[row["code"]] = item
    return items


def _recheck_main_fund_snapshot_clocks(payload: dict, trade_date: date,
                                      now: datetime | None = None) -> dict:
    """Cache TTL is not source freshness; never refresh any stored clock."""
    current = now if now is not None else datetime.now()
    result = dict(payload)
    result["items"] = {}
    for code, raw in (payload.get("items") or {}).items():
        item = dict(raw)
        item["clock_status"] = fund_clock_status(
            item.get("source_quote_at"), item.get("received_at"), item.get("observed_at"),
            trade_date, current, settings.FUND_FLOW_SOURCE_MAX_AGE_SEC,
        )
        item["is_stale"] = bool(
            item.get("is_stale") or item["clock_status"] != "ok"
            or not main_fund_values_valid(item.get("main_net_inflow"), item.get("main_net_inflow_pct"))
        )
        result["items"][code] = item
    return result


async def prewarm_eastmoney_main_fund_snapshot(
    db: AsyncSession,
    trade_date: date | None = None,
    force_refresh: bool = False,
) -> dict:
    """Read-only projection of scheduler-owned FundFlow; never fetch or write.

    The scheduler collects every 30s during trading hours. Startup/API prewarm
    no longer supplies an independent fetch: until that job commits a qualified
    frame (or when the scheduler is disabled), current funds remain unavailable.
    force_refresh is retained for callers but cannot refresh any evidence clock.
    """
    from app.data.main_fund import load_current_main_fund_map

    # Date resolution must not flush an arbitrary caller's pending business ORM
    # changes either; the projection owns no write transaction.
    with db.no_autoflush:
        target_date = await resolve_latest_trade_date(db, FundFlow.trade_date, trade_date)
    decision_at = datetime.now()
    diagnostics = {}
    current = await load_current_main_fund_map(
        db, trade_date=target_date, decision_at=decision_at, diagnostics=diagnostics,
    )
    status_counts = {}
    for status in diagnostics.values():
        status_counts[status] = status_counts.get(status, 0) + 1
    items = {}
    for code, row in current.items():
        item = dict(row)
        for key in ("date", "trade_date", "source_quote_at", "received_at", "observed_at"):
            value = item.get(key)
            if isinstance(value, (date, datetime)):
                item[key] = value.isoformat()
        item.update({
            "provider_source": row.get("source"),
            "source": "fund_flow" if row.get("source") == "tencent" else "eastmoney_main_fund",
            "as_of": item.get("source_quote_at"),
            "clock_basis": (row.get("source_clock_basis") if row.get("source") == "tencent"
                            else "provider_quote_update_f124"),
            "clock_status": "ok",
            "is_stale": False,
        })
        items[code] = item
    return {
        "trade_date": str(target_date),
        # A projection is not a new observation. Keep the latest actual receipt
        # watermark rather than stamping this API invocation as fresh collection.
        "snapshot_time": max(
            (item.get("observed_at") for item in items.values() if item.get("observed_at")),
            default=None,
        ),
        "source": (("fund_flow" if any(item["source"] == "fund_flow" for item in items.values())
                    else "eastmoney_main_fund") if items else "eastmoney_main_fund_unavailable"),
        "items": items,
        # Counts describe this date's stored rows, NOT stock-universe coverage.
        # An empty current map is unavailable, not a measured aggregate zero.
        "fund_quality": {
            "status": "ok" if items else "unavailable",
            "reason": ("qualified" if items else "invalid_cutoff"
                       if decision_at.date() != target_date else "no_qualified_rows"),
            "decision_at": decision_at.isoformat(),
            "denominator": "observed_fund_flow_rows",
            "query_performed": decision_at.date() == target_date,
            "row_count": len(diagnostics) if decision_at.date() == target_date else None,
            "qualified_count": len(items),
            "status_counts": status_counts,
        },
    }


async def _load_recent_main_fund_map(
    db: AsyncSession,
    target_date: date,
    *,
    as_of_at: datetime | None = None,
) -> dict[str, list[dict]]:
    window = await load_main_fund_window(
        db, through_date=target_date,
        decision_at=as_of_at if as_of_at is not None else datetime.now(),
    )
    # Detectors coerce missing list values to zero. Only a complete, verified
    # five-session sequence may reach them; [] means unavailable, not flat.
    return {
        code: list(reversed(item["history"])) if item["complete"] else []
        for code, item in window["items"].items()
    }


def _is_intraday_plan_date(target_date: date, now: datetime | None = None) -> bool:
    """Whether ``target_date`` still has an unfinished A-share daily bar."""
    current = now or datetime.now()
    return target_date == current.date() and (current.hour, current.minute) < (15, 0)


async def _resolve_stock_plan_dates(
    db: AsyncSession,
    code: str,
    target_date: date,
    *,
    now: datetime | None = None,
) -> dict:
    """Resolve quote and completed-daily-bar dates for a detail plan.

    Intraday quote fields may use the current snapshot, while MA/BOLL/MACD/RSI,
    five-session returns and the five-session fund total must stop at the latest
    completed trading day.  This prevents an unfinished ``spot_fallback`` row
    from leaking into a daily decision baseline.
    """
    current = now or datetime.now()
    provisional = _is_intraday_plan_date(target_date, current)
    statement = select(func.max(StockKline.trade_date)).where(StockKline.code == code)
    if provisional:
        statement = statement.where(StockKline.trade_date < target_date)
    else:
        statement = statement.where(StockKline.trade_date <= target_date)
    technical_trade_date = (await db.execute(statement)).scalar_one_or_none()
    return {
        "quote_trade_date": target_date,
        "technical_trade_date": technical_trade_date,
        "is_intraday_provisional": provisional,
        "trade_session": trade_calendar.get_trade_session(current),
    }


async def _load_stock_change_5d(
    db: AsyncSession,
    code: str,
    cutoff_date: date | None,
) -> dict:
    """Load exactly five completed close-to-close intervals (six closes)."""
    if cutoff_date is None:
        return {"change_pct": None, "interval_count": 0, "start_date": None, "end_date": None}
    result = await db.execute(
        select(StockKline.trade_date, StockKline.close)
        .where(
            StockKline.code == code,
            StockKline.trade_date <= cutoff_date,
        )
        .order_by(desc(StockKline.trade_date))
        .limit(6)
    )
    rows = [(row[0], float(row[1])) for row in result.all() if _safe_float(row[1]) > 0]
    interval_count = max(0, len(rows) - 1)
    change_pct = None
    if len(rows) >= 6:
        change_pct = round((rows[0][1] / rows[5][1] - 1) * 100, 2)
    return {
        "change_pct": change_pct,
        "interval_count": min(interval_count, 5),
        "start_date": rows[min(len(rows) - 1, 5)][0] if rows else None,
        "end_date": rows[0][0] if rows else None,
    }


async def _load_stock_fund_context(
    db: AsyncSession,
    code: str,
    *,
    quote_trade_date: date,
    completed_trade_date: date | None,
    current_snapshot: dict | None = None,
    as_of_at: datetime | None = None,
) -> dict:
    """Load historical research values separately from verified current evidence."""
    decision_at = as_of_at if as_of_at is not None else datetime.now()
    fund_window = await load_main_fund_window(
        db, through_date=completed_trade_date, decision_at=decision_at, codes=[code],
    )
    historical = fund_window["items"].get(code, {})
    fund_5d_total = historical.get("total")

    # A caller-supplied cache cannot establish current funds independently of
    # the authoritative table. Read the same qualified provider as prewarm/B1.
    from app.data.main_fund import load_current_main_fund_map
    current_map = await load_current_main_fund_map(
        db, trade_date=quote_trade_date, decision_at=decision_at, codes=[code],
    )
    current_row = current_map.get(code)
    snapshot = {"trade_date": quote_trade_date, "items": {}}
    if current_row:
        item = dict(current_row)
        for key in ("date", "trade_date", "source_quote_at", "received_at", "observed_at"):
            value = item.get(key)
            if isinstance(value, (date, datetime)):
                item[key] = value.isoformat()
        item.update({
            "provider_source": current_row.get("source"),
            "source": "fund_flow" if current_row.get("source") == "tencent" else "eastmoney_main_fund",
            "as_of": item.get("source_quote_at"),
            "clock_basis": (current_row.get("source_clock_basis") if current_row.get("source") == "tencent"
                            else "provider_quote_update_f124"),
            "clock_status": "ok", "is_stale": False,
        })
        snapshot.update({"source": item["source"], "items": {code: item}})
    snapshot = _recheck_main_fund_snapshot_clocks(snapshot, quote_trade_date, decision_at)
    snapshot_source = str(snapshot.get("source") or "")
    snapshot_items = snapshot.get("items") if isinstance(snapshot.get("items"), dict) else {}
    snapshot_item = _get_current_main_fund_item(snapshot_items, code)
    use_fresh_snapshot = (
        snapshot_source in {"eastmoney_main_fund", "fund_flow"}
        and bool(snapshot_item)
        and snapshot_item.get("main_net_inflow") is not None
        and not snapshot_item.get("is_stale")
    )

    if use_fresh_snapshot:
        current_item = dict(snapshot_item)
        current_source = str(current_item["source"])
        current_trade_date = snapshot.get("trade_date") or quote_trade_date
        current_as_of = current_item.get("as_of") or snapshot.get("snapshot_time")
    else:
        current_item = {}
        current_source = "unavailable"
        current_trade_date = None
        current_as_of = None

    return {
        "fund_5d_total": fund_5d_total,
        **fund_window_payload(fund_window, code),
        "fund_5d_clock_unknown_count": sum(
            count for status, count in historical.get("status_counts", {}).items()
            if status not in {"dated_known", "missing"}
        ),
        "fund_5d_start_date": date.fromisoformat(fund_window["session_dates"][0]) if fund_window["session_dates"] else None,
        "fund_5d_end_date": date.fromisoformat(fund_window["session_dates"][-1]) if fund_window["session_dates"] else None,
        "current_item": current_item,
        "current_source": current_source,
        "current_trade_date": current_trade_date,
        "current_as_of": current_as_of,
        "snapshot_source": snapshot_source or "unavailable",
        "used_fallback": not use_fresh_snapshot,
    }


async def _get_current_eastmoney_main_fund_items(
    db: AsyncSession,
    trade_date: date | None = None,
    force_refresh: bool = False,
) -> dict[str, dict]:
    snapshot = await prewarm_eastmoney_main_fund_snapshot(
        db,
        trade_date=trade_date,
        force_refresh=force_refresh,
    )
    items = snapshot.get("items")
    return items if isinstance(items, dict) else {}


def _resolve_anomaly_current_fund_context(snapshot: dict | None) -> tuple[dict[str, dict] | None, str]:
    """Only consume the qualified FundFlow projection, including an empty map."""
    if not isinstance(snapshot, dict):
        return {}, "unavailable"

    items = snapshot.get("items")
    source = str(snapshot.get("source") or "")
    if source in {"eastmoney_main_fund", "fund_flow"} and isinstance(items, dict) and items:
        return items, source

    return {}, "unavailable"


def _get_current_main_fund_item(current_fund_map: dict[str, dict] | None, code: str | None) -> dict:
    if not current_fund_map or not code:
        return {}
    return current_fund_map.get(_normalize_fund_code(code)) or {}


def _merge_current_main_fund_detail(detail: dict | None, current_fund: dict | None) -> dict:
    merged = dict(detail or {})
    if not current_fund:
        return merged

    merged.update({
        "main_net_inflow": _safe_float(current_fund.get("main_net_inflow")),
        "main_net_inflow_pct": _safe_float(current_fund.get("main_net_inflow_pct")),
        "super_net_inflow": current_fund.get("super_net_inflow"),
        "super_net_inflow_pct": current_fund.get("super_net_inflow_pct"),
        "big_net_inflow": current_fund.get("big_net_inflow"),
        "big_net_inflow_pct": current_fund.get("big_net_inflow_pct"),
        "mid_net_inflow": current_fund.get("mid_net_inflow"),
        "mid_net_inflow_pct": current_fund.get("mid_net_inflow_pct"),
        "small_net_inflow": current_fund.get("small_net_inflow"),
        "small_net_inflow_pct": current_fund.get("small_net_inflow_pct"),
        "provider_source": current_fund.get("provider_source"),
        "source_version": current_fund.get("source_version"),
        "source_quote_at": current_fund.get("source_quote_at"),
        "received_at": current_fund.get("received_at"),
        "observed_at": current_fund.get("observed_at"),
        "clock_basis": current_fund.get("clock_basis"),
        "clock_status": current_fund.get("clock_status"),
        "source": str(current_fund.get("source") or "eastmoney_main_fund"),
        "is_stale": bool(current_fund.get("is_stale")),
        "as_of": str(current_fund.get("as_of") or merged.get("as_of") or ""),
    })
    return merged


def _get_current_main_fund_amount(current_fund_map: dict[str, dict] | None, code: str | None) -> float:
    current_fund = _get_current_main_fund_item(current_fund_map, code)
    return _safe_float(current_fund.get("main_net_inflow"))


def _get_current_main_fund_pct(current_fund_map: dict[str, dict] | None, code: str | None) -> float | None:
    current_fund = _get_current_main_fund_item(current_fund_map, code)
    value = current_fund.get("main_net_inflow_pct")
    return _safe_float(value) if value is not None else None


def _serialize_event(event: AnomalyEvent) -> dict:
    return {
        "code": event.code,
        "name": event.name,
        "event_type": event.event_type,
        "level": event.level,
        "score": event.score,
        "detail": event.detail,
        "description": event.description,
        "risk_warnings": event.risk_warnings,
        "is_ipo_recent": event.is_ipo_recent,
        "is_one_word_board": event.is_one_word_board,
    }


def _build_risk_flags(detail: dict) -> list[dict]:
    flags: list[dict] = []
    if _is_recent_high_board_risk(detail):
        flags.append({
            "code": "high_board_break_risk",
            "label": "高位断板放量",
            "level": "warning",
        })
    if _kline_chase_reasons(detail):
        flags.append({
            "code": "high_position_chase_risk",
            "label": "高位追涨风险",
            "level": "danger",
        })
    return flags


def _is_watchlist_member_anomaly(item: dict | None) -> bool:
    detail = (item or {}).get("detail") or {}
    return bool(
        detail.get("watchlist_member")
        or detail.get("low_base_watchlist")
        or detail.get("detection_pool_member")
    )


def _is_low_base_watchlist_anomaly(item: dict | None) -> bool:
    """识别真正通过核心成长或动态趋势模型确认的观察池信号。"""
    detail = (item or {}).get("detail") or {}
    signal_type = str(detail.get("signal_type") or "")
    return bool(
        (
            detail.get("low_base_setup_confirmed")
            and signal_type in {"low_base_breakthrough", "low_base_low_absorb"}
        )
        or (
            detail.get("trend_setup_confirmed")
            and signal_type
            in {
                "trend_driver_breakthrough",
                "trend_driver_low_absorb",
                "trend_support_touch",
                "main_wave_green_open_reclaim",
                "main_wave_shape_pullback_reclaim",
                "main_wave_support_reclaim",
                "second_wave_restart",
                "event_relay_confirmation",
                "second_board_relay_confirmation",
                "leader_linkage_confirmation",
                "sector_core_laggard_ignition",
            }
        )
    )


def _is_actionable_detection_pool_anomaly(item: dict | None) -> bool:
    """A2可推检测池：盘后趋势、产业链修复、旧高标修复和盘中水下反转。"""
    detail = (item or {}).get("detail") or {}
    signal_type = str(detail.get("signal_type") or "")
    return bool(
        _is_low_base_watchlist_anomaly(item)
        or (
            detail.get("sector_repair_confirmed")
            and detail.get("detection_pool_member")
            and signal_type == "sector_repair_reversal"
        )
        or (
            detail.get("old_hot_repair_confirmed")
            and detail.get("detection_pool_member")
            and signal_type == "old_hot_oversold_repair"
        )
        or (
            (
                detail.get("underwater_acceleration_confirmed")
                or detail.get("underwater_reversal_confirmed")
            )
            and detail.get("detection_pool_member")
            and signal_type in {"underwater_acceleration", "underwater_reversal"}
        )
        or (
            detail.get("positive_acceleration_confirmed")
            and detail.get("detection_pool_member")
            and signal_type == "positive_acceleration"
        )
        or (
            detail.get("event_relay_confirmed")
            and detail.get("trend_driver_watchlist")
            and signal_type
            in {"event_relay_confirmation", "second_board_relay_confirmation"}
        )
        or (
            detail.get("leader_linkage_confirmed")
            and detail.get("trend_driver_watchlist")
            and signal_type == "leader_linkage_confirmation"
        )
    )


def _build_a1_blockers(anomaly: dict, peer_events: list[dict] | None = None) -> list[str]:
    detail = anomaly.get("detail") or {}
    event_type = str(anomaly.get("event_type") or "")
    blockers: list[str] = []

    amplitude = _safe_float(detail.get("amplitude"))
    turnover = _safe_float(detail.get("turnover"))
    volume_ratio = _safe_float(detail.get("volume_ratio"), 1.0)
    support_strength = _safe_float(detail.get("support_strength_score"))
    is_underwater_reversal = bool(
        event_type == "low_absorb"
        and (
            detail.get("underwater_acceleration_confirmed")
            or detail.get("underwater_reversal_confirmed")
        )
        and str(detail.get("signal_type") or "")
        in {"underwater_acceleration", "underwater_reversal"}
    )

    if _is_recent_high_board_risk(detail):
        blockers.append("高位连板后放量高换手，短线分歧较大")

    if is_underwater_reversal and not (
        settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_CHANGE_PCT
        <= _safe_float(detail.get("change_pct"))
        <= settings.ANOMALY_UNDERWATER_REVERSAL_MAX_CHANGE_PCT
    ):
        blockers.append("已离开水下翻红的前置提醒窗口")

    if event_type == "capital" and not _has_qualified_main_fund_source(detail):
        blockers.append("真实主力资金证据缺失/过期/时钟异常，不能确认资金买点")
    if event_type in {"capital", "breakthrough"} and not _has_positive_driver_sector(detail):
        blockers.append("主驱动板块未同步转强")
    if (
        event_type == "low_absorb"
        and not is_underwater_reversal
        and not _has_positive_driver_sector(detail)
    ):
        blockers.append("低吸信号缺少主驱动板块共振")

    amplitude_limit = (
        settings.ANOMALY_UNDERWATER_REVERSAL_MAX_AMPLITUDE
        if is_underwater_reversal
        else 5.0
    )
    turnover_limit = (
        settings.ANOMALY_UNDERWATER_REVERSAL_MAX_TURNOVER
        if is_underwater_reversal
        else 12.0
        if event_type == "capital"
        else 15.0
    )
    support_threshold = 70.0 if event_type == "capital" else 65.0

    if amplitude > amplitude_limit:
        blockers.append(f"振幅 {amplitude:.1f}% 偏大，追价风险较高")
    if turnover > turnover_limit:
        blockers.append(f"换手 {turnover:.1f}% 过热，筹码分歧偏大")
    if volume_ratio > 2.5:
        blockers.append(f"量比 {volume_ratio:.1f} 偏热，容易放量冲高")
    if 0 < support_strength < support_threshold:
        blockers.append(f"盘口承接仅 {support_strength:.0f}，尚未达到 A1 阈值")

    deduped: list[str] = []
    seen: set[str] = set()
    for item in blockers:
        if item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def _resolve_anomaly_buy_point_fields(item: dict, peer_events: list[dict] | None = None) -> dict:
    entry = _build_anomaly_push_message_data(item, peer_events or [])
    if entry is None:
        return {
            "buy_point_reached": False,
            "buy_point_pushable": False,
            "buy_point_grade": "",
            "buy_point_grade_display": "",
            "buy_point_track": "",
            "buy_point_label": "",
            "buy_point_alert_tier": "",
            "buy_point_blockers": _build_a1_blockers(item, peer_events),
        }

    blockers = list(entry.get("continuity_reasons") or [])
    if _is_observation_only_anomaly(item):
        blockers.append("当前仅为早期异动观察，尚未完成形态池/止跌回收确认")
    pushable = bool(entry.get("alert_tier") and entry.get("buypoint_pushable"))
    if _is_observation_only_anomaly(item):
        pushable = False
    return {
        "buy_point_reached": pushable,
        "buy_point_pushable": pushable,
        "buy_point_grade": str(entry.get("setup_grade") or ""),
        "buy_point_grade_display": str(entry.get("setup_grade_display") or entry.get("setup_grade") or ""),
        "buy_point_track": str(entry.get("setup_track") or ""),
        "buy_point_label": str(entry.get("signal_label") or ""),
        "buy_point_alert_tier": str(entry.get("alert_tier") or ""),
        "buy_point_blockers": [] if pushable else blockers,
    }


def _enrich_anomaly_display(item: dict, peer_events: list[dict] | None = None) -> dict:
    peer_events = peer_events or []
    setup_grade = _resolve_setup_grade(item, peer_events)
    risk_flags = _build_risk_flags(item.get("detail") or {})
    a1_blockers = []
    if setup_grade and setup_grade != "A1 可直接执行":
        a1_blockers = _build_a1_blockers(item, peer_events)

    enriched = dict(item)
    enriched["setup_grade"] = setup_grade
    enriched["setup_track"] = _resolve_setup_track(item, setup_grade)
    enriched["setup_grade_display"] = _display_setup_grade(item, setup_grade)
    enriched["risk_flags"] = risk_flags
    enriched["a1_blockers"] = a1_blockers
    buy_point_fields = _resolve_anomaly_buy_point_fields(item, peer_events)
    enriched.update(buy_point_fields)
    grade_rank = _grade_rank(setup_grade)
    a2_allowed = (
        not settings.ANOMALY_PUSH_WATCHLIST_A2_ONLY
        or grade_rank >= _grade_rank("A1 可直接执行")
        or _is_actionable_detection_pool_anomaly(item)
    )
    limit_up_allowed = (
        str(item.get("event_type") or "") != "limit_up"
        or settings.ANOMALY_PUSH_ALLOW_LIMIT_UP
    )
    enriched["feishu_pushable"] = (
        bool(buy_point_fields.get("buy_point_pushable"))
        and grade_rank >= _grade_rank("A2 盘口确认后执行")
        and a2_allowed
        and limit_up_allowed
    )
    return enriched


def _summarize_anomalies(anomalies: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    for anomaly in anomalies:
        event_type = _event_filter_key(anomaly)
        by_type[event_type] = by_type.get(event_type, 0) + 1
    return {
        "total": len(anomalies),
        "limit_up_count": by_type.get("limit_up", 0),
        "limit_down_count": by_type.get("limit_down", 0),
        "capital_count": by_type.get("capital", 0),
        "low_absorb_count": by_type.get("low_absorb", 0),
        "breakthrough_count": by_type.get("breakthrough", 0),
        "rapid_rise_count": by_type.get("rapid_rise", 0),
        "pump_dump_count": by_type.get("pump_dump", 0),
        "by_type": by_type,
    }


def _summarize_stock_rows(rows: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    buy_point_count = 0
    buy_point_pushable_count = 0
    anomaly_buy_point_count = 0
    b1_buy_point_count = 0
    for row in rows:
        for event_type in row.get("event_types") or []:
            key = str(event_type or "").strip()
            if not key:
                continue
            by_type[key] = by_type.get(key, 0) + 1
        if row.get("buy_point_reached"):
            buy_point_count += 1
        if row.get("buy_point_pushable"):
            buy_point_pushable_count += 1
        sources = set(row.get("buy_point_sources") or [])
        if "anomaly" in sources:
            anomaly_buy_point_count += 1
        if "b1" in sources:
            b1_buy_point_count += 1
    return {
        "total": len(rows),
        "limit_up_count": by_type.get("limit_up", 0),
        "limit_down_count": by_type.get("limit_down", 0),
        "capital_count": by_type.get("capital", 0),
        "low_absorb_count": by_type.get("low_absorb", 0),
        "breakthrough_count": by_type.get("breakthrough", 0),
        "rapid_rise_count": by_type.get("rapid_rise", 0),
        "pump_dump_count": by_type.get("pump_dump", 0),
        "buy_point_count": buy_point_count,
        "buy_point_pushable_count": buy_point_pushable_count,
        "anomaly_buy_point_count": anomaly_buy_point_count,
        "b1_buy_point_count": b1_buy_point_count,
        "by_type": by_type,
    }


def _event_filter_key(event: dict | str, detail: dict | None = None) -> str:
    if isinstance(event, dict):
        event_type = str(event.get("event_type") or "")
        detail = event.get("detail") or {}
    else:
        event_type = str(event or "")
        detail = detail or {}
    signal_type = str((detail or {}).get("signal_type") or "")
    if event_type == "breakthrough" and signal_type in {
        "momentum_burst",
        "positive_acceleration",
    }:
        return "rapid_rise"
    return event_type


def _event_type_label(event_type: str) -> str:
    return {
        "limit_up": "涨停",
        "limit_down": "跌停",
        "capital": "资金",
        "low_absorb": "低吸",
        "breakthrough": "突破",
        "rapid_rise": "快速拉升",
        "pump_dump": "冲高回落",
    }.get(event_type or "", event_type or "异动")


def _format_orderbook_lot(value: float) -> str:
    numeric = _safe_float(value)
    if not numeric:
        return "-"
    if abs(numeric) >= 10000:
        return f"{numeric / 10000:.1f}万手"
    return f"{numeric:.0f}手"


def _format_sector_line(sector: dict) -> str:
    flow = _safe_float(sector.get("fund_flow"))
    flow_prefix = "+" if flow >= 0 else ""
    return (
        f"{sector.get('sector_name') or '未知板块'} | "
        f"{_safe_float(sector.get('change_pct')):+.2f}% | "
        f"资金{flow_prefix}{flow:.2f}亿 | "
        f"强度{round(_safe_float(sector.get('strength_score')))} | "
        f"涨停{_safe_int(sector.get('limit_up_count'))}家"
    )


def _format_reference_hint(factors: list[dict]) -> str:
    """无因果主驱动时，把最强参考板块压成一行，供单元格直接显示。

    参考板块此前只经 `reference_driver_lines` 进入前端 **hover tooltip**，
    表格里那格只写「暂无明确主驱动」—— 看不出该股所属板块当日是涨是跌、
    资金是进是出。银行/水电/煤炭这类低波动大盘股常年落在这条分支上
    （产业板块当日没同步转强），单元格就长期只有一句无信息量的占位文案。

    这里只补**提示文本**：
    * 不改变 `driver_primary` 的语义（仍是「没有因果主驱动」）；
    * 不参与任何买点/推送门槛（那四个门槛各自要求 `fund_flow > 0`，见
      `tenbagger._has_positive_driver_sector` 等）；
    * 参考板块本来就已排除泛标签（`is_excluded=1`）与申万数字代码，
      所以这里显示的一定是产业/题材板块，不会是「沪股通」这类。
    """
    if not factors:
        return ""
    # 优先该股自己的行业 —— 它才是「为什么没有主驱动」的直接答案
    # （该股所属产业当日没同步转强）。概念参考退居其次。
    # 不依赖 `_select_reference_factors` 的排序，显式表达展示口径。
    top = next(
        (
            item
            for item in factors
            if str(item.get("sector_type") or "") == "industry"
        ),
        factors[0],
    )
    name = str(top.get("sector_name") or "").strip()
    if not name:
        return ""
    return (
        f"无因果主驱动 · 参考：{name} "
        f"{_safe_float(top.get('change_pct')):+.2f}%／"
        f"资金{_safe_float(top.get('fund_flow')):+.2f}亿"
        + (f"（共{len(factors)}个，悬停查看）" if len(factors) > 1 else "")
    )


def _format_sector_peer_line(item: dict) -> str:
    leaders = item.get("leaders") or []
    if not leaders:
        return ""
    parts = []
    for leader in leaders:
        days = _safe_int(leader.get("consecutive_days"))
        suffix = f"{days}连板" if days > 0 else "首板"
        parts.append(f"{leader.get('name') or '--'}({suffix})")
    return f"{item.get('sector_name') or '未知板块'}: {'、'.join(parts)}"


def _is_meaningful_sector_factor(sector: dict) -> bool:
    return any(
        [
            abs(_safe_float(sector.get("change_pct"))) >= 0.01,
            abs(_safe_float(sector.get("fund_flow"))) >= 0.01,
            _safe_float(sector.get("strength_score")) >= 1,
            _safe_int(sector.get("limit_up_count")) > 0,
            _safe_int(sector.get("peer_count")) > 0,
        ]
    )


def _meaningful_sector_factors(items: list[dict]) -> list[dict]:
    return [item for item in (items or []) if _is_meaningful_sector_factor(item)]


def _anomaly_display_score(anomaly: dict) -> float:
    detail = anomaly.get("detail") or {}
    event_type = str(anomaly.get("event_type") or "")
    score = _safe_float(anomaly.get("score"))
    score += _grade_rank(anomaly.get("setup_grade")) * 20
    risk_flags = anomaly.get("risk_flags") or []
    if any((flag or {}).get("code") == "high_board_break_risk" for flag in risk_flags):
        score -= 20

    support_strength = _safe_float(detail.get("support_strength_score"))
    seal_quality = _safe_float(detail.get("seal_quality_score"))
    withdrawal_ratio = _safe_float(detail.get("withdrawal_ratio"))

    if event_type == "capital":
        score += support_strength * 0.25
        score += seal_quality * 0.1
        score -= withdrawal_ratio * 20
    elif event_type == "pump_dump":
        score += withdrawal_ratio * 30
        score += max(0, 50 - support_strength) * 0.2
    elif event_type == "limit_up":
        score += seal_quality * 0.2
        score += support_strength * 0.1
    elif event_type == "breakthrough":
        score += support_strength * 0.15
    elif event_type == "low_absorb":
        score += support_strength * 0.22
        score += max(0, 3.0 - abs(_safe_float(detail.get("distance_to_ma5_pct")))) * 2.0
        score += max(0, _safe_float(detail.get("intraday_rebound_pct"))) * 1.5

    return score


def _anomaly_as_timestamp(value: str | None) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    try:
        return int(datetime.fromisoformat(raw).timestamp())
    except Exception:
        return 0


def _unique_strings(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _best_events_by_type(events: list[dict]) -> list[dict]:
    by_type: dict[str, dict] = {}
    for event in events:
        event_type = _event_filter_key(event)
        current = by_type.get(event_type)
        if current is None or _anomaly_display_score(event) > _anomaly_display_score(current):
            by_type[event_type] = event
    return sorted(
        by_type.values(),
        key=lambda item: (_anomaly_display_score(item), _anomaly_as_timestamp((item.get("detail") or {}).get("as_of"))),
        reverse=True,
    )


def _build_aggregated_anomaly_row(items: list[dict]) -> dict:
    sorted_events = sorted(
        items,
        key=lambda item: (_anomaly_display_score(item), _anomaly_as_timestamp((item.get("detail") or {}).get("as_of"))),
        reverse=True,
    )
    primary = sorted_events[0] if sorted_events else {}
    quote_event = next((item for item in sorted_events if _safe_float((item.get("detail") or {}).get("price")) > 0), primary)
    capital_event = next((item for item in sorted_events if abs(_safe_float((item.get("detail") or {}).get("main_net_inflow"))) > 0), primary)

    merged_detail = {}
    for item in [quote_event, primary, capital_event]:
        if item and isinstance(item.get("detail"), dict):
            merged_detail.update(item.get("detail") or {})

    best_events = _best_events_by_type(sorted_events)
    event_badges = [
        {"value": _event_filter_key(event), "label": _event_type_label(_event_filter_key(event))}
        for event in best_events[:3]
        if _event_filter_key(event)
    ]
    descriptions = _unique_strings([str(event.get("description") or "") for event in sorted_events])

    latest_as_of = ""
    latest_ts = 0
    for event in sorted_events:
        event_as_of = str((event.get("detail") or {}).get("as_of") or "")
        ts = _anomaly_as_timestamp(event_as_of)
        if ts >= latest_ts:
            latest_ts = ts
            latest_as_of = event_as_of

    sector_factors = _meaningful_sector_factors(merged_detail.get("sector_factors") or [])
    sector_components = [
        item for item in (merged_detail.get("sector_components") or [])
        if _is_meaningful_sector_factor(item)
    ]
    reference_factors = _meaningful_sector_factors(merged_detail.get("sector_reference_factors") or [])
    merged_detail["sector_factors"] = sector_factors
    merged_detail["sector_components"] = sector_components
    merged_detail["sector_reference_factors"] = reference_factors
    driver_primary = sector_factors[0] if sector_factors else {}
    driver_secondary = (
        f"{_safe_float(driver_primary.get('change_pct')):+.2f}% · "
        f"资金{('+' if _safe_float(driver_primary.get('fund_flow')) >= 0 else '')}{_safe_float(driver_primary.get('fund_flow')):.2f}亿 · "
        f"强度{round(_safe_float(driver_primary.get('strength_score')))}"
        if driver_primary
        # 无因果主驱动时，把最强参考板块提到副标题 —— 否则单元格只有一句
        # 「暂无明确主驱动」，看不出该股所属板块当日到底什么状态（参考板块
        # 此前只出现在 hover tooltip 的 `reference_driver_lines` 里）。
        # 纯展示：`driver_primary` 语义不变，也不参与任何买点门槛。
        else _format_reference_hint(reference_factors)
        or "暂无明确主驱动，先看个股盘口与量价确认"
    )
    driver_peer_summary = _format_sector_peer_line(sector_components[0]) if sector_components else ""

    risk_flags: list[dict] = []
    seen_flags: set[str] = set()
    for event in sorted_events:
        for flag in event.get("risk_flags") or []:
            key = str((flag or {}).get("code") or (flag or {}).get("label") or "").strip()
            if not key or key in seen_flags:
                continue
            seen_flags.add(key)
            risk_flags.append(flag)

    primary_reason = descriptions[0] if descriptions else f"{_event_type_label(_event_filter_key(primary))}异动"
    badge_labels = [item["label"] for item in event_badges]
    secondary_reason = (
        f"共振：{' / '.join(badge_labels[1:])}"
        if len(badge_labels) > 1
        else "当前以主机会信号为准"
    )
    events = []
    for event in sorted_events:
        detail = event.get("detail") or {}
        events.append({
            "key": _anomaly_signal_identity(event),
            "event_type": event.get("event_type") or "",
            "event_filter_type": _event_filter_key(event),
            "event_label": _event_type_label(_event_filter_key(event)),
            "description": event.get("description") or "",
            "score": _safe_float(event.get("score")),
            "display_score": _anomaly_display_score(event),
            "setup_grade": event.get("setup_grade") or "",
            "setup_grade_display": event.get("setup_grade_display") or event.get("setup_grade") or "",
            "setup_track": event.get("setup_track") or "",
            "feishu_pushable": bool(event.get("feishu_pushable")),
            "buy_point_reached": bool(event.get("buy_point_reached")),
            "buy_point_pushable": bool(event.get("buy_point_pushable")),
            "buy_point_grade": event.get("buy_point_grade") or "",
            "buy_point_grade_display": event.get("buy_point_grade_display") or event.get("buy_point_grade") or "",
            "buy_point_label": event.get("buy_point_label") or "",
            "buy_point_blockers": event.get("buy_point_blockers") or [],
            "risk_flags": event.get("risk_flags") or [],
            "a1_blockers": event.get("a1_blockers") or [],
            "latest_as_of": detail.get("as_of") or latest_as_of,
            "price": _safe_float(detail.get("price")),
            "change_pct": _safe_float(detail.get("change_pct")),
            # 同股展开明细也是展示合同：缺失不造零，来源和资金时钟随原事件透传。
            "main_net_inflow": detail.get("main_net_inflow"),
            "main_net_inflow_pct": detail.get("main_net_inflow_pct"),
            "fund_display": detail.get("fund_evidence"),
            **{key: detail.get(key) for key in (
                "source", "provider_source", "source_version", "source_quote_at",
                "received_at", "observed_at", "clock_status", "is_stale",
            )},
            "volume_ratio": _safe_float(detail.get("volume_ratio")),
            "support_strength_score": detail.get("support_strength_score"),
            "seal_quality_score": detail.get("seal_quality_score"),
            "withdrawal_ratio": detail.get("withdrawal_ratio"),
            "rolling_60s_change_pct": _safe_float(
                detail.get("rolling_60s_change_pct")
            ),
            "rolling_60s_amount_pace_ratio": _safe_float(
                detail.get("rolling_60s_amount_pace_ratio")
            ),
            "rolling_60s_tier": str(detail.get("rolling_60s_tier") or ""),
            "rolling_60s_path_confirmed": bool(
                detail.get("rolling_60s_path_confirmed")
            ),
        })

    buy_point_event = next((event for event in sorted_events if event.get("buy_point_pushable")), None)
    buy_point_blockers = _unique_strings([
        str(reason)
        for event in sorted_events
        for reason in (event.get("buy_point_blockers") or event.get("a1_blockers") or [])
    ])[:5]

    row = {
        "key": f"{primary.get('code') or ''}-aggregate",
        "code": primary.get("code") or "",
        "name": primary.get("name") or "",
        "event_type": _event_filter_key(primary),
        "event_types": [_event_filter_key(event) for event in best_events if _event_filter_key(event)],
        "event_badges": event_badges,
        "event_count": len(sorted_events),
        "level": primary.get("level") or "",
        "score": _safe_float(primary.get("score")),
        "display_score": _anomaly_display_score(primary) + min(max(len(sorted_events) - 1, 0), 3) * 3,
        "detail": merged_detail,
        # Pick one whole primary-event frame, never stitch money to peer clocks.
        "fund_evidence": (primary.get("detail") or {}).get("fund_evidence"),
        "description": primary.get("description") or "",
        "setup_grade": primary.get("setup_grade") or "",
        "setup_grade_display": primary.get("setup_grade_display") or primary.get("setup_grade") or "",
        "setup_track": primary.get("setup_track") or "",
        "risk_flags": risk_flags,
        "a1_blockers": primary.get("a1_blockers") or [],
        "feishu_pushable": any(bool(event.get("feishu_pushable")) for event in sorted_events),
        "buy_point_reached": bool(buy_point_event),
        "buy_point_pushable": bool(buy_point_event),
        "buy_point_type": "异动确认" if buy_point_event else "",
        "buy_point_label": (buy_point_event or {}).get("buy_point_label") or "",
        "buy_point_grade_display": (buy_point_event or {}).get("buy_point_grade_display") or "",
        "buy_point_sources": ["anomaly"] if buy_point_event else [],
        "buy_point_reasons": [
            f"{(buy_point_event or {}).get('buy_point_grade_display') or (buy_point_event or {}).get('setup_grade_display') or '买点'}｜{(buy_point_event or {}).get('buy_point_label') or _event_type_label(_event_filter_key(buy_point_event or {}))}"
        ] if buy_point_event else [],
        "buy_point_blockers": buy_point_blockers,
        "is_ipo_recent": any(bool(event.get("is_ipo_recent")) for event in sorted_events),
        "is_one_word_board": any(bool(event.get("is_one_word_board")) for event in sorted_events),
        "latest_as_of": latest_as_of,
        "primary_reason": primary_reason,
        "secondary_reason": secondary_reason,
        "driver_primary": driver_primary.get("sector_name") or "暂无明确主驱动",
        "driver_secondary": driver_secondary,
        "driver_peer_summary": driver_peer_summary,
        "driver_detail_lines": [_format_sector_line(item) for item in sector_factors],
        "reference_driver_lines": [_format_sector_line(item) for item in reference_factors],
        "events": events,
    }
    return _apply_stock_row_buy_point_status(row)


def _compare_aggregated_rows(row: dict, sort_by: str) -> tuple:
    detail = row.get("detail") or {}
    if sort_by == "buy_point":
        return (
            1 if row.get("buy_point_pushable") else 0,
            1 if row.get("buy_point_reached") else 0,
            1 if row.get("b1_pushable") else 0,
            _grade_rank(row.get("setup_grade")),
            row.get("priority_score") or row.get("display_score") or 0,
            _anomaly_as_timestamp(row.get("latest_as_of")),
        )
    if sort_by == "grade":
        return (
            _grade_rank(row.get("setup_grade")),
            row.get("display_score") or 0,
            _anomaly_as_timestamp(row.get("latest_as_of")),
        )
    if sort_by == "change_pct":
        return (
            _safe_float(detail.get("change_pct")),
            row.get("display_score") or 0,
            _anomaly_as_timestamp(row.get("latest_as_of")),
        )
    if sort_by == "net_inflow":
        return (
            _safe_float(detail.get("main_net_inflow")),
            row.get("display_score") or 0,
            _anomaly_as_timestamp(row.get("latest_as_of")),
        )
    if sort_by == "latest":
        return (
            _anomaly_as_timestamp(row.get("latest_as_of")),
            row.get("display_score") or 0,
            _grade_rank(row.get("setup_grade")),
        )
    return (
        1 if row.get("buy_point_pushable") else 0,
        1 if row.get("buy_point_reached") else 0,
        row.get("priority_score") or row.get("display_score") or 0,
        row.get("b1_priority_bonus") or 0,
        row.get("display_score") or 0,
        _grade_rank(row.get("setup_grade")),
        _anomaly_as_timestamp(row.get("latest_as_of")),
    )


def _build_stock_rows(
    anomalies: list[dict],
    *,
    event_type: str = "",
    setup_track: str = "",
    sort_by: str = "priority",
) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for item in anomalies:
        code = str(item.get("code") or "")
        if not code:
            continue
        grouped.setdefault(code, []).append(item)

    rows = [_build_aggregated_anomaly_row(items) for items in grouped.values()]

    if event_type:
        rows = [row for row in rows if event_type in (row.get("event_types") or [])]
    if setup_track:
        rows = [row for row in rows if str(row.get("setup_track") or "") == setup_track]

    return sorted(rows, key=lambda row: _compare_aggregated_rows(row, sort_by), reverse=True)


def _normalize_sector_type(value: str | None) -> str:
    return str(value or "").strip().lower()


def _resolve_b1_industry_names(items: list[dict]) -> tuple[str, str]:
    if not items:
        return "", ""
    industry = ""
    sub_industry = ""
    for item in items:
        sector_type = _normalize_sector_type(item.get("sector_type"))
        sector_name = str(item.get("sector_name") or "").strip()
        if not sector_name:
            continue
        if not industry and sector_type in {"industry", "sw_l1"}:
            industry = sector_name
            continue
        if not sub_industry and sector_type in {"sw_l2", "sw_l3"}:
            sub_industry = sector_name
            continue
    if not industry:
        industry = str(items[0].get("sector_name") or "").strip()
    if not sub_industry and len(items) > 1:
        for item in items[1:]:
            sector_name = str(item.get("sector_name") or "").strip()
            if sector_name and sector_name != industry:
                sub_industry = sector_name
                break
    return industry, sub_industry


def _build_live_b1_bar(spot: StockSpot | None, trade_day: date, latest_bar: dict | None = None) -> dict | None:
    if not spot or _safe_float(spot.price) <= 0 or _safe_float(spot.open) <= 0 or _safe_float(spot.high) <= 0 or _safe_float(spot.low) <= 0:
        return None
    latest_date = latest_bar.get("trade_date") if latest_bar else None
    if latest_date and latest_date >= trade_day:
        return None
    return {
        "trade_date": trade_day,
        "open": _safe_float(spot.open),
        "high": _safe_float(spot.high),
        "low": _safe_float(spot.low),
        "close": _safe_float(spot.price),
        "volume": int(_safe_float(spot.volume) * 100),
        "turnover": _safe_float(spot.turnover),
        "change_pct": _safe_float(spot.change_pct),
    }


def _calc_change_pct_over_days(bars: list[dict], days: int = 5) -> float | None:
    if len(bars) < 2:
        return None
    current_close = _safe_float(bars[-1].get("close"))
    target_index = max(0, len(bars) - days - 1)
    base_close = _safe_float(bars[target_index].get("close"))
    if current_close <= 0 or base_close <= 0:
        return None
    return round((current_close / base_close - 1.0) * 100.0, 2)


def _resolve_b1_signal_status(signal_key: str | None, *, uses_live_bar: bool) -> str:
    if not signal_key:
        return ""
    return B1_STATUS_INTRADAY_PREVIEW if uses_live_bar else B1_STATUS_CLOSE_CONFIRMED


def _resolve_b1_push_eligibility(row: dict) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    signal_key = str(row.get("b1_signal_key") or "").strip()
    signal_status = str(row.get("b1_signal_status") or "").strip()
    if not signal_key or signal_status not in {B1_STATUS_INTRADAY_PREVIEW, B1_STATUS_CLOSE_CONFIRMED}:
        blockers.append("当前未形成有效B1状态")
        return False, blockers

    hold_score = _safe_int(row.get("b1_hold_score"))
    if hold_score < 3:
        blockers.append(f"持股分数仅 {hold_score} 分")
    if bool(row.get("b1_break_trend")):
        blockers.append("已跌破趋势白线")

    success_rate_3d = row.get("b1_success_rate_3d")
    success_samples = _safe_int(row.get("b1_success_samples"))
    if success_samples >= 3:
        if success_rate_3d is None or float(success_rate_3d) < 0.55:
            blockers.append("3日修复率不足 55%")
    elif hold_score < 4:
        blockers.append("历史样本不足，且当前持股分数未达 4 分")

    return len(blockers) == 0, blockers


def _apply_stock_row_buy_point_status(row: dict) -> dict:
    row_copy = dict(row)
    events = list(row_copy.get("events") or [])
    anomaly_event = next((event for event in events if event.get("buy_point_pushable")), None)
    anomaly_sources = ["anomaly"] if anomaly_event or row_copy.get("buy_point_pushable") else []
    b1_pushable = bool(row_copy.get("b1_pushable"))

    sources = list(dict.fromkeys(anomaly_sources + (["b1"] if b1_pushable else [])))
    reasons: list[str] = []
    if anomaly_event:
        grade = str(anomaly_event.get("buy_point_grade_display") or anomaly_event.get("setup_grade_display") or "").strip()
        label = str(anomaly_event.get("buy_point_label") or anomaly_event.get("event_label") or "").strip()
        reasons.append("｜".join(item for item in [grade, label] if item))
    elif row_copy.get("buy_point_reasons"):
        reasons.extend(str(item) for item in (row_copy.get("buy_point_reasons") or []) if item)

    if b1_pushable:
        status_label = str(row_copy.get("b1_signal_status_label") or "").strip()
        signal = str(row_copy.get("b1_signal") or "").strip()
        hold_score = _safe_int(row_copy.get("b1_hold_score"))
        reasons.append(f"B1{status_label}｜{signal}｜持股分{hold_score}")

    blockers = _unique_strings(
        [str(item) for item in (row_copy.get("buy_point_blockers") or [])]
        + [str(item) for item in (row_copy.get("b1_push_blockers") or [])]
    )[:6]

    if "anomaly" in sources and "b1" in sources:
        buy_point_type = "异动+B1共振"
    elif "b1" in sources:
        buy_point_type = "B1状态机"
    elif "anomaly" in sources:
        buy_point_type = "异动确认"
    else:
        buy_point_type = ""

    row_copy.update(
        {
            "buy_point_reached": bool(sources),
            "buy_point_pushable": bool(sources),
            "buy_point_type": buy_point_type,
            "buy_point_sources": sources,
            "buy_point_reasons": _unique_strings(reasons)[:5],
            "buy_point_blockers": [] if sources else blockers,
        }
    )
    if not row_copy.get("buy_point_label"):
        row_copy["buy_point_label"] = reasons[0] if reasons else ""
    return row_copy


def _serialize_b1_state_row(row: dict) -> dict | None:
    signal_status = str(row.get("b1_signal_status") or "").strip()
    if not signal_status:
        return None

    pushable, blockers = _resolve_b1_push_eligibility(row)
    detail = row.get("detail") or {}
    return {
        "code": str(row.get("code") or "").strip(),
        "name": str(row.get("name") or "").strip(),
        "signal_key": str(row.get("b1_signal_key") or "").strip(),
        "signal_label": str(row.get("b1_signal") or "").strip(),
        "signal_status": signal_status,
        "signal_status_label": B1_STATUS_LABELS.get(signal_status, signal_status),
        "pushable": pushable,
        "push_blockers": blockers,
        "hold_score": _safe_int(row.get("b1_hold_score")),
        "break_trend": bool(row.get("b1_break_trend")),
        "trend_white_price": row.get("b1_trend_white_price"),
        "success_rate_3d": row.get("b1_success_rate_3d"),
        "success_rate_5d": row.get("b1_success_rate_5d"),
        "success_samples": _safe_int(row.get("b1_success_samples")),
        "history_high_confidence": bool(row.get("b1_history_high_confidence")),
        "j": row.get("b1_j"),
        "rsi": row.get("b1_rsi"),
        "short_score": row.get("b1_short_score"),
        "long_score": row.get("b1_long_score"),
        "setup_grade": str(row.get("setup_grade_display") or row.get("setup_grade") or "").strip(),
        "setup_track": str(row.get("setup_track") or "").strip(),
        "priority_score": _safe_float(row.get("priority_score")),
        "display_score": _safe_float(row.get("display_score")),
        "current_price": row.get("current_price"),
        "day_change_pct": row.get("day_change_pct"),
        "turnover_rate": row.get("turnover_rate"),
        "main_net_inflow": row.get("main_net_inflow"),
        "industry": str(row.get("industry") or "").strip(),
        "sub_industry": str(row.get("sub_industry") or "").strip(),
        "latest_as_of": str(row.get("latest_as_of") or "").strip(),
        "primary_reason": str(row.get("primary_reason") or "").strip(),
        "event_badges": row.get("event_badges") or [],
        "watchlist_member": bool(detail.get("watchlist_member")),
        "low_base_setup_confirmed": bool(detail.get("low_base_setup_confirmed")),
    }


def _summarize_b1_states(states: list[dict]) -> dict:
    summary = {
        "total": len(states),
        "intraday_preview": 0,
        "close_confirmed": 0,
        "pushable": 0,
    }
    for item in states:
        status = str(item.get("signal_status") or "")
        if status in summary:
            summary[status] += 1
        if item.get("pushable"):
            summary["pushable"] += 1
    return summary


async def _build_b1_state_rows_for_snapshot(anomalies: list[dict], db: AsyncSession) -> list[dict]:
    stock_rows = _build_stock_rows(
        anomalies,
        event_type="",
        setup_track="",
        sort_by="priority",
    )
    if not stock_rows:
        return []

    enriched_rows = await _enrich_stock_rows_with_b1(stock_rows, db)
    states = [
        item
        for item in (_serialize_b1_state_row(row) for row in enriched_rows)
        if item and item.get("code")
    ]
    return sorted(
        states,
        key=lambda item: (
            1 if item.get("pushable") else 0,
            1 if item.get("signal_status") == B1_STATUS_CLOSE_CONFIRMED else 0,
            _safe_float(item.get("priority_score")),
            _anomaly_as_timestamp(item.get("latest_as_of")),
        ),
        reverse=True,
    )


def _resolve_b1_transition(current: dict | None, previous: dict | None) -> str:
    if current is None:
        return "invalidated"
    if previous is None or not previous.get("pushable"):
        return "appeared"
    previous_status = str(previous.get("signal_status") or "")
    current_status = str(current.get("signal_status") or "")
    previous_signal_key = str(previous.get("signal_key") or "")
    current_signal_key = str(current.get("signal_key") or "")
    if (
        previous_status == B1_STATUS_INTRADAY_PREVIEW
        and current_status == B1_STATUS_CLOSE_CONFIRMED
        and previous_signal_key == current_signal_key
    ):
        return "confirmed"
    if previous_signal_key != current_signal_key:
        return "signal_changed"
    return "appeared"


def _select_b1_state_changes(current_states: list[dict], previous_states: list[dict] | None = None) -> list[dict]:
    previous_states = previous_states or []
    current_by_code = {
        str(item.get("code") or "").strip(): item
        for item in current_states
        if str(item.get("code") or "").strip()
    }
    previous_by_code = {
        str(item.get("code") or "").strip(): item
        for item in previous_states
        if str(item.get("code") or "").strip()
    }

    changes: list[dict] = []
    for code, current in current_by_code.items():
        previous = previous_by_code.get(code)
        current_pushable = bool(current.get("pushable"))
        previous_pushable = bool(previous.get("pushable")) if previous else False
        signal_changed = (
            str(current.get("signal_key") or "") != str((previous or {}).get("signal_key") or "")
        )
        status_changed = (
            str(current.get("signal_status") or "") != str((previous or {}).get("signal_status") or "")
        )
        if current_pushable and (not previous_pushable or signal_changed or status_changed):
            changes.append({
                "transition": _resolve_b1_transition(current, previous),
                "signal_status": str(current.get("signal_status") or ""),
                "current": current,
                "previous": previous,
            })

    for code, previous in previous_by_code.items():
        if not previous.get("pushable"):
            continue
        current = current_by_code.get(code)
        if current is None or not current.get("pushable"):
            changes.append({
                "transition": "invalidated",
                "signal_status": B1_STATUS_INVALIDATED,
                "current": current,
                "previous": previous,
            })

    transition_order = {
        "confirmed": 0,
        "appeared": 1,
        "signal_changed": 2,
        "invalidated": 3,
    }
    return sorted(
        changes,
        key=lambda item: (
            -transition_order.get(str(item.get("transition") or ""), 99),
            _safe_float(((item.get("current") or item.get("previous") or {}).get("priority_score"))),
            _anomaly_as_timestamp((item.get("current") or item.get("previous") or {}).get("latest_as_of")),
        ),
        reverse=True,
    )


def _build_b1_state_push_message(change: dict):
    transition = str(change.get("transition") or "")
    signal_status = str(change.get("signal_status") or "")
    if signal_status == B1_STATUS_INVALIDATED:
        return None
    current = change.get("current") or {}
    previous = change.get("previous") or {}
    base = current or previous
    if not base:
        return None

    blockers = list(base.get("push_blockers") or [])

    message = b1_state_alert(
        code=str(base.get("code") or ""),
        name=str(base.get("name") or ""),
        signal_label=str(base.get("signal_label") or ""),
        signal_status=signal_status,
        transition=transition,
        current_price=_safe_float((current or base).get("current_price")),
        day_change_pct=_safe_float((current or base).get("day_change_pct")),
        turnover_rate=_safe_float((current or base).get("turnover_rate")),
        main_net_inflow=_safe_float((current or base).get("main_net_inflow")),
        j_value=(current or base).get("j"),
        rsi_value=(current or base).get("rsi"),
        short_score=(current or base).get("short_score"),
        long_score=(current or base).get("long_score"),
        hold_score=_safe_int((current or base).get("hold_score")),
        success_rate_3d=(current or base).get("success_rate_3d"),
        success_rate_5d=(current or base).get("success_rate_5d"),
        success_samples=_safe_int((current or base).get("success_samples")),
        trend_white_price=(current or base).get("trend_white_price"),
        is_break_trend=bool((current or base).get("break_trend")),
        setup_grade=str((current or base).get("setup_grade") or ""),
        setup_track=str((current or base).get("setup_track") or ""),
        primary_reason=str((current or previous or base).get("primary_reason") or ""),
        blockers=blockers,
        previous_signal_label=str(previous.get("signal_label") or ""),
    )
    message.extra = dict(message.extra or {})
    message.extra.update({
        "event_type": "b1_state",
        "signal_variant": str(base.get("signal_key") or "b1_state"),
        "signal_identity": "|".join([
            str(base.get("code") or ""),
            "b1_state",
            str(base.get("signal_key") or ""),
        ]),
        "signal_price": _safe_float((current or base).get("current_price")),
        "signal_score": _safe_float((current or base).get("priority_score")),
        "watchlist_member": bool(base.get("watchlist_member")),
        "low_base_watchlist": bool(base.get("low_base_setup_confirmed")),
        "low_base_setup_confirmed": bool(base.get("low_base_setup_confirmed")),
    })
    return message


def _anomaly_orderbook_display(detail: dict, *, as_of_at: datetime) -> dict:
    """Quote-only provenance; detail.source_quote_at may belong to funds."""
    source_at = evidence_clock(detail.get("quote_source_at"))
    received_at = evidence_clock(detail.get("quote_received_at"))
    status = "unknown"
    if source_at and received_at:
        if source_at > as_of_at or received_at > as_of_at:
            status = "future"
        elif source_at > received_at or source_at.date() != received_at.date():
            status = "invalid"
        elif trade_calendar.get_trade_session(as_of_at) not in {"morning", "afternoon"}:
            status = "historical"
        elif (as_of_at - source_at).total_seconds() > settings.ANOMALY_QUOTE_MAX_AGE_SEC:
            status = "stale"
        elif _safe_float(detail.get("bid_depth_5")) <= 0 or _safe_float(detail.get("ask_depth_5")) <= 0:
            status = "single_sided"
        else:
            status = "ok"
    return {
        "purpose": "display_only", "clock_status": status,
        "source_quote_at": source_at.isoformat() if source_at else None,
        "received_at": received_at.isoformat() if received_at else None,
    }


async def _attach_anomaly_display_context(
    rows: list[dict], db: AsyncSession, *,
    trade_date: date | None, as_of_at: datetime,
) -> list[dict]:
    """Last response step, AFTER all entry gates/filtering/sorting.

    Preserve execution detail and original events. Daily latest funds can fill
    the aggregate's explicitly labelled latest display, NEVER an earlier event.
    """
    from app.data.main_fund import (
        load_latest_main_fund_display_map, main_fund_display_evidence,
    )
    if not rows or trade_date is None:
        return rows
    read_failed = False
    try:
        latest = await load_latest_main_fund_display_map(
            db, trade_date=trade_date, as_of_at=as_of_at,
            codes=[row.get("code") for row in rows],
        )
    except SQLAlchemyError as exc:
        await db.rollback()
        logger.warning(f"异动资金展示读取失败，保留原事件证据: {type(exc).__name__}")
        latest, read_failed = {}, True

    def event_display(raw, code):
        raw = raw if isinstance(raw, dict) else {}
        if str(raw.get("code") or "") != str(code or ""):
            raw = {}
        return main_fund_display_evidence(
            raw, trade_date=trade_date, as_of_at=as_of_at,
            basis="event_snapshot", event_at=raw.get("event_at"),
        )

    result = []
    for row in rows:
        original = event_display(row.get("fund_evidence"), row.get("code"))
        display = original if original["available"] else latest.get(str(row.get("code")))
        if display is None:
            display = main_fund_display_evidence(
                {}, trade_date=trade_date, as_of_at=as_of_at,
            )
            if read_failed:
                display["clock_status"] = "read_error"
        result.append({
            **row,
            "fund_display": display,
            "orderbook_display": _anomaly_orderbook_display(
                row.get("detail") or {}, as_of_at=as_of_at,
            ),
            "events": [
                {**event, "fund_display": event_display(event.get("fund_display"), row.get("code"))}
                for event in row.get("events") or []
            ],
        })
    return result


def _cached_b1_analysis(code: str, bars: list[dict]) -> dict:
    """Memoize only deterministic technical analysis of identical owned bars.

    We still read current K lines/quotes/funds on every request. A repaired old
    bar or a changed live bar invalidates the content key; buy-point eligibility,
    source clocks and funding evidence are deliberately outside this cache.
    """
    try:
        raw = json.dumps(bars, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)
        key = (code, analyze_b1_signal, hashlib.sha256(raw.encode()).digest())
    except (TypeError, ValueError):
        return analyze_b1_signal(code, bars)
    cached = _B1_ANALYSIS_CACHE.get(key)
    if cached is not None:
        _B1_ANALYSIS_CACHE.move_to_end(key)
        return copy.deepcopy(cached)
    result = analyze_b1_signal(code, bars)
    _B1_ANALYSIS_CACHE[key] = copy.deepcopy(result)
    while len(_B1_ANALYSIS_CACHE) > _B1_ANALYSIS_CACHE_MAX_ENTRIES:
        _B1_ANALYSIS_CACHE.popitem(last=False)
    return result


async def _enrich_stock_rows_with_b1(
    rows: list[dict],
    db: AsyncSession,
    target_date: date | None = None,
) -> list[dict]:
    if not rows:
        return rows

    codes = [str(row.get("code") or "").strip() for row in rows if str(row.get("code") or "").strip()]
    if not codes:
        return rows

    today = target_date or await resolve_latest_trade_date(db, StockKline.trade_date)
    from app.data.main_fund import load_current_main_fund_map
    fund_map = await load_current_main_fund_map(
        db, trade_date=today, decision_at=datetime.now(), codes=codes,
    )
    # Even a no-K-line early return must not retain a stale input fund value.
    qualified_rows = []
    for row in rows:
        current_fund = fund_map.get(str(row.get("code") or "").strip()) or {}
        qualified_rows.append({
            **row,
            "main_net_inflow": current_fund.get("main_net_inflow"),
            "detail": {
                **(row.get("detail") or {}),
                "main_net_inflow": current_fund.get("main_net_inflow"),
                "main_net_inflow_pct": current_fund.get("main_net_inflow_pct"),
                "super_net_inflow": current_fund.get("super_net_inflow"),
                "super_net_inflow_pct": current_fund.get("super_net_inflow_pct"),
                "big_net_inflow": current_fund.get("big_net_inflow"),
                "big_net_inflow_pct": current_fund.get("big_net_inflow_pct"),
                "mid_net_inflow": current_fund.get("mid_net_inflow"),
                "mid_net_inflow_pct": current_fund.get("mid_net_inflow_pct"),
                "small_net_inflow": current_fund.get("small_net_inflow"),
                "small_net_inflow_pct": current_fund.get("small_net_inflow_pct"),
                "as_of": (current_fund["source_quote_at"].isoformat()
                          if isinstance(current_fund.get("source_quote_at"), datetime)
                          else current_fund.get("source_quote_at")),
                "clock_basis": current_fund.get("source_clock_basis"),
                "source": (("fund_flow" if current_fund.get("source") == "tencent"
                            else "eastmoney_main_fund") if current_fund else "unavailable"),
                "provider_source": current_fund.get("source"),
                "source_version": current_fund.get("source_version"),
                "source_quote_at": current_fund.get("source_quote_at"),
                "received_at": current_fund.get("received_at"),
                "observed_at": current_fund.get("observed_at"),
                "clock_status": "ok" if current_fund else "unknown",
                "is_stale": not bool(current_fund),
            },
        })
    rows = qualified_rows
    recent_dates_result = await db.execute(
        select(StockKline.trade_date)
        .where(StockKline.trade_date <= today)
        .distinct()
        .order_by(desc(StockKline.trade_date))
        .limit(180)
    )
    recent_dates = [item[0] for item in recent_dates_result.all() if item[0] is not None]
    if not recent_dates:
        return rows

    kline_result = await db.execute(
        select(
            StockKline.code,
            StockKline.trade_date,
            StockKline.open,
            StockKline.high,
            StockKline.low,
            StockKline.close,
            StockKline.volume,
            StockKline.turnover,
            StockKline.change_pct,
        ).where(
            StockKline.code.in_(codes),
            StockKline.trade_date.in_(recent_dates),
        ).order_by(StockKline.code, StockKline.trade_date)
    )
    kline_map: dict[str, list[dict]] = {}
    for code, trade_date, open_price, high_price, low_price, close_price, volume, turnover, change_pct in kline_result.all():
        kline_map.setdefault(code, []).append(
            {
                "trade_date": trade_date,
                "open": _safe_float(open_price),
                "high": _safe_float(high_price),
                "low": _safe_float(low_price),
                "close": _safe_float(close_price),
                "volume": _safe_float(volume),
                "turnover": _safe_float(turnover),
                "change_pct": _safe_float(change_pct),
            }
        )

    spot_result = await db.execute(
        select(StockSpot).where(StockSpot.code.in_(codes))
    )
    spot_map = {spot.code: spot for spot in spot_result.scalars().all()}

    mapping_result = await db.execute(
        select(StockSectorMapping.code, StockSectorMapping.sector_name, StockSectorMapping.sector_type)
        .where(StockSectorMapping.code.in_(codes))
    )
    sector_map: dict[str, list[dict]] = {}
    for code, sector_name, sector_type in mapping_result.all():
        sector_map.setdefault(code, []).append({
            "sector_name": sector_name,
            "sector_type": sector_type,
        })

    enriched_rows = []
    for row in rows:
        code = str(row.get("code") or "").strip()
        row_copy = dict(row)
        bars = [dict(item) for item in (kline_map.get(code) or [])]
        spot = spot_map.get(code)
        live_bar = _build_live_b1_bar(spot, today, latest_bar=bars[-1] if bars else None)
        if live_bar:
            bars.append(live_bar)

        industry_name, sub_industry_name = _resolve_b1_industry_names(sector_map.get(code) or [])
        analysis = _cached_b1_analysis(code, bars) if len(bars) >= 60 else {}

        detail = row_copy.get("detail") or {}
        current_price_raw = spot.price if spot and spot.price is not None else detail.get("price")
        day_change_pct_raw = spot.change_pct if spot and spot.change_pct is not None else detail.get("change_pct")
        turnover_raw = spot.turnover if spot and spot.turnover is not None else detail.get("turnover")
        if turnover_raw is None and bars:
            turnover_raw = bars[-1].get("turnover")
        current_price = _safe_float(current_price_raw)
        day_change_pct = _safe_float(day_change_pct_raw)
        current_turnover = _safe_float(turnover_raw)
        # Current real zero is evidence; absence is unknown, never a quote-field
        # or previous-day fallback. B1 must share the same qualified collection.
        current_fund = fund_map.get(code) or {}
        main_net_inflow = current_fund.get("main_net_inflow")

        priority_bonus = int(analysis.get("priority_bonus") or 0)
        priority_score = (row_copy.get("display_score") or 0) + priority_bonus
        signal_status = _resolve_b1_signal_status(
            analysis.get("signal_key"),
            uses_live_bar=bool(live_bar),
        )

        row_copy.update(
            {
                "industry": industry_name,
                "sub_industry": sub_industry_name,
                "b1_signal": analysis.get("signal_label") or "",
                "b1_signal_key": analysis.get("signal_key") or "",
                "b1_j": analysis.get("j"),
                "b1_rsi": analysis.get("rsi"),
                "b1_short_score": analysis.get("short_score"),
                "b1_long_score": analysis.get("long_score"),
                "b1_hold_score": analysis.get("hold_score"),
                "b1_kdj_signal": analysis.get("kdj_signal") or "",
                "b1_success_rate_3d": analysis.get("success_rate_3d"),
                "b1_success_samples": analysis.get("success_samples") or 0,
                "b1_success_scope": analysis.get("success_scope") or "",
                "b1_history_high_confidence": bool(analysis.get("history_high_confidence")),
                "b1_success_rate_5d": analysis.get("success_rate_5d"),
                "b1_success_samples_5d": analysis.get("success_samples_5d") or 0,
                "b1_success_scope_5d": analysis.get("success_scope_5d") or "",
                "b1_history_high_confidence_5d": bool(analysis.get("history_high_confidence_5d")),
                "b1_trend_white_price": analysis.get("trend_white_price"),
                "b1_break_trend": bool(analysis.get("is_break_trend")),
                "b1_signal_status": signal_status,
                "b1_signal_status_label": B1_STATUS_LABELS.get(signal_status, ""),
                "b1_priority_bonus": priority_bonus,
                "priority_score": priority_score,
                "current_price": round(current_price, 2) if current_price is not None else None,
                "day_change_pct": round(day_change_pct, 2) if day_change_pct is not None else None,
                "change_pct_5d": _calc_change_pct_over_days(bars, days=5),
                "close_price": analysis.get("close") if analysis.get("close") is not None else (_safe_float(bars[-1].get("close")) if bars else None),
                "turnover_rate": round(current_turnover, 2) if current_turnover is not None else None,
                "main_net_inflow": main_net_inflow,
            }
        )
        b1_pushable, b1_push_blockers = _resolve_b1_push_eligibility(row_copy)
        row_copy["b1_pushable"] = b1_pushable
        row_copy["b1_push_blockers"] = b1_push_blockers
        row_copy = _apply_stock_row_buy_point_status(row_copy)
        enriched_rows.append(row_copy)

    return enriched_rows


def _normalize_json_payload(payload: dict) -> dict:
    try:
        return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str))
    except Exception:
        return payload


def _monitor_market_session(now: datetime) -> str:
    if now.weekday() >= 5:
        return "weekend"
    if is_official_closed_day(now.date()):
        return "holiday"
    return trade_calendar.get_trade_session(now)


def _monitor_snapshot_ttl(trade_date: date, now: datetime) -> int:
    # Leave lunch, auction and the first post-close collection window unchanged.
    # At/after 09:15 on the next weekday an overnight snapshot is not extended.
    clock = (now.hour, now.minute)
    settled = trade_date == now.date() and clock >= (15, 15)
    overnight = trade_date < now.date() and (clock < (9, 15) or now.weekday() >= 5 or clock >= (15, 15))
    return ANOMALY_MONITOR_CLOSED_CACHE_TTL_SECONDS if settled or overnight else ANOMALY_CACHE_TTL_SECONDS


def _snapshot_age_seconds(payload: dict) -> float | None:
    try:
        at = datetime.fromisoformat(str(payload.get("snapshot_time") or ""))
        return (datetime.now() - at).total_seconds()
    except (TypeError, ValueError):
        return None


def _get_cached_anomaly_snapshot(trade_date: date, *, ttl_seconds: int | None = None):
    cached = _ANOMALY_SNAPSHOT_CACHE.get(str(trade_date))
    if not cached:
        return None
    ttl = ANOMALY_CACHE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    payload = cached["payload"]
    if str(payload.get("trade_date") or "") != str(trade_date):
        return None
    age = _snapshot_age_seconds(payload)
    # Loading a persisted snapshot must not restart its absolute lifetime.
    if age is None or age < 0 or age > ttl or time.time() - cached["ts"] > ttl:
        return None
    return _enrich_snapshot_payload(payload)


def _get_any_cached_anomaly_snapshot():
    if not _ANOMALY_SNAPSHOT_CACHE:
        return None
    cached = max(_ANOMALY_SNAPSHOT_CACHE.values(), key=lambda item: item.get("ts", 0))
    payload = cached.get("payload")
    if not isinstance(payload, dict):
        return None
    payload = _enrich_snapshot_payload(copy.deepcopy(payload))
    payload["degraded"] = True
    payload["degrade_reason"] = "异动扫描正忙，使用最近内存快照"
    return payload


def _set_cached_anomaly_snapshot(trade_date: date, payload: dict):
    _ANOMALY_SNAPSHOT_CACHE[str(trade_date)] = {
        "ts": time.time(),
        "payload": payload,
    }
    # Retain the current and a small number of recent trading dates only.
    while len(_ANOMALY_SNAPSHOT_CACHE) > 3:
        oldest = min(_ANOMALY_SNAPSHOT_CACHE, key=lambda key: _ANOMALY_SNAPSHOT_CACHE[key]["ts"])
        _ANOMALY_SNAPSHOT_CACHE.pop(oldest, None)


def _enrich_snapshot_payload(payload: dict) -> dict:
    anomalies_raw = list(payload.get("anomalies") or [])
    if not anomalies_raw:
        return payload
    # Cached grades are not live evidence. Re-evaluate owned rows at read time;
    # never refill their frozen funds from a later FundFlow record.

    anomalies_by_code: dict[str, list[dict]] = {}
    for item in anomalies_raw:
        code = str(item.get("code") or "")
        if code:
            anomalies_by_code.setdefault(code, []).append(item)
    anomalies = [
        _enrich_anomaly_display(item, anomalies_by_code.get(str(item.get("code") or ""), []))
        for item in anomalies_raw
    ]
    enriched = dict(payload)
    enriched["anomalies"] = anomalies
    enriched["summary"] = _summarize_anomalies(anomalies)
    return enriched


async def _get_persisted_anomaly_snapshot(
    db: AsyncSession, trade_date: date, *, ttl_seconds: int | None = None,
):
    result = await db.execute(
        select(DashboardSnapshot)
        .where(
            and_(
                DashboardSnapshot.snapshot_key == _anomaly_snapshot_key(),
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
    ttl = ANOMALY_CACHE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
    row_age = (datetime.now() - row.snapshot_time).total_seconds()
    if row_age < 0 or row_age > ttl:
        return None
    try:
        payload = json.loads(row.payload_json)
        if not isinstance(payload, dict) or str(payload.get("trade_date") or "") != str(trade_date):
            return None
        age = _snapshot_age_seconds(payload)
        if age is None or age < 0 or age > ttl:
            return None
        payload = _enrich_snapshot_payload(payload)
        _set_cached_anomaly_snapshot(trade_date, payload)
        return payload
    except Exception:
        return None


async def _get_latest_persisted_anomaly_snapshot(
    db: AsyncSession, trade_date: date, *, as_of_at: datetime | None = None,
):
    payload = await _get_latest_persisted_dashboard_snapshot(
        db,
        _anomaly_snapshot_key(),
        trade_date,
        **({"as_of_at": as_of_at} if as_of_at is not None else {}),
    )
    if not isinstance(payload, dict):
        return None
    return _enrich_snapshot_payload(payload)


async def _persist_anomaly_snapshot(db: AsyncSession, trade_date: date, payload: dict):
    payload = _normalize_json_payload(payload)
    payload_json = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str)
    result = await db.execute(
        select(DashboardSnapshot)
        .where(
            and_(
                DashboardSnapshot.snapshot_key == _anomaly_snapshot_key(),
                DashboardSnapshot.trade_date == trade_date,
            )
        )
        .order_by(desc(DashboardSnapshot.snapshot_time))
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = DashboardSnapshot(
            snapshot_key=_anomaly_snapshot_key(),
            trade_date=trade_date,
            snapshot_time=datetime.now(),
            payload_json=payload_json,
            status="ok",
        )
        db.add(row)
    else:
        row.snapshot_time = datetime.now()
        row.payload_json = payload_json
        row.status = "ok"
    _set_cached_anomaly_snapshot(trade_date, payload)
    try:
        await db.flush()
        await db.commit()
    except OperationalError as exc:
        await db.rollback()
        logger.warning(f"异动快照持久化跳过，数据库正忙: {trade_date} {exc}")


async def _resolve_anomaly_trade_date(
    db: AsyncSession,
    requested: date | None = None,
) -> date:
    """异动扫描日期以实时行情为准，不能被滞后的板块派生表拖回前一日。"""
    if requested is not None:
        return requested

    current_day = date.today()
    latest_quote_at = await db.scalar(select(func.max(StockSpot.updated_at)))
    if (
        isinstance(latest_quote_at, datetime)
        and latest_quote_at.date() == current_day
        and await trade_calendar.is_trade_day(current_day)
    ):
        return current_day
    return await resolve_latest_trade_date(db, SectorPersistence.trade_date)


async def prewarm_anomaly_snapshot(
    db: AsyncSession,
    trade_date: date | None = None,
    force_refresh: bool = False,
    *,
    monitor_read: bool = False,
) -> dict:
    target_date = await _resolve_anomaly_trade_date(db, trade_date)

    # Lunch GETs show the last recorded snapshot, not a new scan of stopped
    # quotes/funds. Keep its original clock and never persist this projection.
    # Default/forced consumers retain their existing 45-second policy.
    now = datetime.now()
    if (monitor_read and not force_refresh
            and _monitor_market_session(now) == "lunch_break"):
        read_failed = False
        try:
            saved = await _get_latest_persisted_anomaly_snapshot(db, target_date, as_of_at=now)
        except SQLAlchemyError:
            await db.rollback()
            saved, read_failed = None, True
        cached = (_ANOMALY_SNAPSHOT_CACHE.get(str(target_date)) or {}).get("payload")
        candidates = []
        for item in (saved, cached):
            if target_date > now.date() or not isinstance(item, dict) or str(item.get("trade_date") or "") != str(target_date):
                continue
            age = _snapshot_age_seconds(item)
            if age is not None and age >= 0:
                candidates.append((age, item))
        if candidates:
            result = _enrich_snapshot_payload(copy.deepcopy(min(candidates, key=lambda pair: pair[0])[1]))
            if read_failed:
                result.update(degraded=True, degrade_reason="午休快照读取失败，使用同日内存快照")
            return result
        return {
            "trade_date": str(target_date), "snapshot_time": None,
            "anomalies": [], "summary": {}, "b1_states": [], "b1_summary": {},
            "degraded": True, "degrade_reason": "午休暂无可用已存快照；不重新扫描陈旧行情",
        }

    async def available():
        current_at = datetime.now()
        options = {"ttl_seconds": _monitor_snapshot_ttl(target_date, current_at)} if monitor_read else {}

        def eligible(payload):
            if payload is None:
                return None
            # A force/legacy producer may have minted a lunch snapshot shortly
            # before 13:00. Its short wall-clock age is not a live-session scan.
            if monitor_read and _monitor_market_session(current_at) in {"morning", "afternoon"}:
                captured_at = evidence_clock(payload.get("snapshot_time"))
                if captured_at is None or _monitor_market_session(captured_at) not in {"morning", "afternoon"}:
                    return None
            return payload

        cached = eligible(_get_cached_anomaly_snapshot(target_date, **options))
        if cached is not None:
            return cached
        return eligible(await _get_persisted_anomaly_snapshot(db, target_date, **options))

    if not force_refresh:
        cached = await available()
        if cached is not None:
            return cached

    # One scanner owns its mutable intraday state per event loop. API cold reads
    # share the completed snapshot; explicit force_refresh retains its meaning.
    loop = asyncio.get_running_loop()
    lock = _ANOMALY_SCAN_LOCKS.setdefault(loop, asyncio.Lock())
    async with lock:
        if not force_refresh:
            cached = await available()
            if cached is not None:
                return cached
        return await _scan_anomaly_snapshot(db, target_date)


async def _scan_anomaly_snapshot(db: AsyncSession, target_date: date) -> dict:
    fund_decision_at = datetime.now()
    current_fund_snapshot = await prewarm_eastmoney_main_fund_snapshot(
        db,
        trade_date=target_date,
        # 只读scheduler已提交的权威资金；API/启动预热不会触发第二路采集。
        force_refresh=False,
    )
    current_fund_map, fund_source = _resolve_anomaly_current_fund_context(current_fund_snapshot)
    # Scanner loads the authoritative completed-session window once, at this
    # original cutoff (not a later clock after cache/DB awaits cross 15:00).
    events = await anomaly_scanner.scan_market(
        db,
        min_score=0,
        target_date=target_date,
        current_fund_map=current_fund_map,
        as_of_at=fund_decision_at,
        fund_trade_date=target_date,
        fund_snapshot_is_stale=False,
        fund_source=fund_source,
    )
    anomalies_raw = [_serialize_event(event) for event in events]
    anomalies_by_code: dict[str, list[dict]] = {}
    for item in anomalies_raw:
        code = str(item.get("code") or "")
        if code:
            anomalies_by_code.setdefault(code, []).append(item)
    anomalies = [
        _enrich_anomaly_display(item, anomalies_by_code.get(str(item.get("code") or ""), []))
        for item in anomalies_raw
    ]
    b1_states = await _build_b1_state_rows_for_snapshot(anomalies, db)
    payload = {
        "trade_date": str(target_date),
        "snapshot_time": datetime.now().isoformat(),
        "anomalies": anomalies,
        "summary": _summarize_anomalies(anomalies),
        "b1_states": b1_states,
        "b1_summary": _summarize_b1_states(b1_states),
    }
    await _persist_anomaly_snapshot(db, target_date, payload)
    return payload


def _anomaly_identity(anomaly: dict) -> str:
    return "|".join(
        [
            str(anomaly.get("code") or ""),
            str(anomaly.get("event_type") or ""),
            str(anomaly.get("description") or ""),
        ]
    )


def _anomaly_signal_identity(anomaly: dict) -> str:
    """更稳定的异动信号键.

    目标:
    - 避免金额、百分比、描述细节变化导致同一信号被误判为“新异动”
    - 保留真正不同信号类型的区分能力
    """
    code = str(anomaly.get("code") or "")
    event_type = str(anomaly.get("event_type") or "")
    detail = anomaly.get("detail") or {}

    if event_type in {"capital", "pump_dump"}:
        subtype = str(detail.get("capital_anomaly_type") or event_type)
        return f"{code}|{event_type}|{subtype}"

    if event_type == "breakthrough":
        signal_type = str(detail.get("signal_type") or "breakthrough")
        if signal_type == "positive_acceleration":
            phase = (
                "limit_up"
                if detail.get("positive_acceleration_limit_up")
                else "pre_limit_up"
                if detail.get("positive_acceleration_pre_limit_up")
                else "catchup"
                if detail.get("positive_acceleration_catchup")
                else f"rolling_{str(detail.get('rolling_60s_tier') or 'watch')}"
                if detail.get("positive_acceleration_rolling60")
                else "regular"
            )
            return f"{code}|{event_type}|{signal_type}|{phase}"
        raw_anchor = (
            detail.get("breakout_anchor")
            or detail.get("high_20d")
            or detail.get("ma")
            or detail.get("alignment")
            or detail.get("boll_upper")
            or signal_type
        )
        if isinstance(raw_anchor, (int, float)):
            anchor = f"{float(raw_anchor):.3f}"
        else:
            anchor = str(raw_anchor)
        return f"{code}|{event_type}|{signal_type}|{anchor}"

    if (
        event_type == "low_absorb"
        and str(detail.get("signal_type") or "")
        in {
            "underwater_acceleration",
            "underwater_reversal",
            "trend_support_touch",
            "main_wave_shape_pullback_reclaim",
            "main_wave_support_reclaim",
            "second_wave_restart",
        }
    ):
        return f"{code}|{event_type}|{str(detail.get('signal_type') or '')}"

    if event_type in {"limit_up", "limit_down"}:
        consecutive_days = _safe_int(detail.get("consecutive_days"), 1)
        return f"{code}|{event_type}|{consecutive_days}"

    desc = str(anomaly.get("description") or "").strip()
    desc = re.sub(r"[-+]?\d+(?:\.\d+)?亿", "{AMOUNT_YI}", desc)
    desc = re.sub(r"[-+]?\d+(?:\.\d+)?万", "{AMOUNT_WAN}", desc)
    desc = re.sub(r"[-+]?\d+(?:\.\d+)?%", "{PCT}", desc)
    return f"{code}|{event_type}|{desc}"


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _intraday_acceleration_volume_ready(detail: dict) -> bool:
    """急拉必须由同一短周期窗口的增量成交额确认，全天量比不能替代。"""
    if (
        detail.get("positive_acceleration_rolling60")
        or detail.get("main_wave_green_open_reclaim_rolling60")
        or detail.get("main_wave_shape_pullback_rolling60")
        or detail.get("main_wave_support_reclaim_rolling60")
        or detail.get("tenbagger_pullback_confirmed")
        or detail.get("sector_core_laggard_confirmed")
    ):
        return bool(
            detail.get("rolling_60s_confirmed")
            and detail.get("rolling_60s_path_confirmed", True)
            and _safe_float(detail.get("rolling_60s_amount_delta"))
            >= settings.ANOMALY_ACCELERATION_MIN_AMOUNT_DELTA
            and _safe_float(detail.get("rolling_60s_amount_pace_ratio"))
            >= settings.ANOMALY_ACCELERATION_MIN_AMOUNT_PACE_RATIO
        )
    return bool(
        detail.get("intraday_amount_confirmed")
        and _safe_float(detail.get("intraday_amount_delta"))
        >= settings.ANOMALY_ACCELERATION_MIN_AMOUNT_DELTA
        and _safe_float(detail.get("intraday_amount_pace_ratio"))
        >= settings.ANOMALY_ACCELERATION_MIN_AMOUNT_PACE_RATIO
    )


def _has_acceleration_distribution_risk(detail: dict) -> bool:
    """推送层二次拦截撤单或卖盘压制，避免旧缓存绕过扫描器。"""
    withdrawal_ratio = _safe_float(detail.get("withdrawal_ratio"))
    imbalance = _safe_float(detail.get("orderbook_imbalance"))
    bid_depth_5 = _safe_float(detail.get("bid_depth_5"))
    ask_depth_5 = _safe_float(detail.get("ask_depth_5"))
    return bool(
        withdrawal_ratio >= settings.ANOMALY_ACCELERATION_MAX_WITHDRAWAL_RATIO
        or imbalance <= -0.15
        or (bid_depth_5 > 0 and ask_depth_5 > bid_depth_5 * 1.35)
    )


def _empty_market_breadth_context(reason: str = "市场宽度样本不足") -> dict:
    return {
        "avg_change": 0.0,
        "up_ratio": 0.0,
        "index_ret5": 0.0,
        "index_ret20": 0.0,
        "index_above_ma20": False,
        "index_above_ma60": False,
        "market_regime": "neutral",
        "market_regime_label": "数据不足/中性",
        "market_regime_reason": reason,
        "direct_buy_ok": False,
        "direct_buy_blockers": [reason],
    }


def _classify_market_breadth(
    *,
    index_above_ma20: bool,
    index_above_ma60: bool,
    up_ratio: float,
    avg_change: float,
    index_ret5: float,
    index_ret20: float,
) -> dict:
    """将市场宽度转成牛熊/极强窗口，供明日预案直接买点风控使用。"""
    direct_buy_ok = (
        index_above_ma20
        and index_above_ma60
        and up_ratio >= _MARKET_DIRECT_UP_RATIO
        and avg_change >= _MARKET_DIRECT_AVG_CHANGE
        and index_ret5 >= _MARKET_DIRECT_RET5
        and _MARKET_DIRECT_RET20_MIN <= index_ret20 <= _MARKET_DIRECT_RET20_MAX
    )

    if direct_buy_ok:
        regime = "hot"
        label = "极强窗口"
        reason = "市场宽度、趋势和5日动能均满足v31直接买点"
    elif index_above_ma60 and index_ret20 >= 0:
        regime = "bull"
        label = "牛市/强势"
        reason = "站上60日线且20日收益为正，但未达到v31极强窗口"
    elif (not index_above_ma60) and index_ret20 < 0:
        regime = "bear"
        label = "熊市/弱势"
        reason = "跌破60日线且20日收益为负，明日预案不生成直接买点"
    else:
        regime = "neutral"
        label = "中性/震荡"
        reason = "趋势或20日方向未形成稳定强势，明日预案以观察为主"

    blockers: list[str] = []
    if not index_above_ma20:
        blockers.append("合成指数未站上MA20")
    if not index_above_ma60:
        blockers.append("合成指数未站上MA60")
    if up_ratio < _MARKET_DIRECT_UP_RATIO:
        blockers.append(f"上涨家数占比不足{_MARKET_DIRECT_UP_RATIO:.0%}")
    if avg_change < _MARKET_DIRECT_AVG_CHANGE:
        blockers.append(f"全市场平均涨幅不足{_MARKET_DIRECT_AVG_CHANGE:.1f}%")
    if index_ret5 < _MARKET_DIRECT_RET5:
        blockers.append(f"5日合成指数涨幅不足{_MARKET_DIRECT_RET5:.1f}%")
    if index_ret20 < _MARKET_DIRECT_RET20_MIN:
        blockers.append(f"20日合成指数涨幅不足{_MARKET_DIRECT_RET20_MIN:.1f}%")
    elif index_ret20 > _MARKET_DIRECT_RET20_MAX:
        blockers.append(f"20日合成指数涨幅超过{_MARKET_DIRECT_RET20_MAX:.1f}%，短线过热")
    if regime == "bear":
        blockers.insert(0, "熊市/弱势环境不生成直接买点")
    if not blockers and not direct_buy_ok:
        blockers.append(reason)

    return {
        "market_regime": regime,
        "market_regime_label": label,
        "market_regime_reason": reason,
        "direct_buy_ok": bool(direct_buy_ok),
        "direct_buy_blockers": blockers[:6],
    }


async def _get_market_breadth_context(db: AsyncSession, target_date: date) -> dict:
    """用全市场K线构造可回测的市场宽度过滤，避免弱市硬给直接买点。"""
    try:
        date_result = await db.execute(
            select(StockKline.trade_date)
            .where(StockKline.trade_date <= target_date)
            .group_by(StockKline.trade_date)
            .order_by(desc(StockKline.trade_date))
            .limit(65)
        )
        recent_dates = [row[0] for row in date_result.all()]
        if len(recent_dates) < 20:
            return _empty_market_breadth_context()

        from sqlalchemy import text

        ordered_dates = sorted(recent_dates)
        date_params = {f"date_{idx}": str(d) for idx, d in enumerate(ordered_dates)}
        placeholders = ", ".join(f":date_{idx}" for idx in range(len(ordered_dates)))
        result = await db.execute(
            text(f"""
                SELECT
                    trade_date,
                    AVG(COALESCE(change_pct, 0)) AS avg_change,
                    SUM(CASE WHEN COALESCE(change_pct, 0) > 0 THEN 1 ELSE 0 END) AS up_count,
                    COUNT(*) AS total_count
                FROM stock_kline
                WHERE trade_date IN ({placeholders})
                  AND (
                    substr(code, 1, 3) IN ('000', '001', '002', '003', '600', '601', '603', '605')
                  )
                GROUP BY trade_date
                ORDER BY trade_date ASC
            """),
            date_params,
        )
        rows = result.all()
        if len(rows) < 20:
            return _empty_market_breadth_context()

        index_level = 1000.0
        index_points: list[float] = []
        target_row = None
        for row in rows:
            avg_change = _safe_float(row[1])
            up_count = _safe_float(row[2])
            total_count = _safe_float(row[3])
            index_level *= 1 + avg_change / 100
            index_points.append(index_level)
            if str(row[0]) == str(target_date):
                target_row = row

        if target_row is None:
            target_row = rows[-1]

        avg_change = _safe_float(target_row[1])
        total_count = _safe_float(target_row[3])
        up_ratio = _safe_float(target_row[2]) / total_count if total_count > 0 else 0.0
        ma20 = sum(index_points[-20:]) / 20
        ma60 = sum(index_points[-60:]) / 60 if len(index_points) >= 60 else None
        index_ret5 = (
            (index_points[-1] / index_points[-6] - 1) * 100
            if len(index_points) >= 6 and index_points[-6] > 0
            else 0.0
        )
        index_ret20 = (
            (index_points[-1] / index_points[-21] - 1) * 100
            if len(index_points) >= 21 and index_points[-21] > 0
            else 0.0
        )
        index_above_ma20 = index_points[-1] >= ma20
        index_above_ma60 = ma60 is not None and index_points[-1] >= ma60
        classification = _classify_market_breadth(
            index_above_ma20=bool(index_above_ma20),
            index_above_ma60=bool(index_above_ma60),
            up_ratio=up_ratio,
            avg_change=avg_change,
            index_ret5=index_ret5,
            index_ret20=index_ret20,
        )
        return {
            "avg_change": round(avg_change, 3),
            "up_ratio": round(up_ratio, 3),
            "index_ret5": round(index_ret5, 3),
            "index_ret20": round(index_ret20, 3),
            "index_level": round(index_points[-1], 3),
            "index_ma20": round(ma20, 3),
            "index_ma60": round(ma60, 3) if ma60 is not None else None,
            "index_above_ma20": bool(index_above_ma20),
            "index_above_ma60": bool(index_above_ma60),
            **classification,
        }
    except Exception as exc:
        logger.warning(f"[market_breadth] 计算失败: {exc}")
        return _empty_market_breadth_context("市场宽度计算失败")


async def _load_market_regime_context(db: AsyncSession, target_date: date) -> dict:
    """Reuse Dashboard 2.0 and the risk circuit-breaker market definitions."""
    from app.dashboard2.service import dashboard2_service
    from app.risk.circuit_breaker import sentiment_circuit_breaker

    sentiment_state = await sentiment_circuit_breaker.get_current_state(db, target_date)
    sh = await dashboard2_service._query_latest_index(db, "000001", target_date)
    sz = await dashboard2_service._query_latest_index(db, "399001", target_date)
    cyb = await dashboard2_service._query_latest_index(db, "399006", target_date)
    limit_up_count = (
        await db.execute(
            select(func.count(LimitUpPool.id)).where(LimitUpPool.trade_date == target_date)
        )
    ).scalar() or 0
    limit_down_count = (
        await db.execute(
            select(func.count(LimitDownPool.id)).where(LimitDownPool.trade_date == target_date)
        )
    ).scalar() or 0
    judge_context = {
        "limit_up_count_actual": int(limit_up_count),
        "limit_down_count_actual": int(limit_down_count),
    }
    market_environment = dashboard2_service._judge_market_environment(
        sh,
        sz,
        cyb,
        sentiment_state,
        judge_context,
    )
    phase = str(getattr(sentiment_state, "phase", "") or "pending")
    sentiment_trade_date = getattr(sentiment_state, "trade_date", None)
    if sentiment_trade_date is None:
        phase = "pending"
    return {
        "market_environment": market_environment,
        "sentiment_phase": phase,
        "sentiment_cycle": dashboard2_service.SENTIMENT_CYCLE_LABELS.get(phase, "待定"),
        "sentiment_trade_date": sentiment_trade_date,
        "sentiment_score": _safe_float(getattr(sentiment_state, "score", None), 0),
        "limit_up_count": int(limit_up_count),
        "limit_down_count": int(limit_down_count),
        "seal_rate": _safe_float(getattr(sentiment_state, "seal_rate", None), 0),
        "index_trade_dates": {
            "sh": str(sh.trade_date) if sh is not None else None,
            "sz": str(sz.trade_date) if sz is not None else None,
            "cyb": str(cyb.trade_date) if cyb is not None else None,
        },
    }


async def _judge_market_env(db: AsyncSession, target_date: date) -> tuple[str, str]:
    """Backward-compatible tuple wrapper around the canonical market regime."""
    try:
        context = await _load_market_regime_context(db, target_date)
        return context["market_environment"], context["sentiment_cycle"]
    except Exception as exc:
        logger.warning(f"[market_regime] 统一大盘/情绪口径加载失败: {exc}")
        return "neutral", "待定"


async def _load_stock_plan_sector_context(
    db: AsyncSession,
    code: str,
    target_date: date,
    stock_change_pct: float,
) -> dict:
    """Select one causal driver and use SectorLifecycle as the sole state source."""
    result = await db.execute(
        select(
            StockSectorMapping.sector_code.label("sector_code"),
            StockSectorMapping.sector_name.label("mapping_name"),
            StockSectorMapping.sector_type.label("mapping_type"),
            StockSectorMapping.source.label("mapping_source"),
            StockSectorMapping.weight.label("mapping_weight"),
            SectorPersistence.sector_name.label("sector_name"),
            SectorPersistence.change_pct.label("change_pct"),
            SectorPersistence.fund_flow.label("fund_flow"),
            SectorPersistence.strength_score.label("strength_score"),
            SectorPersistence.consecutive_days.label("consecutive_days"),
            SectorLifecycle.lifecycle_state.label("lifecycle_state"),
            SectorLifecycle.state_score.label("lifecycle_score"),
            SectorLifecycle.active_days.label("active_days"),
            SectorLifecycle.kline_trend.label("kline_trend"),
            SectorLifecycle.is_main_line.label("is_main_line"),
        )
        .select_from(StockSectorMapping)
        .join(
            SectorPersistence,
            and_(
                StockSectorMapping.sector_code == SectorPersistence.sector_code,
                SectorPersistence.trade_date == target_date,
            ),
        )
        .outerjoin(
            SectorLifecycle,
            and_(
                StockSectorMapping.sector_code == SectorLifecycle.sector_code,
                SectorLifecycle.trade_date == target_date,
            ),
        )
        .where(StockSectorMapping.code == code)
    )
    candidates = []
    lifecycle_rank = {
        "accelerating": 4,
        "emerging": 3,
        "climax": 1,
        "dormant": 0,
        "diverging": -1,
        "declining": -2,
        "one_day": -3,
    }
    for row in result.mappings().all():
        item = dict(row)
        name = str(item.get("sector_name") or item.get("mapping_name") or "")
        mapping_type = str(item.get("mapping_type") or "").strip().lower()
        mapping_source = str(item.get("mapping_source") or "").strip().lower()
        is_primary_industry = mapping_source == "sw" and (
            mapping_type == "industry" or mapping_type.startswith("sw_l")
        )
        if not is_primary_industry and not _is_causal_trade_driver_sector({
            "sector_name": name,
            "sector_type": mapping_type,
            "source": mapping_source,
        }):
            continue
        state = str(item.get("lifecycle_state") or "").strip().lower()
        type_rank = 3 if mapping_type == "industry" else 2 if mapping_type.startswith("sw_l") else 1
        selection_rank = (
            1 if state in _NEXT_DAY_ALLOWED_SECTOR_LIFECYCLES else 0,
            lifecycle_rank.get(state, -4),
            1 if item.get("is_main_line") else 0,
            type_rank,
            _safe_float(item.get("mapping_weight")),
            _safe_float(item.get("strength_score")),
            _safe_float(item.get("fund_flow")),
        )
        item.update({"name": name, "state": state, "selection_rank": selection_rank})
        candidates.append(item)

    if not candidates:
        return {
            "available": False,
            "sector_change_pct": 0.0,
            "sector_lifecycle_state": "",
            "sector_lifecycle_label": "生命周期缺失",
            "sector_status": "板块数据不足",
            "sector_resonance": "数据不足",
            "sector_resonance_score": 0.0,
            "sector_resonance_name": "",
            "sector_resonance_change": 0.0,
            "sector_resonance_fund": 0.0,
        }

    best = max(candidates, key=lambda item: item["selection_rank"])
    state = str(best.get("state") or "")
    lifecycle_label = _SECTOR_LIFECYCLE_LABELS.get(state, "生命周期缺失")
    sector_change = _safe_float(best.get("change_pct"))
    sector_fund = _safe_float(best.get("fund_flow"))
    sector_strength = _safe_float(best.get("strength_score"))
    stock_change = _safe_float(stock_change_pct)
    positive_lifecycle = state in _NEXT_DAY_ALLOWED_SECTOR_LIFECYCLES

    if positive_lifecycle and stock_change > 0 and sector_change > 0 and sector_fund > 0 and sector_strength >= 70:
        resonance = "强共振"
    elif positive_lifecycle and stock_change > 0 and sector_change > 0 and sector_fund > 0 and sector_strength >= 40:
        resonance = "弱共振"
    elif stock_change > 0 and sector_change < 0:
        resonance = "逆势"
    elif stock_change < 0 and sector_change >= 0:
        resonance = "个股弱于板块"
    elif stock_change < 0 and sector_change < 0:
        resonance = "个股弱于板块" if stock_change < sector_change - 1 else "同向走弱"
    else:
        resonance = "无共振"

    sector_name = str(best.get("name") or "")
    return {
        "available": True,
        "sector_code": best.get("sector_code"),
        "sector_change_pct": sector_change,
        "sector_lifecycle_state": state,
        "sector_lifecycle_label": lifecycle_label,
        "sector_lifecycle_score": _safe_float(best.get("lifecycle_score")),
        "sector_status": f"{sector_name}·{lifecycle_label}" if sector_name else lifecycle_label,
        "sector_resonance": resonance,
        "sector_resonance_score": sector_strength,
        "sector_resonance_name": sector_name,
        "sector_resonance_change": sector_change,
        "sector_resonance_fund": sector_fund,
        "sector_active_days": _safe_int(best.get("active_days")),
        "sector_kline_trend": str(best.get("kline_trend") or ""),
    }


def _plan_result_to_dict(plan_result) -> dict:
    """将 NextDayPlanResult 转为可序列化dict"""
    sr = plan_result.sr
    return {
        "code": plan_result.code,
        "name": plan_result.name,
        "bull_level": plan_result.bull_level,
        "bull_score": plan_result.bull_score,
        "price": plan_result.price,
        "change_pct": plan_result.change_pct,
        # 策略列表(核心)
        "strategies": [
            {
                "strategy_type": s.strategy_type,
                "strategy_label": s.strategy_label,
                "entry_condition": s.entry_condition,
                "entry_price_hint": s.entry_price_hint,
                "position_ratio": s.position_ratio,
                "stop_loss": s.stop_loss,
                "stop_loss_pct": s.stop_loss_pct,
                "target_price": s.target_price,
                "target_price_pct": s.target_price_pct,
                "invalidation": s.invalidation,
                "risk_reward_ratio": s.risk_reward_ratio,
                "confidence": s.confidence,
            }
            for s in plan_result.strategies
        ],
        # 兼容旧字段(strategy/action/support/resistance/invalidation)
        "strategy": plan_result.strategies[0].strategy_type if plan_result.strategies else "watch",
        "action": plan_result.strategies[0].strategy_label if plan_result.strategies else "观望",
        "support": sr.primary_support if sr.primary_support > 0 else None,
        "resistance": sr.primary_resistance if sr.primary_resistance > 0 else None,
        "invalidation": plan_result.strategies[0].invalidation if plan_result.strategies else "",
        # 不买原因
        "avoid_reasons": plan_result.avoid_reasons,
        # 支撑/压力详情
        "support_detail": {
            "primary": sr.primary_support if sr.primary_support > 0 else None,
            "secondary": sr.secondary_support if sr.secondary_support > 0 else None,
            "source": sr.support_source,
        },
        "resistance_detail": {
            "primary": sr.primary_resistance if sr.primary_resistance > 0 else None,
            "secondary": sr.secondary_resistance if sr.secondary_resistance > 0 else None,
            "source": sr.resistance_source,
        },
        # 核心信号+风险
        "top_signals": plan_result.top_signals,
        "risk_warnings": plan_result.risk_warnings,
        # 5日资金
        "fund_5d_direction": plan_result.fund_5d_direction,
        "fund_5d_billion": plan_result.fund_5d_billion,
        # 技术状态
        "tech_summary": plan_result.tech_summary,
        # 板块
        "sector_status": plan_result.sector_status,
        # 板块共振(新增)
        "sector_resonance": plan_result.sector_resonance,
        "sector_resonance_score": plan_result.sector_resonance_score,
        "sector_strength_score": plan_result.sector_resonance_score,
        "sector_resonance_name": plan_result.sector_resonance_name,
        "sector_resonance_change": plan_result.sector_resonance_change,
        "sector_resonance_fund": plan_result.sector_resonance_fund,
        # 情绪面(新增)
        "market_environment": plan_result.market_environment,
        "sentiment_cycle": plan_result.sentiment_cycle,
        # notes(兼容旧前端)
        "notes": plan_result.avoid_reasons + plan_result.risk_warnings[:3],
    }


_NEXT_DAY_ACTIONABLE_STRATEGIES = {
    "main_wave_confirm",
    "trend",
    "trend_pullback_buy",
    "trend_breakout_buy",
    "repair_followup_buy",
    "strong_get_stronger",
    "aggressive",
}
_NEXT_DAY_LOW_ABSORB_STRATEGIES = {
    "main_wave_confirm",
    "trend",
    "trend_pullback_buy",
    "repair_followup_buy",
    "strong_get_stronger",
}
_NEXT_DAY_CHASE_STRATEGIES = {
    "trend_breakout_buy",
    "aggressive",
}
_NEXT_DAY_DIRECT_BUY_BLOCKER_KEYWORDS = (
    "明日不追高",
    "直接买点过高",
    "等回踩",
    "等分歧",
    "性价比不足",
    "赔率不足",
    "风险收益比",
    "贴近20日高点且当日大涨",
    "回测胜率偏低",
    "严选胜率不足",
    "直接买点确认不足",
    "先观察",
)
_NEXT_DAY_SOURCE_SOFT_CAP_RATIO = {
    "low_expectation_trend_watch": 0.20,
    "tenbagger_pullback_watch": 0.20,
    "leader_linkage_follow": 0.20,
    # 收盘后的强板块补涨只是次日盘中检测种子，不能因为同一板块成分股很多
    # 就占据购买预案的大半页面。Top20最多保留2只，盘中扫描池仍保留完整覆盖。
    "sector_core_laggard": 0.10,
    "event_first_board": 0.25,
    "event_high_board": 0.15,
    "second_board_relay": 0.25,
    "high_board_event_lock": 0.20,
    "high_board_theme_turnover": 0.25,
    "high_board_emotion_memory": 0.20,
    "high_board_earnings_surprise": 0.20,
    "high_board_second_wave": 0.20,
    "trend_driver_setup": 0.30,
    "main_wave_pullback_pattern": 0.20,
    "pre_board_momentum_shakeout": 0.25,
    "pre_board_probe_wash": 0.20,
    "pre_board_compression": 0.35,
    "pre_board_long_base": 0.20,
    "pre_board_probe_breakout": 0.05,
    "trend_main_wave_pattern": 0.10,
    "second_wave_pattern": 0.35,
    "second_wave_reset_watch": 0.20,
    "high_board_platform_breakout": 0.15,
    "high_board_lockup_acceleration": 0.15,
    "high_board_turnover_second_wave": 0.25,
    "sector_repair_reversal": 0.15,
    "old_hot_oversold_repair": 0.10,
}
_NEXT_DAY_MAX_DIRECT_BUY_PLANS = 2
_NEXT_DAY_ALLOWED_SECTOR_LIFECYCLES = {"emerging", "accelerating"}
_NEXT_DAY_BLOCKED_SECTOR_LIFECYCLES = {"dormant", "climax", "diverging", "declining", "one_day"}
_SECTOR_LIFECYCLE_LABELS = {
    "dormant": "休眠",
    "emerging": "启动",
    "accelerating": "加速",
    "climax": "高潮",
    "diverging": "分化",
    "declining": "退潮",
    "one_day": "一日游",
}
_NEXT_DAY_SOURCE_EDGE_RANK = {
    "low_expectation_trend_watch": -2,
    "tenbagger_pullback_watch": -1,
    "leader_linkage_follow": -2,
    # 昨日主线不等于今日主线；隔夜补涨观察排在个股形态和事件确认之后。
    "sector_core_laggard": 7,
    "event_first_board": -3,
    "event_high_board": 1,
    "second_board_relay": -3,
    "high_board_event_lock": -4,
    "high_board_theme_turnover": -3,
    "high_board_emotion_memory": -2,
    "high_board_earnings_surprise": -3,
    "high_board_second_wave": -2,
    "sector_repair_reversal": -2,
    "old_hot_oversold_repair": -1,
    "pre_board_momentum_shakeout": -1,
    "pre_board_probe_wash": 0,
    "trend_driver_setup": 0,
    "pre_board_compression": 0,
    "pre_board_long_base": 2,
    "high_board_turnover_second_wave": 1,
    "high_board_lockup_acceleration": 1,
    "main_wave_pattern": 2,
    "second_wave_pattern": 3,
    "second_wave_reset_watch": 2,
    "platform_breakout_pattern": 4,
    "pre_board_probe_breakout": 5,
    "high_board_platform_breakout": 5,
    "trend_main_wave_pattern": 6,
}
_PLAN_PATTERN_SCORE_ADJUST = {
    "low_expectation_trend_watch": 10.0,
    "tenbagger_pullback_watch": 8.0,
    "leader_linkage_follow": 4.0,
    "high_board_event_lock": 10.0,
    "high_board_theme_turnover": 8.0,
    "high_board_emotion_memory": 7.0,
    "high_board_earnings_surprise": 9.0,
    "high_board_second_wave": 7.0,
    "sector_core_laggard": 8.0,
    "pre_board_momentum_shakeout": 10.0,
    "pre_board_probe_wash": 12.0,
    "trend_driver_setup": 6.0,
    "pre_board_compression": 4.0,
    "pre_board_long_base": 0.0,
    "high_board_turnover_second_wave": 0.0,
    "high_board_lockup_acceleration": 4.0,
    "second_wave_pattern": -4.0,
    "second_wave_reset_watch": 0.0,
    "pre_board_probe_breakout": -8.0,
    "trend_main_wave_pattern": -16.0,
}
_DYNAMIC_TREND_POOL_SOURCE_PRIORITY = {
    "low_expectation_trend_watch": 11,
    "sector_core_laggard": 12,
    "tenbagger_pullback_watch": 9,
    "second_board_relay": 11,
    "event_first_board": 10,
    "leader_linkage_follow": 9,
    "main_wave_pullback_pattern": 9,
    "event_high_board": 4,
    "high_board_event_lock": 12,
    "high_board_theme_turnover": 11,
    "high_board_emotion_memory": 10,
    "high_board_earnings_surprise": 11,
    "high_board_second_wave": 10,
    "pre_board_momentum_shakeout": 10,
    "pre_board_probe_wash": 9,
    "sector_repair_reversal": 8,
    "old_hot_oversold_repair": 7,
    "platform_breakout_pattern": 6,
    "second_wave_reset_watch": 6,
    "second_wave_pattern": 5,
    "main_wave_pattern": 5,
    "trend_main_wave_pattern": 5,
    "pre_board_probe_breakout": 4,
    "trend_driver_setup": 3,
    "pre_board_compression": 2,
    "pre_board_long_base": 1,
}

_HIGH_BOARD_TYPE_LABELS = {
    "event_lock": "事件锁价龙",
    "theme_turnover": "题材换手龙",
    "emotion_memory": "情绪记忆龙",
    "earnings_surprise": "业绩预期差龙",
    "second_wave": "断板反包/二波龙",
}
_HIGH_BOARD_TYPE_SOURCE = {
    "event_lock": "high_board_event_lock",
    "theme_turnover": "high_board_theme_turnover",
    "emotion_memory": "high_board_emotion_memory",
    "earnings_surprise": "high_board_earnings_surprise",
    "second_wave": "high_board_second_wave",
}
_HIGH_BOARD_EVENT_KEYWORDS = (
    "控制权", "实控人", "重组", "并购", "收购", "资产注入", "借壳",
    "重大合同", "重大订单", "中标", "战略合作", "股权转让", "要约收购",
)
_HIGH_BOARD_EARNINGS_KEYWORDS = (
    "业绩", "预增", "扭亏", "减亏", "净利润", "扣非", "中报", "半年报",
)
_CAPACITY_TREND_SOURCES = {
    "low_expectation_trend_watch",
    "tenbagger_pullback_watch",
    "trend_driver_setup",
    "trend_main_wave_pattern",
    "main_wave_pattern",
    "main_wave_pullback_pattern",
}


def _board_like_change_threshold(trade_date_value: date | str | None, *, is_st: bool) -> float:
    """前复权涨幅的宽容涨停阈值，复用中央日期规则。"""

    return limit_up_change_threshold(
        "000001",
        "ST样本" if is_st else "样本",
        trade_date_value,
        adjusted_bar=True,
    )


def _is_board_like_change(
    change_pct: float,
    trade_date_value: date | str | None,
    *,
    is_st: bool,
) -> bool:
    return _safe_float(change_pct) >= _board_like_change_threshold(
        trade_date_value,
        is_st=is_st,
    )


def _classify_high_board_candidate(item: dict) -> dict:
    """按驱动而非盈亏状态分类；亏损只作为风险说明，不是候选硬门槛。"""
    stats = dict(item.get("main_wave_stats") or {})
    source = str(item.get("candidate_source") or "")
    board_count = max(
        _safe_int(stats.get("board_count")),
        _safe_int(stats.get("consecutive_days")),
    )
    previous_max_streak = max(
        _safe_int(stats.get("previous_max_board_streak")),
        _safe_int(stats.get("max_board_streak")),
    )
    turnover = _safe_float(stats.get("turnover"), _safe_float(item.get("turnover")))
    volume_ratio = _safe_float(item.get("volume_ratio"), 1.0)
    reason_text = " ".join(
        str(value or "")
        for value in (
            stats.get("limit_up_reason"),
            stats.get("news_title"),
            (item.get("event_catalyst") or {}).get("title") if isinstance(item.get("event_catalyst"), dict) else "",
            " ".join(str(signal) for signal in item.get("top_signals") or []),
        )
    )
    event_grade = str(stats.get("news_event_grade") or "")
    event_score = _safe_float(stats.get("news_catalyst_score"))
    current_board = bool(stats.get("is_current_limit_up")) or board_count > 0 or source in {
        "event_first_board", "event_high_board", "second_board_relay",
    }
    board_attack_ready = current_board or bool(stats.get("board_attack_ready"))
    is_one_word = bool(stats.get("is_one_word"))
    same_reason_count = _safe_int(stats.get("same_reason_limit_up_count"))
    days_since_previous_high = _safe_int(stats.get("days_since_previous_high_board"))

    types: list[str] = []
    reasons: list[str] = []
    has_event_keyword = any(keyword in reason_text for keyword in _HIGH_BOARD_EVENT_KEYWORDS)
    has_earnings_keyword = any(keyword in reason_text for keyword in _HIGH_BOARD_EARNINGS_KEYWORDS)

    if board_attack_ready and (
        has_event_keyword
        or event_grade == "hard"
        or (event_grade == "medium" and event_score >= 58.0)
    ):
        types.append("event_lock")
        reasons.append("控制权/重组/订单等直接事件形成隔夜预期差")

    if board_attack_ready and not is_one_word and 1.5 <= turnover <= 22.0 and (
        board_count >= 2
        or same_reason_count >= 2
        or volume_ratio >= 1.2
    ):
        types.append("theme_turnover")
        reasons.append(
            f"题材梯队与正常换手共振（{board_count or 1}板、换手{turnover:.1f}%）"
        )

    if board_attack_ready and (board_count >= 3 or previous_max_streak >= 3):
        types.append("emotion_memory")
        reasons.append(
            f"历史/当前最高{max(board_count, previous_max_streak)}板，具备市场辨识度记忆"
        )

    if board_attack_ready and has_earnings_keyword:
        types.append("earnings_surprise")
        reasons.append("业绩预增、扭亏、减亏或扣非改善构成预期差；不要求静态盈利")

    is_second_wave_source = source in {
        "second_wave_pattern",
        "second_wave_reset_watch",
        "high_board_turnover_second_wave",
        "high_board_second_wave",
    }
    if is_second_wave_source or (
        board_attack_ready
        and previous_max_streak >= 3
        and board_count <= 2
        and days_since_previous_high >= 3
    ):
        types.append("second_wave")
        reasons.append("前高标断板整理后重新聚合，属于反包/二波路线")

    types = list(dict.fromkeys(types))
    primary_type = next(
        (
            high_board_type
            for high_board_type in (
                "event_lock",
                "earnings_surprise",
                "second_wave",
                "theme_turnover",
                "emotion_memory",
            )
            if high_board_type in types
        ),
        "",
    )
    classified = dict(item)
    classified["high_board_types"] = types
    classified["high_board_type_labels"] = [_HIGH_BOARD_TYPE_LABELS[key] for key in types]
    classified["high_board_primary_type"] = primary_type
    classified["high_board_primary_type_label"] = _HIGH_BOARD_TYPE_LABELS.get(primary_type, "")
    classified["high_board_reasons"] = reasons
    classified["is_high_board_candidate"] = bool(types)
    return classified


def _is_capacity_trend_candidate(item: dict) -> bool:
    """只剔除大市值、低弹性的纯趋势容量票；有高标因果类型时不误杀。"""
    source = str(item.get("candidate_source") or "")
    stats = dict(item.get("main_wave_stats") or {})
    market_cap = max(
        _safe_float(item.get("circ_market_cap_billion")),
        _safe_float(item.get("circ_market_cap")),
        _safe_float(stats.get("circ_market_cap")),
    )
    turnover = _safe_float(item.get("turnover"), _safe_float(stats.get("turnover")))
    board_count = max(
        _safe_int(stats.get("board_count")),
        _safe_int(stats.get("consecutive_days")),
    )
    event_text = " ".join(str(stats.get(key) or "") for key in (
        "limit_up_reason", "news_title",
    ))
    has_structural_hard_event = bool(
        "event_lock" in (item.get("high_board_types") or [])
        and any(keyword in event_text for keyword in _HIGH_BOARD_EVENT_KEYWORDS)
    )
    # 大盘金融/资源等单板即使叠加业绩新闻，主要交易结构仍是容量趋势；
    # 只有控制权、重组、重大订单等结构性硬事件可以豁免。
    if (
        market_cap >= 300.0
        and board_count <= 1
        and turnover <= 8.0
        and not has_structural_hard_event
    ):
        return True
    if item.get("is_high_board_candidate") or item.get("high_board_types"):
        return False
    if source not in _CAPACITY_TREND_SOURCES:
        return False
    return market_cap >= 180.0 and turnover <= 8.0


def _select_dynamic_trend_pool_candidates(candidates: list[dict], limit: int = 100) -> list[dict]:
    """按形态保留运行池名额，避免同分趋势候选挤掉试盘突破。"""
    source_reservations = {
        "low_expectation_trend_watch": 16,
        "sector_core_laggard": 24,
        "tenbagger_pullback_watch": 12,
        "second_board_relay": 12,
        "event_first_board": 12,
        "event_high_board": 6,
        "high_board_event_lock": 12,
        "high_board_theme_turnover": 16,
        "high_board_emotion_memory": 12,
        "high_board_earnings_surprise": 12,
        "high_board_second_wave": 12,
        "leader_linkage_follow": 12,
        "main_wave_pullback_pattern": 15,
        "pre_board_momentum_shakeout": 18,
        "pre_board_probe_wash": 15,
        "trend_driver_setup": 35,
        "pre_board_probe_breakout": 12,
        "pre_board_long_base": 8,
        "pre_board_compression": 8,
        "platform_breakout_pattern": 12,
        "second_wave_reset_watch": 10,
        "second_wave_pattern": 8,
        "main_wave_pattern": 20,
        "trend_main_wave_pattern": 12,
        "sector_repair_reversal": 10,
        "old_hot_oversold_repair": 7,
    }

    def _rank(item: dict) -> tuple[float, float, str]:
        return (
            _safe_float(item.get("main_wave_score")),
            _safe_float(item.get("total_score")),
            str(item.get("code") or ""),
        )

    buckets: dict[str, list[dict]] = {source: [] for source in source_reservations}
    for item in candidates:
        source = str(item.get("dynamic_pool_source") or item.get("candidate_source") or "")
        if source in buckets and item.get("code"):
            normalized = dict(item)
            if item.get("dynamic_pool_source"):
                normalized["candidate_source"] = source
                normalized["candidate_source_label"] = item.get("dynamic_pool_label") or item.get("candidate_source_label")
                normalized["main_wave_score"] = item.get("dynamic_pool_score") or item.get("main_wave_score")
                normalized["main_wave_stats"] = item.get("dynamic_pool_stats") or item.get("main_wave_stats") or {}
            buckets[source].append(normalized)
    for rows in buckets.values():
        rows.sort(key=_rank, reverse=True)

    selected: list[dict] = []
    seen_codes: set[str] = set()
    for source, reservation in source_reservations.items():
        for item in buckets[source][:reservation]:
            code = str(item.get("code") or "")
            if code in seen_codes:
                continue
            selected.append(item)
            seen_codes.add(code)

    if len(selected) < limit:
        remaining = sorted(
            (
                item
                for rows in buckets.values()
                for item in rows
                if str(item.get("code") or "") not in seen_codes
            ),
            key=lambda item: (
                _DYNAMIC_TREND_POOL_SOURCE_PRIORITY.get(
                    str(item.get("candidate_source") or ""),
                    0,
                ),
                *_rank(item),
            ),
            reverse=True,
        )
        for item in remaining:
            if len(selected) >= limit:
                break
            code = str(item.get("code") or "")
            if code in seen_codes:
                continue
            selected.append(item)
            seen_codes.add(code)

    selected.sort(
        key=lambda item: (
            _DYNAMIC_TREND_POOL_SOURCE_PRIORITY.get(
                str(item.get("candidate_source") or ""),
                0,
            ),
            *_rank(item),
        ),
        reverse=True,
    )
    return selected[:limit]


def _next_day_strategy_types(plan: dict) -> list[str]:
    return [str(s.get("strategy_type") or "") for s in plan.get("strategies") or []]


def _market_allows_conditional_buy(plan: dict) -> bool:
    """极强窗口控制标准仓位；普通牛市仍可保留轻仓条件预案。"""
    market_breadth = plan.get("market_breadth") or {}
    return bool(
        market_breadth.get("direct_buy_ok")
        or str(market_breadth.get("market_regime") or "") == "bull"
    )


def _conditional_position_ratio(
    plan: dict,
    *,
    hot_position: str,
    bull_position: str,
) -> str:
    """返回盘中条件触发后的计划仓位；触发前仓位始终由信号字段保持为0。"""
    if plan.get("is_tradeable") is False or plan.get("research_only"):
        return "0"
    market_breadth = plan.get("market_breadth") or {}
    if market_breadth.get("direct_buy_ok"):
        return hot_position
    if str(market_breadth.get("market_regime") or "") == "bull":
        return bull_position
    return "0"


def _has_next_day_direct_buy_blocker(plan: dict) -> bool:
    texts: list[str] = []
    texts.extend(str(item or "") for item in plan.get("avoid_reasons") or [])
    texts.extend(str(item or "") for item in plan.get("risk_warnings") or [])
    for strategy in plan.get("strategies") or []:
        texts.append(str(strategy.get("invalidation") or ""))
    return any(
        keyword in text
        for text in texts
        for keyword in _NEXT_DAY_DIRECT_BUY_BLOCKER_KEYWORDS
    )


def _next_day_plan_action_priority(plan: dict) -> int:
    """越小越适合放在明日可执行预案前面。"""
    if plan.get("is_tradeable") is False:
        # 创业板/科创板等事件票仍需在“事件”页可见，但始终是零仓位观察。
        if str(plan.get("candidate_source") or "") in {
            "event_first_board",
            "event_high_board",
            "second_board_relay",
            *set(_HIGH_BOARD_TYPE_SOURCE.values()),
        }:
            return 4
        return 6
    types = set(_next_day_strategy_types(plan))
    if types.intersection(_NEXT_DAY_ACTIONABLE_STRATEGIES):
        if not _market_allows_conditional_buy(plan):
            return 4
        if _has_next_day_direct_buy_blocker(plan):
            return 4
    # 默认执行顺序改成低吸优先：强趋势票也必须等待回踩/支撑回收；
    # 放量突破和竞价急拉只保留条件观察，不能排在低吸候选前面。
    if types.intersection(_NEXT_DAY_CHASE_STRATEGIES):
        return 3
    # A2强修复已在盘中完成首次确认，预案本身只是一条二次确认条件；
    # 先纳入Top2预算，再由超额降级，避免页面仍显示大量“可买”动作。
    if "repair_followup_buy" in types:
        return 2
    if "main_wave_confirm" in types:
        return 0
    if "strong_get_stronger" in types:
        return 1
    if types.intersection({
        "trend",
        "trend_pullback_buy",
        "trend_breakout_buy",
    }):
        return 2
    if "aggressive" in types:
        return 3
    if types.intersection({
        "watch",
        "event_relay_watch",
        "high_board_watch",
        "leader_linkage_watch",
        "sector_core_laggard_watch",
    }):
        return 4
    return 5


def _next_day_plan_sort_key(plan: dict) -> tuple:
    return (
        _next_day_plan_action_priority(plan),
        0 if plan.get("is_high_board_candidate") else 1,
        1 if plan.get("research_only") else 0,
        -_safe_float(plan.get("plan_priority_score")),
        _NEXT_DAY_SOURCE_EDGE_RANK.get(str(plan.get("candidate_source") or ""), 9),
        -_safe_float(plan.get("bull_score")),
        -_safe_float(plan.get("main_wave_score")),
        str(plan.get("code") or ""),
    )


def _apply_next_day_prediction_quality(plan: dict) -> dict:
    """统一预案排序口径，避免未校准的形态原始分和泛板块标签主导Top20。"""
    updated = dict(plan)
    source = str(updated.get("candidate_source") or "")
    stats = updated.get("main_wave_stats") or {}
    score = min(_safe_float(updated.get("bull_score")), 100.0) * 0.28
    score += min(_safe_float(updated.get("main_wave_score")), 100.0) * 0.32
    reasons: list[str] = []

    source_bonus = {
        "low_expectation_trend_watch": 12.0,
        "tenbagger_pullback_watch": 8.0,
        "event_first_board": 12.0,
        "high_board_event_lock": 14.0,
        "high_board_theme_turnover": 12.0,
        "high_board_emotion_memory": 11.0,
        "high_board_earnings_surprise": 13.0,
        "high_board_second_wave": 11.0,
        "leader_linkage_follow": 9.0,
        "main_wave_pullback_pattern": 10.0,
        "pre_board_momentum_shakeout": 10.0,
        "pre_board_probe_wash": 9.0,
        "pre_board_compression": 7.0,
        "trend_driver_setup": 7.0,
        "main_wave_pattern": 8.0,
        "trend_main_wave_pattern": 5.0,
        "second_wave_pattern": 4.0,
        "second_wave_reset_watch": 4.0,
        "sector_repair_reversal": 7.0,
        "old_hot_oversold_repair": 5.0,
    }.get(source, 0.0)
    score += source_bonus
    if source_bonus:
        reasons.append("形态来源已校准")

    # 形态相似只能决定“进入检测池”，不能独自证明次日会有资金接力。
    # 对独立/逆势且处于休眠板块的股票，除非存在可追溯的直接硬消息，
    # 必须降低页面关注级别；否则历史形态高分会把每日真实轮动方向挤出Top20。
    event_catalyst = updated.get("event_catalyst") or {}
    event_grade = str(event_catalyst.get("grade") or "").strip().lower()
    event_has_identity = bool(
        str(event_catalyst.get("type") or "").strip()
        or str(event_catalyst.get("title") or "").strip()
    )
    news_event_grade = str(stats.get("news_event_grade") or "").strip().lower()
    has_hard_direct_catalyst = bool(
        (event_grade == "hard" and event_has_identity)
        or news_event_grade == "hard"
    )
    has_medium_direct_catalyst = bool(news_event_grade == "medium")
    if has_hard_direct_catalyst:
        score += 10.0 if bool(event_catalyst.get("fresh_after_close")) else 6.0
        reasons.append("直接硬消息可追溯")
    elif has_medium_direct_catalyst:
        score += 3.0
        reasons.append("直接事件待盘中验证")

    if source == "sector_core_laggard":
        # 该通道用于发现“今日主线里尚未启动的成员”，历史上最容易把单日高潮
        # 机械外推到次日。候选仍进入分钟级扫描池，但盘前关注分不得与独立个股
        # 形态同权，更不能因昨日板块强度直接获得A级。
        score -= 6.0
        reasons.append("隔夜板块补涨待当日重验")
        if bool(stats.get("breadth_climax_risk")):
            score -= 10.0
            reasons.append("单日涨停扩散过热防兑现")
        if bool(stats.get("one_day_reversal_burst")):
            score -= 8.0
            reasons.append("单日反转脉冲未证明持续")

    change_pct = _safe_float(updated.get("change_pct"))
    if -2.0 <= change_pct <= 4.5:
        score += 6.0
        reasons.append("收盘位置未过热")
    elif change_pct > 6.5:
        score -= 10.0
        reasons.append("当日涨幅偏高")
    elif change_pct < -4.0:
        score -= 8.0
        reasons.append("收盘仍偏弱")

    strategy_types = set(_next_day_strategy_types(updated))
    if strategy_types.intersection(_NEXT_DAY_LOW_ABSORB_STRATEGIES):
        score += 6.0
        reasons.append("回踩/支撑回收优先")
    if strategy_types.intersection(_NEXT_DAY_CHASE_STRATEGIES):
        score -= 7.0
        reasons.append("突破追价仅作备选")

    causal_sector = bool(updated.get("sector_driver_causal"))
    resonance = str(updated.get("sector_resonance") or "")
    lifecycle = str(updated.get("sector_lifecycle_state") or "")
    if causal_sector and resonance == "强共振" and lifecycle in _NEXT_DAY_ALLOWED_SECTOR_LIFECYCLES:
        score += 10.0
        reasons.append("产业主驱动强共振")
    elif causal_sector and resonance == "弱共振":
        score += 5.0
        reasons.append("产业板块弱共振")
    elif source in {
        "leader_linkage_follow",
        "sector_repair_reversal",
        "old_hot_oversold_repair",
    }:
        score -= 12.0
        reasons.append("缺少可验证产业主驱动")
    if (
        not causal_sector
        and resonance in {"独立行情", "逆势"}
        and not (has_hard_direct_catalyst or has_medium_direct_catalyst)
    ):
        score -= 12.0
        reasons.append("独立形态缺少当日资金方向")
    if lifecycle == "dormant" and not has_hard_direct_catalyst:
        score -= 8.0
        reasons.append("所属主驱动仍处休眠期")
    sector_change = _safe_float(updated.get("sector_resonance_change"))
    if (
        sector_change <= -1.5
        and resonance != "强共振"
        and not has_hard_direct_catalyst
    ):
        score -= 5.0
        reasons.append("所属方向当日走弱")

    setup_state = str(stats.get("setup_state") or "")
    if setup_state in {
        "armed_sector_core_laggard",
        "armed_pullback",
        "armed_breakout",
        "armed_main_wave_pullback",
        "armed_main_wave_shape_pullback",
    }:
        score += 4.0
    long_cycle_regime = str(stats.get("long_cycle_regime") or "")
    long_cycle_quality_score = _safe_float(stats.get("quality_setup_score"))
    if bool(stats.get("shape_ready")) and long_cycle_regime != "high_overheat":
        score += min(max(long_cycle_quality_score - 50.0, 0.0) * 0.20, 8.0)
        reasons.append(str(stats.get("long_cycle_regime_label") or "长周期准备态"))
    elif long_cycle_regime == "high_overheat":
        score -= 12.0
        reasons.append("250日位置与中短期涨幅过热")
    position_120 = _safe_float(stats.get("position_120"), _safe_float(stats.get("pos_120"), -1.0))
    if 0 <= position_120 <= 0.42:
        score += 4.0
        reasons.append("120日位置较低")
    dry_up_ratio = _safe_float(stats.get("dry_up_ratio"), -1.0)
    if 0 < dry_up_ratio <= 0.80:
        score += 4.0
        reasons.append("成交缩量沉淀")
    max_drawdown_20 = _safe_float(stats.get("max_drawdown_20"))
    if max_drawdown_20 > 20.0 and long_cycle_regime not in {
        "historical_board_reset",
        "oversold_reset",
    }:
        score -= 5.0
        reasons.append("近20日回撤偏大")

    if updated.get("is_tradeable") is False:
        score = 0.0
    score = round(max(0.0, min(score, 100.0)), 1)
    updated["plan_priority_score"] = score
    updated["plan_priority_tier"] = "A" if score >= 80 else "B" if score >= 68 else "C"
    updated["plan_priority_reasons"] = reasons[:4]
    return updated


def _is_second_wave_direct_buy_ready(stats: dict) -> bool:
    """二波形态只在回踩修复后仍有承接时进入直接买入层。"""
    break_down_abs = abs(_safe_float(stats.get("break_down_pct")))
    reclaim_peak_ratio = _safe_float(stats.get("reclaim_peak_ratio"))
    near_recent_high_ratio = _safe_float(stats.get("near_recent_high_ratio"))
    post_up_days = _safe_int(stats.get("post_up_days"))
    days_after_break = _safe_int(stats.get("days_after_break"))
    vol_expansion = _safe_float(stats.get("vol_expansion"))

    return (
        6 <= days_after_break <= 35
        and 12 <= break_down_abs <= 24
        and 0.84 <= reclaim_peak_ratio <= 0.94
        and near_recent_high_ratio >= 0.94
        and post_up_days >= 4
        and vol_expansion >= 1.5
    )


def _is_pre_board_compression_direct_buy_ready(plan: dict) -> bool:
    """连板前压缩直接买点必须叠加市场宽度，弱市只观察不直接买。"""
    stats = plan.get("main_wave_stats") or {}
    market_breadth = plan.get("market_breadth") or {}
    pattern_score = _safe_float(plan.get("main_wave_score"))
    change_pct = _safe_float(plan.get("change_pct"))
    platform_range_pct = _safe_float(stats.get("platform_range_pct"), 100.0)
    range_20_pct = _safe_float(stats.get("range_20_pct"), 100.0)
    pos_60 = _safe_float(stats.get("pos_60"), 1.0)
    pct_10 = _safe_float(stats.get("pct_10"))
    pct_20 = _safe_float(stats.get("pct_20"))
    dry_up_ratio = _safe_float(stats.get("dry_up_ratio"), 1.0)
    vol_today_ratio = _safe_float(stats.get("vol_today_ratio"), 1.0)
    board_like_count_20 = _safe_int(stats.get("board_like_count_20"))
    market_direct_ok = bool(market_breadth.get("direct_buy_ok"))
    market_up_ratio = _safe_float(market_breadth.get("up_ratio"))
    market_avg_change = _safe_float(market_breadth.get("avg_change"))
    market_index_ret5 = _safe_float(market_breadth.get("index_ret5"))
    market_index_ret20 = _safe_float(market_breadth.get("index_ret20"))

    return (
        market_direct_ok
        and pattern_score >= 82.0
        and market_up_ratio >= _MARKET_DIRECT_UP_RATIO
        and market_avg_change >= _MARKET_DIRECT_AVG_CHANGE
        and market_index_ret5 >= _MARKET_DIRECT_RET5
        and _MARKET_DIRECT_RET20_MIN <= market_index_ret20 <= _MARKET_DIRECT_RET20_MAX
        and -2.0 <= change_pct <= 4.5
        and (platform_range_pct <= 15.0 or range_20_pct <= 12.0)
        and 0.35 <= pos_60 <= 0.65
        and pct_10 <= 10.0
        and pct_20 <= 15.0
        and board_like_count_20 == 0
        and (dry_up_ratio <= 0.75 or vol_today_ratio <= 0.80)
    )


def _is_high_board_turnover_direct_buy_ready(plan: dict) -> bool:
    """高标换手二波只保留洗盘充分、修复不过热、量能重新承接的形态。"""
    stats = plan.get("main_wave_stats") or {}
    change_pct = _safe_float(plan.get("change_pct"))
    break_down_pct = _safe_float(stats.get("break_down_pct"))
    break_down_abs = abs(break_down_pct)
    reclaim_peak_ratio = _safe_float(stats.get("reclaim_peak_ratio"))
    vol_expansion = _safe_float(stats.get("vol_expansion"))
    max_board_streak = _safe_int(stats.get("max_board_streak"))
    board_like_count_20 = _safe_int(stats.get("board_like_count_20"))

    return (
        -1.5 <= change_pct <= 2.5
        and max_board_streak >= 2
        and board_like_count_20 >= 2
        and 12.0 <= break_down_abs <= 20.0
        and 0.84 <= reclaim_peak_ratio <= 0.94
        and vol_expansion >= 1.6
    )


def _backtested_low_edge_reason(plan: dict) -> str:
    source = str(plan.get("candidate_source") or "")
    stats = plan.get("main_wave_stats") or {}
    strategy_types = set(_next_day_strategy_types(plan))
    if not source and strategy_types.intersection({"trend", "aggressive"}):
        return "普通强势排行未匹配高胜率K线形态，先观察支撑承接"
    if source == "trend_driver_setup":
        return "趋势驱动候选已入池，等待盘中支撑回收或放量突破确认"
    if source == "pre_board_momentum_shakeout":
        return "中期动量后的缩量洗盘只定义启动预测；次日必须等支撑回收、滚动60秒增量成交与板块/资金确认"
    if source == "pre_board_probe_wash":
        return "试盘缩量洗盘候选已入池，只有盘中支撑回收或放量突破后才形成买点"
    if source == "main_wave_pattern":
        return "普通主升形态回测边际不足，先观察贴线确认"
    if source == "trend_main_wave_pattern":
        return "趋势主升加速回测胜率偏低，先观察回踩确认"
    if source == "pre_board_probe_breakout":
        return "试盘突破样本不稳定，先观察题材发酵和盘口主动性"
    if source == "pre_board_long_base":
        return "120日低位底座已升级为250日背景分层，只进入连板观察池；等待滚动60秒增量成交、VWAP及产业/盘口确认"
    if source == "platform_breakout_pattern":
        return "普通平台突破回测胜率偏低，先观察突破后承接"
    if source == "pre_board_compression":
        return "压缩蓄势只定义盘前准备态；未出现滚动60秒增量成交、VWAP与产业/盘口确认前保持零仓位"
    if source == "high_board_turnover_second_wave":
        return "高标换手二波回测胜率低于压缩蓄势，先观察承接"
    if source == "high_board_lockup_acceleration":
        return "高标缩量锁筹回测胜率偏低，先观察承接"
    if source == "high_board_platform_breakout":
        return "高标平台突破波动较大，先观察题材合力"
    if source == "second_wave_pattern":
        return "普通二波直接买点样本不足，先观察回踩不破和量能承接"
    if source == "second_wave_reset_watch":
        return "高标下杀仅进入二波检测池，等待盘中止跌急拉与盘口共振"
    return ""


def _downgrade_backtested_low_edge_plan(plan: dict) -> dict:
    """把回测表现不稳定的形态从“购买预案”降到观察，避免候选池伪装成买点。"""
    if plan.get("is_tradeable") is False:
        return plan
    if not set(_next_day_strategy_types(plan)).intersection(_NEXT_DAY_ACTIONABLE_STRATEGIES):
        return plan

    reason = _backtested_low_edge_reason(plan)
    if not reason:
        return plan

    source = str(plan.get("candidate_source") or "")
    is_second_wave_reset = source == "second_wave_reset_watch"
    stats = plan.get("main_wave_stats") or {}
    reset_support = _safe_float(stats.get("support"))
    strategy_label = "⏱️ 二波检测中" if is_second_wave_reset else "👁️ 观察确认"
    entry_condition = (
        "等待盘中止跌急拉、放量，并由盘口/资金/板块至少双确认"
        if is_second_wave_reset
        else "候选形态成立，但回测后不作为明日直接买点"
    )
    entry_price_hint = "未触发前不买" if is_second_wave_reset else "--"
    invalidation = reason
    if is_second_wave_reset:
        invalidation = (
            f"跌破回撤低点{reset_support:.2f}或急拉无量、无承接，二波检测失效"
            if reset_support > 0
            else "继续破位或急拉无量、无承接，二波检测失效"
        )

    downgraded = dict(plan)
    downgraded["strategies"] = [{
        "strategy_type": "watch",
        "strategy_label": strategy_label,
        "entry_condition": entry_condition,
        "entry_price_hint": entry_price_hint,
        "position_ratio": "0",
        "stop_loss": 0,
        "stop_loss_pct": 0,
        "target_price": 0,
        "target_price_pct": 0,
        "invalidation": invalidation,
        "risk_reward_ratio": 0,
        "confidence": "低",
    }]
    downgraded["strategy"] = "watch"
    downgraded["action"] = strategy_label
    downgraded["invalidation"] = invalidation
    if is_second_wave_reset:
        downgraded["monitor_state"] = "armed_second_wave_reset"
        downgraded["monitor_state_label"] = "二波检测中"
        downgraded["intraday_trigger"] = entry_condition
    for key in ("avoid_reasons", "risk_warnings", "notes"):
        values = list(downgraded.get(key) or [])
        values.append(reason)
        downgraded[key] = list(dict.fromkeys(values))
    return downgraded


def _downgrade_next_day_plan_to_watch(
    plan: dict,
    reason: str,
    *,
    entry_condition: str = "候选形态保留，等待风险条件解除后重新计算买点",
) -> dict:
    """统一把不可执行的条件买点降为零仓位观察，页面、模拟盘与排序共用。"""
    downgraded = dict(plan)
    downgraded["strategies"] = [{
        "strategy_type": "watch",
        "strategy_label": "👁️ 观察确认",
        "entry_condition": entry_condition,
        "entry_price_hint": "--",
        "position_ratio": "0",
        "stop_loss": 0,
        "stop_loss_pct": 0,
        "target_price": 0,
        "target_price_pct": 0,
        "invalidation": reason,
        "risk_reward_ratio": 0,
        "confidence": "低",
    }]
    downgraded["strategy"] = "watch"
    downgraded["action"] = "👁️ 观察确认"
    downgraded["invalidation"] = reason
    downgraded.pop("buy_signal", None)
    for key in ("avoid_reasons", "risk_warnings", "notes"):
        values = list(downgraded.get(key) or [])
        values.append(reason)
        downgraded[key] = list(dict.fromkeys(values))
    return downgraded


def _apply_event_limit_up_protocol(plan: dict) -> dict:
    """事件涨停只生成次日接力观察；竞价与开盘换手确认前仓位始终为零。"""
    source = str(plan.get("candidate_source") or "")
    if source not in {"event_first_board", "event_high_board", "second_board_relay"}:
        return plan

    stats = dict(plan.get("main_wave_stats") or {})
    board_count = max(1, _safe_int(stats.get("board_count"), 1))
    is_second_board_relay = source == "second_board_relay"
    event_grade = str(stats.get("news_event_grade") or ("relay" if is_second_board_relay else ""))
    event_score = _safe_float(
        stats.get("news_catalyst_score"),
        _safe_float(stats.get("relay_quality_score")),
    )
    limit_up_time = str(stats.get("limit_up_time") or "")
    break_count = _safe_int(stats.get("break_count"))
    turnover = _safe_float(stats.get("turnover"))
    is_one_word = bool(stats.get("is_one_word"))
    fresh_after_close = bool(stats.get("news_fresh_after_trade_close"))
    same_reason_limit_up_count = _safe_int(stats.get("same_reason_limit_up_count"))
    return_20d = _safe_float(stats.get("return_20d"))
    board_like_count_20 = _safe_int(stats.get("board_like_count_20"))
    lifecycle_state = str(plan.get("sector_lifecycle_state") or "")
    sector_resonance = str(plan.get("sector_resonance") or "")

    blockers: list[str] = []
    if plan.get("is_tradeable") is False:
        blockers.append(str(plan.get("tag") or "观察板块不可执行"))
    if is_one_word:
        blockers.append("一字板没有正常换手，不排队追单")
    if break_count > 3:
        blockers.append(f"当日炸板{break_count}次，封板分歧过大")
    if turnover <= 0 or turnover > 22.0:
        blockers.append(f"换手率{turnover:.1f}%不在接力观察区间")
    if not is_second_board_relay and (event_grade not in {"hard", "medium"} or event_score < 58.0):
        blockers.append("消息硬度未达到事件接力阈值")
    if return_20d > 35.0 or board_like_count_20 >= 3:
        blockers.append(
            f"近20日涨幅{return_20d:.1f}%/强板{board_like_count_20}次，属于高位博弈而非低位事件首板"
        )
    if source in {"event_first_board", "second_board_relay"}:
        if limit_up_time and limit_up_time > "10:30:00":
            blockers.append(f"首板封板时间{limit_up_time}偏晚")
        if turnover < 2.0:
            blockers.append(f"换手率{turnover:.1f}%不足，筹码未经市场确认")
        has_board_cohort = same_reason_limit_up_count >= 3
        has_sector_cohort = bool(
            lifecycle_state in _NEXT_DAY_ALLOWED_SECTOR_LIFECYCLES
            and sector_resonance in {"强共振", "弱共振"}
        )
        has_independent_hard_event = bool(
            (event_grade == "hard" and event_score >= 68.0)
            or (is_second_board_relay and bool(stats.get("relay_pool_ready")))
        )
        if not (has_board_cohort or has_sector_cohort or has_independent_hard_event):
            blockers.append(
                f"同类涨停仅{same_reason_limit_up_count}家且板块未启动，不能只靠单股消息追板"
            )
    else:
        if board_count >= 4:
            blockers.append(f"已连续{board_count}板，进入高位加速风险区")
        persistent_hard_event = bool(
            event_grade == "hard"
            and event_score >= 68.0
            and board_count <= 3
            and break_count <= 3
        )
        if not (fresh_after_close or persistent_hard_event):
            blockers.append("高标缺少仍在有效窗口内的硬事件，不按旧消息继续追板")

    relay_ready = not blockers
    if source in {"event_first_board", "second_board_relay"}:
        label = (
            "🟡 首板冲二板确认"
            if is_second_board_relay
            else "🟣 重大利好首板接力"
            if event_grade == "hard"
            else "🟣 事件首板接力"
        )
        entry_condition = (
            "次日竞价涨幅0.5%~3.5%、竞价量能放大且同题材至少保留前排；"
            "开盘后5~10分钟不破前日涨停价/VWAP，换手放量后再次向上才触发提醒"
        )
        invalidation = (
            "竞价低于-1.5%或高于5%、开盘跌破前日涨停价后不能收回、"
            "板块前排转弱、首次换手回封失败均取消"
        )
    else:
        label = "🟠 高标新增事件续强" if fresh_after_close else "⛔ 高标不追"
        entry_condition = (
            "仅限收盘后新增硬利好；次日竞价1%~4%、高标分歧转一致并完成换手回封后再提示，禁止竞价直接排板"
        )
        invalidation = "无新增硬消息、竞价高开>5%、一字加速、放量炸板或板块梯队退潮即取消"

    if blockers:
        entry_condition = "当前仅保留事件观察；" + "；".join(dict.fromkeys(blockers))
    planned_position = (
        _conditional_position_ratio(
            plan,
            hot_position="1/6仓",
            bull_position="1/8仓",
        )
        if relay_ready
        else "0"
    )
    strategy = {
        "strategy_type": "event_relay_watch",
        "strategy_label": label if relay_ready else "👁️ 事件观察",
        "entry_condition": entry_condition,
        "entry_price_hint": "等待次日竞价/换手确认" if relay_ready else "不追板",
        "position_ratio": planned_position,
        "position_before_trigger": 0,
        "position_after_trigger": planned_position,
        "stop_loss": 0,
        "stop_loss_pct": 0,
        "target_price": 0,
        "target_price_pct": 0,
        "invalidation": invalidation,
        "risk_reward_ratio": 0,
        "confidence": "中" if relay_ready and event_grade == "hard" else "低",
    }
    updated = dict(plan)
    updated["strategies"] = [strategy]
    updated["strategy"] = "event_relay_watch"
    updated["action"] = strategy["strategy_label"]
    updated["invalidation"] = invalidation
    updated["event_catalyst"] = {
        "grade": event_grade,
        "score": round(event_score, 2),
        "type": str(stats.get("news_event_type") or ""),
        "title": str(stats.get("news_title") or ""),
        "source": str(stats.get("news_source") or ""),
        "publish_time": str(stats.get("news_publish_time") or ""),
        "fresh_after_close": fresh_after_close,
        "relay_ready": relay_ready,
        "blockers": list(dict.fromkeys(blockers)),
    }
    updated["monitor_state"] = "armed_event_relay" if relay_ready else "event_watch_only"
    if relay_ready:
        updated["buy_signal"] = {
            "type": "second_board_relay_confirmation" if is_second_board_relay else "event_relay_confirmation",
            "label": "冲二板二次确认" if is_second_board_relay else "事件接力二次确认",
            "status": "waiting_auction_and_turnover_confirmation",
            "position_before_trigger": 0,
            "position_after_trigger": planned_position,
            "auction_change_min": 0.5 if source in {"event_first_board", "second_board_relay"} else 1.0,
            "auction_change_max": 3.5 if source in {"event_first_board", "second_board_relay"} else 4.0,
            "requires_open_turnover_confirmation": True,
            "requires_sector_front_row": True,
        }
    else:
        updated.pop("buy_signal", None)
    note = "事件涨停不等于可追；页面只做次日条件预案，真正买点须由竞价、换手回封和板块梯队再次确认"
    for key in ("risk_warnings", "notes"):
        values = list(updated.get(key) or [])
        values.extend([note] + blockers)
        updated[key] = list(dict.fromkeys(values))
    return updated


def _apply_leader_linkage_protocol(plan: dict) -> dict:
    """看A做B只生成零仓位预案；A稳定和B量价确认前禁止执行。"""
    if str(plan.get("candidate_source") or "") != "leader_linkage_follow":
        return plan
    stats = dict(plan.get("main_wave_stats") or {})
    leader_code = str(stats.get("leader_code") or "")
    leader_name = str(stats.get("leader_name") or leader_code)
    link_sector = str(stats.get("link_sector_name") or "关联板块")
    linkage_score = _safe_float(stats.get("linkage_score"))
    business_relevance_score = _safe_float(stats.get("business_relevance_score"))
    follower_shape_score = _safe_float(stats.get("follower_shape_score"))
    theme_alignment_score = _safe_float(stats.get("theme_alignment_score"))
    leader_score = _safe_float(stats.get("leader_recognition_score"))
    return_20d = _safe_float(stats.get("return_20d"))
    blockers: list[str] = []
    if not leader_code:
        blockers.append("缺少明确龙头A")
    if leader_score < 68:
        blockers.append(f"龙头辨识度{leader_score:.0f}不足")
    if linkage_score < 72:
        blockers.append(f"A/B联动评分{linkage_score:.0f}不足")
    if business_relevance_score < 76:
        blockers.append(f"主营业务相关性{business_relevance_score:.0f}不足")
    if follower_shape_score < 68:
        blockers.append(f"B自身形态{follower_shape_score:.0f}偏弱")
    if theme_alignment_score < 75:
        blockers.append(f"龙头当日归因与联动主题匹配度{theme_alignment_score:.0f}不足")
    if return_20d > 25:
        blockers.append(f"B近20日已上涨{return_20d:.1f}%，不属于低位补涨")
    if plan.get("is_tradeable") is False:
        blockers.append(str(plan.get("tag") or "观察板块不可执行"))

    relay_ready = not blockers
    entry_condition = (
        f"观察{leader_name}({leader_code})先稳定封板或维持前排强势；"
        "B涨幅-1.5%~5.5%、量比1.15~3.0，站回VWAP并突破5分钟高点，"
        "盘口或实时资金确认后触发A2提醒"
    )
    invalidation = (
        f"{leader_name}炸板后不能快速回封/跌离日内高位、{link_sector}上涨家数收缩、"
        "B跌破VWAP或涨幅超过6%才出现信号均取消"
    )
    if blockers:
        entry_condition = "当前仅观察；" + "；".join(blockers)
    strategy = {
        "strategy_type": "leader_linkage_watch",
        "strategy_label": f"🔗 看{leader_name}做B" if relay_ready else "👁️ A/B联动观察",
        "entry_condition": entry_condition,
        "entry_price_hint": "等待A稳定+B转强" if relay_ready else "不参与",
        "position_ratio": "0",
        "stop_loss": 0,
        "stop_loss_pct": 0,
        "target_price": 0,
        "target_price_pct": 0,
        "invalidation": invalidation,
        "risk_reward_ratio": 0,
        "confidence": "中" if relay_ready else "低",
    }
    updated = dict(plan)
    updated["strategies"] = [strategy]
    updated["strategy"] = "leader_linkage_watch"
    updated["action"] = strategy["strategy_label"]
    updated["invalidation"] = invalidation
    updated["leader_linkage"] = {
        "leader_code": leader_code,
        "leader_name": leader_name,
        "sector_name": link_sector,
        "linkage_score": round(linkage_score, 1),
        "business_relevance_score": round(business_relevance_score, 1),
        "follower_shape_score": round(follower_shape_score, 1),
        "theme_alignment_score": round(theme_alignment_score, 1),
        "leader_driver_reason": str(stats.get("leader_driver_reason") or ""),
        "leader_industry": str(stats.get("leader_industry") or ""),
        "follower_industry": str(stats.get("follower_industry") or ""),
        "leader_recognition_score": round(leader_score, 1),
        "leader_type": str(stats.get("leader_type") or ""),
        "relay_ready": relay_ready,
        "blockers": blockers,
    }
    updated["monitor_state"] = "armed_leader_linkage" if relay_ready else "leader_linkage_watch_only"
    if relay_ready:
        updated["buy_signal"] = {
            "type": "leader_linkage_confirmation",
            "label": "龙头映射补涨二次确认",
            "status": "waiting_leader_and_follower_confirmation",
            "position_before_trigger": 0,
            "requires_leader_stable": True,
            "requires_vwap_reclaim": True,
        }
    else:
        updated.pop("buy_signal", None)
    note = "看A做B不是看到龙头上涨就买跟风；A稳定与B量价确认缺一不可"
    for key in ("risk_warnings", "notes"):
        values = list(updated.get(key) or [])
        values.extend([note] + blockers)
        updated[key] = list(dict.fromkeys(values))
    return updated


def _apply_sector_core_laggard_protocol(plan: dict) -> dict:
    """昨日强主线只入零仓位检测池，次日按当前板块和量价重新确认。"""
    if str(plan.get("candidate_source") or "") != "sector_core_laggard":
        return plan
    stats = dict(plan.get("main_wave_stats") or {})
    sector_name = str(stats.get("sector_name") or "主线板块")
    sector_strength = _safe_float(stats.get("sector_strength_score"))
    sector_change = _safe_float(stats.get("sector_change_pct"))
    sector_fund = _safe_float(stats.get("sector_fund_flow"))
    sector_limit_ups = _safe_int(stats.get("sector_limit_up_count"))
    membership_count = _safe_int(stats.get("hot_sector_membership_count"))
    business_alignment_count = _safe_int(stats.get("business_aligned_sector_count"))
    blockers: list[str] = []
    if str(stats.get("sector_lifecycle_state") or "") not in _NEXT_DAY_ALLOWED_SECTOR_LIFECYCLES:
        blockers.append("板块已离开启动/加速周期")
    if sector_strength < 70 or sector_change < 2.0:
        blockers.append("板块强度或涨幅不足")
    if sector_fund <= 0 or sector_limit_ups < 3:
        blockers.append("板块资金或涨停扩散不足")
    if membership_count < 2:
        blockers.append("仅有单一概念映射，主营相关性不足")
    if business_alignment_count < 1:
        blockers.append("主营行业未得到同板块涨停成员验证")
    if plan.get("is_tradeable") is False:
        blockers.append(str(plan.get("tag") or "非主板不可执行"))

    close_setup_ready = not blockers
    if bool(stats.get("breadth_climax_risk")):
        blockers.append("昨日属于单日涨停扩散高潮，次日先防兑现")
    if bool(stats.get("one_day_reversal_burst")):
        blockers.append("昨日是下跌后的单日反转脉冲，尚未证明持续")
    entry_condition = (
        f"次日重新确认{sector_name}仍维持强度≥70、资金净流入且不少于3只涨停；"
        "个股涨幅-2.5%~4.8%、量比1.15~3.2，滚动60秒累计上涨≥0.3%并有增量成交，"
        "站回VWAP且盘口承接或实时资金至少一项确认后触发A2"
    )
    invalidation = (
        f"次日{sector_name}未进入当前强势前排、涨停扩散收缩到3只以下、板块资金转负、个股跌破盘前支撑，"
        "或涨幅超过5.5%后才出现信号、60秒急拉无增量成交时取消"
    )
    strategy = {
        "strategy_type": "sector_core_laggard_watch",
        "strategy_label": "👁️ 隔夜主线观察",
        "entry_condition": entry_condition,
        "entry_price_hint": "等待当日板块重验+60秒量价确认",
        "position_ratio": "0",
        "stop_loss": 0,
        "stop_loss_pct": 0,
        "target_price": 0,
        "target_price_pct": 0,
        "invalidation": invalidation,
        "risk_reward_ratio": 0,
        "confidence": "低",
    }
    updated = dict(plan)
    updated["strategies"] = [strategy]
    updated["strategy"] = strategy["strategy_type"]
    updated["action"] = strategy["strategy_label"]
    updated["invalidation"] = invalidation
    updated["sector_core_laggard"] = {
        "sector_code": str(stats.get("sector_code") or ""),
        "sector_name": sector_name,
        "sector_strength_score": round(sector_strength, 1),
        "sector_change_pct": round(sector_change, 2),
        "sector_fund_flow": round(sector_fund, 2),
        "sector_limit_up_count": sector_limit_ups,
        "hot_sector_membership_count": membership_count,
        "hot_sector_names": list(stats.get("hot_sector_names") or []),
        "primary_industry": str(stats.get("primary_industry") or ""),
        "business_aligned_sector_count": business_alignment_count,
        "ready": False,
        "close_setup_ready": close_setup_ready,
        "overnight_revalidation_required": True,
        "blockers": blockers,
    }
    updated["monitor_state"] = "sector_core_laggard_revalidation"
    # buy_signal在页面“买点”筛选中代表已具备个股级别的盘前条件。昨日板块
    # 补涨必须先通过次日当前板块重验，因此这里只暴露监控条件，不伪装成买点。
    updated.pop("buy_signal", None)
    updated["monitor_trigger"] = {
        "type": "sector_core_laggard_intraday_revalidation",
        "status": "waiting_current_sector_and_rolling_60s_confirmation",
        "position_before_trigger": 0,
        "requires_current_sector_expansion": True,
        "requires_rolling_60s_incremental_amount": True,
        "requires_vwap_reclaim": True,
    }
    note = "昨日板块强不等于次日个股可买；先重验当日板块，再由60秒量价与盘口/资金确认"
    for key in ("risk_warnings", "notes"):
        values = list(updated.get(key) or [])
        values.extend([note] + blockers)
        updated[key] = list(dict.fromkeys(values))
    return updated


def _real_resistance_target(plan: dict, trigger_price: float, *, breakout: bool) -> float:
    """只从真实技术压力位取目标，不再用固定涨幅制造赔率。"""
    detail = plan.get("resistance_detail") or {}
    stats = plan.get("main_wave_stats") or {}
    primary_candidates = [
        _safe_float(stats.get("resistance")),
        _safe_float(detail.get("primary")),
        _safe_float(plan.get("resistance")),
    ]
    secondary_candidates = [
        _safe_float(stats.get("next_resistance")),
        _safe_float(stats.get("resistance_2")),
        _safe_float(detail.get("secondary")),
    ]
    if breakout:
        candidates = secondary_candidates
    else:
        candidates = primary_candidates
    valid = sorted({value for value in candidates if value > trigger_price})
    return valid[0] if valid else 0.0


def _apply_next_day_execution_risk_gate(plan: dict) -> dict:
    """最终执行闸门：弱市、板块无持续性或真实赔率不足时不输出买入。"""
    strategy_types = set(_next_day_strategy_types(plan))
    if not strategy_types.intersection(_NEXT_DAY_ACTIONABLE_STRATEGIES):
        return plan

    blockers: list[str] = []
    if not isinstance(plan.get("buy_signal"), dict):
        blockers.append("缺少个股级盘中触发信号，不能仅凭盘后评分给出仓位")
    market_breadth = plan.get("market_breadth") or {}
    if not _market_allows_conditional_buy(plan):
        market_label = str(market_breadth.get("market_regime_label") or "市场环境")
        market_reasons = list(market_breadth.get("direct_buy_blockers") or [])
        detail = market_reasons[0] if market_reasons else "未进入直接买入窗口"
        blockers.append(f"{market_label}不允许盘后条件买入：{detail}")

    lifecycle_state = str(plan.get("sector_lifecycle_state") or "").strip().lower()
    sector_name = str(plan.get("sector_resonance_name") or "主驱动板块")
    sector_change = _safe_float(plan.get("sector_resonance_change"))
    sector_fund = _safe_float(plan.get("sector_resonance_fund"))
    sector_score = _safe_float(plan.get("sector_resonance_score"))
    if not lifecycle_state:
        blockers.append(f"{sector_name}生命周期数据缺失")
    elif lifecycle_state in _NEXT_DAY_BLOCKED_SECTOR_LIFECYCLES:
        blockers.append(
            f"{sector_name}处于{_SECTOR_LIFECYCLE_LABELS.get(lifecycle_state, lifecycle_state)}阶段"
        )
    elif lifecycle_state not in _NEXT_DAY_ALLOWED_SECTOR_LIFECYCLES:
        blockers.append(f"{sector_name}生命周期状态未达到启动/加速")
    if sector_change <= 0 or sector_fund <= 0 or sector_score < 40:
        blockers.append(
            f"{sector_name}未形成正向共振（涨幅{sector_change:+.1f}%/资金{sector_fund:+.1f}亿/强度{sector_score:.0f}）"
        )

    if _has_next_day_direct_buy_blocker(plan):
        blockers.append("原始技术或赔率风险尚未解除")

    for strategy in plan.get("strategies") or []:
        if str(strategy.get("strategy_type") or "") not in _NEXT_DAY_ACTIONABLE_STRATEGIES:
            continue
        target_pct = _safe_float(strategy.get("target_price_pct"))
        rr_ratio = _safe_float(strategy.get("risk_reward_ratio"))
        if target_pct < 5.0:
            blockers.append(f"真实压力位空间仅{target_pct:.1f}%")
        if rr_ratio < 1.5:
            blockers.append(f"真实风险收益比仅{rr_ratio:.1f}")

    if not blockers:
        return plan
    reason = "；".join(dict.fromkeys(blockers))
    return _downgrade_next_day_plan_to_watch(
        plan,
        reason,
        entry_condition="当前不满足市场、板块和真实赔率三重执行条件，仅保留观察",
    )


def _downgrade_excess_direct_buy_plan(plan: dict) -> dict:
    """单日直接买点只保留回测边际最好的Top2，其余转观察。"""
    reason = "单日直接买点超过Top2回测上限，避免低边际候选稀释胜率"
    downgraded = dict(plan)
    downgraded["strategies"] = [{
        "strategy_type": "watch",
        "strategy_label": "👁️ 观察确认",
        "entry_condition": "形态成立但排名未进入当日Top2直接买点，只观察盘口承接",
        "entry_price_hint": "--",
        "position_ratio": "0",
        "stop_loss": 0,
        "stop_loss_pct": 0,
        "target_price": 0,
        "target_price_pct": 0,
        "invalidation": reason,
        "risk_reward_ratio": 0,
        "confidence": "低",
    }]
    downgraded["strategy"] = "watch"
    downgraded["action"] = "👁️ 观察确认"
    downgraded["invalidation"] = reason
    downgraded.pop("buy_signal", None)
    for key in ("avoid_reasons", "risk_warnings", "notes"):
        values = list(downgraded.get(key) or [])
        values.append(reason)
        downgraded[key] = list(dict.fromkeys(values))
    return downgraded


def _cap_daily_direct_buy_plans(plans: list[dict]) -> list[dict]:
    """限制Top2且优先跨形态分散，避免两个同类修复信号占满购买预案。"""
    ordered = sorted(plans, key=_next_day_plan_sort_key)
    direct_plans = [item for item in ordered if _next_day_plan_action_priority(item) <= 3]

    def _signal_family(item: dict) -> str:
        source = str(item.get("candidate_source") or "")
        if source in {"sector_repair_reversal", "old_hot_oversold_repair"}:
            return "repair"
        if source == "pre_board_momentum_shakeout":
            return "momentum_shakeout"
        if source == "pre_board_probe_wash":
            return "probe_wash"
        if source in {
            "tenbagger_pullback_watch",
            "trend_driver_setup",
            "trend_main_wave_pattern",
            "main_wave_pattern",
        }:
            return "trend"
        if source.startswith("pre_board_"):
            return "pre_board"
        if "second_wave" in source or source.startswith("high_board_"):
            return "second_wave"
        return source or str((_next_day_strategy_types(item) or ["other"])[0])

    selected_direct_codes: set[str] = set()
    seen_families: set[str] = set()
    for item in direct_plans:
        family = _signal_family(item)
        if family in seen_families:
            continue
        selected_direct_codes.add(str(item.get("code") or ""))
        seen_families.add(family)
        if len(selected_direct_codes) >= _NEXT_DAY_MAX_DIRECT_BUY_PLANS:
            break
    if len(selected_direct_codes) < _NEXT_DAY_MAX_DIRECT_BUY_PLANS:
        for item in direct_plans:
            code = str(item.get("code") or "")
            if code in selected_direct_codes:
                continue
            selected_direct_codes.add(code)
            if len(selected_direct_codes) >= _NEXT_DAY_MAX_DIRECT_BUY_PLANS:
                break

    capped: list[dict] = []
    for plan in ordered:
        if (
            _next_day_plan_action_priority(plan) <= 3
            and str(plan.get("code") or "") not in selected_direct_codes
        ):
            capped.append(_downgrade_excess_direct_buy_plan(plan))
            continue
        capped.append(plan)
    return capped


def _apply_ultra_short_entry_protocol(plan: dict) -> dict:
    """对回测保留下来的低位压缩买点附加次日开盘确认，避免预案变成无条件追买。"""
    if plan.get("candidate_source") != "pre_board_compression":
        return plan
    if plan.get("is_tradeable") is False:
        return plan
    if not _is_pre_board_compression_direct_buy_ready(plan):
        return plan

    strategies = []
    for strategy in plan.get("strategies") or []:
        strategy_type = str(strategy.get("strategy_type") or "")
        if strategy_type in {"avoid", "watch"}:
            strategies.append(strategy)
            continue
        updated = dict(strategy)
        updated["strategy_label"] = "🔵 低位起爆确认"
        updated["entry_condition"] = "次日开盘0%~0.7%且不破开盘价，竞价/开盘量能放大再买；计划持有到第三个交易日收盘，低开弱修复或高开>1%放弃"
        updated["position_ratio"] = "1/4仓"
        price = _safe_float(plan.get("price"))
        if price > 0:
            updated["stop_loss"] = round(price * 0.97, 2)
            updated["stop_loss_pct"] = -3.0
            updated["target_price"] = round(price * 1.05, 2)
            updated["target_price_pct"] = 5.0
            updated["risk_reward_ratio"] = 1.7
        updated["invalidation"] = "次日开盘承接不足、高开>1%追价、持有期跌破前日低点或第三个交易日收盘前不达预期先兑现"
        updated["confidence"] = "中"
        strategies.append(updated)

    if not strategies:
        return plan

    updated_plan = dict(plan)
    updated_plan["strategies"] = strategies
    updated_plan["strategy"] = strategies[0].get("strategy_type", plan.get("strategy"))
    updated_plan["action"] = strategies[0].get("strategy_label", plan.get("action"))
    updated_plan["invalidation"] = strategies[0].get("invalidation", plan.get("invalidation"))
    note = "高胜率超短确认: 市场宽度>=88%且5日合成指数涨幅>=4%，次日开盘0%~0.7%承接不破开盘价，回测以第三个交易日收盘兑现更优"
    for key in ("risk_warnings", "notes"):
        values = list(updated_plan.get(key) or [])
        values.append(note)
        updated_plan[key] = list(dict.fromkeys(values))
    return updated_plan


def _apply_trend_driver_entry_protocol(plan: dict) -> dict:
    """把趋势准备态转换成有价格、有量能条件的次日条件买入预案。"""
    source = str(plan.get("candidate_source") or "")
    if source not in {
        "trend_driver_setup",
        "pre_board_probe_wash",
        "tenbagger_pullback_watch",
        "low_expectation_trend_watch",
    }:
        return plan
    if plan.get("is_tradeable") is False:
        return plan

    is_probe_wash = source == "pre_board_probe_wash"
    is_tenbagger_pullback = source in {
        "tenbagger_pullback_watch",
        "low_expectation_trend_watch",
    }
    stats = dict(plan.get("main_wave_stats") or {})
    setup_state = str(stats.get("setup_state") or "")
    pattern_score = _safe_float(plan.get("main_wave_score"))
    price = _safe_float(plan.get("price"))
    change_pct = _safe_float(plan.get("change_pct"))
    turnover = _safe_float((plan.get("main_wave_stats") or {}).get("turnover")) or _safe_float(
        plan.get("turnover")
    )
    support = _safe_float(stats.get("support"))
    resistance = _safe_float(stats.get("resistance"))
    support_gap_pct = _safe_float(stats.get("support_gap_pct"), 100.0)
    resistance_gap_pct = _safe_float(stats.get("distance_to_resistance_pct"), -100.0)
    range_10_pct = _safe_float(stats.get("range_10_pct"), 100.0)
    dry_up_ratio = _safe_float(stats.get("dry_up_ratio"), 2.0)
    position_60 = _safe_float(stats.get("position_60"), 1.0)
    position_120 = _safe_float(stats.get("position_120"), 1.0)
    board_like_count = _safe_int(stats.get("board_like_count_20"))
    max_drawdown_20 = _safe_float(stats.get("max_drawdown_20"), 100.0)

    hard_risk_keywords = (
        "评分C",
        "评分D",
        "5日净流出",
        "RSI=",  # 这里只会命中引擎输出的极度超买原因
        "缩量涨停",
        "当日涨幅",
    )
    avoid_reasons = [str(item or "") for item in plan.get("avoid_reasons") or []]
    if any(keyword in reason for reason in avoid_reasons for keyword in hard_risk_keywords):
        return plan
    if not (
        setup_state in {"armed_pullback", "armed_breakout"}
        and pattern_score >= (72.0 if is_tenbagger_pullback else 84.0)
        and price > 0
        and support > 0
        and resistance > support
        and -3.5 <= change_pct <= 5.5
        and turnover <= 12.0
        and board_like_count == 0
        and max_drawdown_20 <= (22.0 if is_probe_wash else 18.0)
        and (
            is_tenbagger_pullback
            or dry_up_ratio <= (0.80 if is_probe_wash else 1.15)
        )
        and (not is_probe_wash or position_120 <= 0.55)
    ):
        return plan

    market_breadth = dict(plan.get("market_breadth") or {})
    if not _market_allows_conditional_buy(plan):
        return _downgrade_next_day_plan_to_watch(
            plan,
            "市场处于中性/弱势，趋势形态只能观察",
            entry_condition=(
                "等待市场趋势恢复后，优先按支撑位止跌回收重新计算；"
                "未回踩直接拉升不追"
            ),
        )
    planned_position = _conditional_position_ratio(
        plan,
        hot_position="1/4仓",
        bull_position="1/6仓",
    )
    market_condition = (
        "大盘极强窗口，按标准仓位等待确认"
        if market_breadth.get("direct_buy_ok")
        else "大盘强势但非极强，确认后仅轻仓"
    )

    if setup_state == "armed_pullback":
        if not (-1.5 <= support_gap_pct <= 3.0 and position_60 <= 0.82):
            return plan
        strategy_type = "trend_pullback_buy"
        strategy_label = (
            "🟢 低位预期回踩"
            if source == "low_expectation_trend_watch"
            else "🟢 牛股潜质回踩"
            if is_tenbagger_pullback
            else "🟢 试盘洗盘回踩"
            if is_probe_wash
            else "🟢 回踩买点"
        )
        signal_label = (
            "低位预期支撑回收"
            if source == "low_expectation_trend_watch"
            else "牛股潜质支撑回收"
            if is_tenbagger_pullback
            else "试盘洗盘支撑回收"
            if is_probe_wash
            else "趋势回踩承接"
        )
        trigger_price = round(support * 1.005, 2)
        stop_loss = round(support * 0.975, 2)
        target_price = round(_real_resistance_target(plan, trigger_price, breakout=False), 2)
        entry_condition = (
            f"次日回踩{support:.2f}附近不破并重新站上{trigger_price:.2f}，"
            f"量比0.8~1.8、滚动60秒价升量增并修复VWAP，"
            f"盘口/资金/主驱动至少双确认；{market_condition}"
        )
        if is_probe_wash:
            entry_condition += f"；若未回踩，仅放量突破{resistance:.2f}后再确认"
        invalidation = (
            f"有效跌破{stop_loss:.2f}、反抽不能重回VWAP、主力明显流出或板块转弱则取消"
        )
    else:
        if not (
            position_60 <= (0.78 if is_probe_wash else 0.72)
            and range_10_pct <= (22.0 if is_probe_wash else 13.0)
            and -8.0 <= resistance_gap_pct <= 1.5
        ):
            return plan
        strategy_type = "trend_breakout_buy"
        strategy_label = "🔵 试盘洗盘启动" if is_probe_wash else "🔵 低位启动"
        signal_label = "试盘洗盘放量突破" if is_probe_wash else "低位放量突破"
        trigger_price = round(resistance * 1.003, 2)
        stop_loss = round(max(support * 0.985, trigger_price * 0.97), 2)
        target_price = round(_real_resistance_target(plan, trigger_price, breakout=True), 2)
        entry_condition = (
            f"放量突破{resistance:.2f}并站稳{trigger_price:.2f}，"
            f"量比1.3~2.5、涨幅不超过6.5%，主力净流入或板块共振确认；{market_condition}"
        )
        invalidation = (
            f"突破后跌回{resistance:.2f}下方、量比>3、冲高回落或跌破{stop_loss:.2f}则取消"
        )

    if target_price <= trigger_price:
        return _downgrade_next_day_plan_to_watch(
            plan,
            "缺少触发价上方的真实技术压力位，无法验证目标空间",
        )
    target_space_pct = (target_price / trigger_price - 1.0) * 100.0
    risk = max(trigger_price - stop_loss, 0.01)
    reward = max(target_price - trigger_price, 0.0)
    risk_reward_ratio = round(reward / risk, 1)
    if target_space_pct < 5.0 or risk_reward_ratio < 1.5:
        return _downgrade_next_day_plan_to_watch(
            plan,
            f"真实压力位空间{target_space_pct:.1f}% / 风险收益比{risk_reward_ratio:.1f}不足",
        )
    strategy = {
        "strategy_type": strategy_type,
        "strategy_label": strategy_label,
        "entry_condition": entry_condition,
        "entry_price_hint": f"触发价 {trigger_price:.2f}",
        "position_ratio": planned_position,
        "position_before_trigger": 0,
        "position_after_trigger": planned_position,
        "stop_loss": stop_loss,
        "stop_loss_pct": round((stop_loss / trigger_price - 1.0) * 100.0, 1),
        "target_price": target_price,
        "target_price_pct": round((target_price / trigger_price - 1.0) * 100.0, 1),
        "invalidation": invalidation,
        "risk_reward_ratio": risk_reward_ratio,
        "confidence": "中",
    }
    updated = dict(plan)
    updated["strategies"] = [strategy]
    updated["strategy"] = strategy_type
    updated["action"] = strategy_label
    updated["invalidation"] = invalidation
    updated["avoid_reasons"] = avoid_reasons
    updated["support"] = round(support, 2)
    updated["resistance"] = round(resistance, 2)
    updated["support_detail"] = {
        "primary": round(support, 2),
        "secondary": 0,
        "source": (
            "低位预期趋势支撑"
            if source == "low_expectation_trend_watch"
            else "牛股潜质趋势支撑"
            if is_tenbagger_pullback
            else "试盘洗盘支撑"
            if is_probe_wash
            else "趋势形态支撑"
        ),
    }
    updated["resistance_detail"] = {
        "primary": round(resistance, 2),
        "secondary": 0,
        "source": "洗盘压力位" if is_probe_wash else "趋势突破压力",
    }
    updated["buy_signal"] = {
        "type": strategy_type,
        "label": signal_label,
        "status": "waiting_intraday_confirmation",
        "position_before_trigger": 0,
        "position_after_trigger": planned_position,
        "pattern_source": source,
        "trigger_price": trigger_price,
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "volume_condition": "0.8~1.8" if setup_state == "armed_pullback" else "1.3~2.5",
        "requires_fund_or_sector_confirmation": True,
        "requires_rolling_60s_volume": is_tenbagger_pullback,
        "anti_chase_max_change_pct": 1.8 if is_tenbagger_pullback else 6.5,
    }
    if is_probe_wash:
        updated["buy_signal"].update({
            "probe_gain_pct": _safe_float(stats.get("probe_gain_pct")),
            "terminal_volume_ratio": _safe_float(stats.get("terminal_volume_ratio")),
            "days_since_probe": _safe_int(stats.get("days_since_probe")),
        })
    note = "条件买点仅在分时确认后生效，不按收盘价直接买入；盘中确认后由异动链路推送飞书"
    for key in ("risk_warnings", "notes"):
        values = list(updated.get(key) or [])
        values.append(note)
        updated[key] = list(dict.fromkeys(values))
    return updated


def _mark_st_plan_research_only(plan: dict) -> dict:
    """ST可以参与形态研究，但不能被通用tagger误标为可执行。"""
    if not (plan.get("is_st") or _is_st_name(str(plan.get("name") or ""))):
        return plan
    marked = dict(plan)
    marked.update({
        "is_st": True,
        "research_only": True,
        "is_tradeable": False,
        "tag": "⚠️ ST研究池",
    })
    warning = "ST仅用于高标样本研究，仓位固定为0，不进入交易、模拟盘和飞书买点推送"
    for key in ("risk_warnings", "notes"):
        marked[key] = list(dict.fromkeys(list(marked.get(key) or []) + [warning]))
    return marked


def _apply_high_board_watch_protocol(plan: dict) -> dict:
    """五类高标只给次日验证条件；昨日封板本身不构成追板买点。"""
    types = list(plan.get("high_board_types") or [])
    if not types:
        return plan
    stats = dict(plan.get("main_wave_stats") or {})
    board_count = max(1, _safe_int(stats.get("board_count"), 1))
    turnover = _safe_float(stats.get("turnover"), _safe_float(plan.get("turnover")))
    break_count = _safe_int(stats.get("break_count"))
    is_one_word = bool(stats.get("is_one_word"))
    type_labels = list(plan.get("high_board_type_labels") or [])
    blockers: list[str] = []
    if is_one_word:
        blockers.append("一字板未完成换手，禁止排队追单")
    if break_count > 3:
        blockers.append(f"当日炸板{break_count}次，分歧过大")
    if turnover <= 0 or turnover > 24.0:
        blockers.append(f"换手率{turnover:.1f}%不在高标承接观察区间")
    if board_count >= 5:
        blockers.append(f"已达{board_count}板，盈亏比进入极端博弈区")
    if plan.get("research_only"):
        blockers.append("ST研究标的不可执行")

    planned_position = (
        _conditional_position_ratio(
            plan,
            hot_position="1/6仓",
            bull_position="1/8仓",
        )
        if not blockers
        else "0"
    )
    conditional_ready = planned_position != "0"
    reason = "；".join(blockers) if blockers else "竞价、换手、VWAP与题材梯队未完成次日确认"
    entry_condition = (
        "仅观察竞价不极端、开盘完成换手后回收VWAP，且同题材前排未掉队；"
        "任何条件缺失都不追板"
    )
    strategy = {
        "strategy_type": "high_board_watch",
        "strategy_label": "🧭 高标条件买点" if conditional_ready else "🧭 高标验证",
        "entry_condition": entry_condition,
        "entry_price_hint": "未确认前不买",
        "position_ratio": planned_position,
        "position_before_trigger": 0,
        "position_after_trigger": planned_position,
        "stop_loss": 0,
        "stop_loss_pct": 0,
        "target_price": 0,
        "target_price_pct": 0,
        "invalidation": reason,
        "risk_reward_ratio": 0,
        "confidence": "低",
    }
    updated = dict(plan)
    updated["strategies"] = [strategy]
    updated["strategy"] = "high_board_watch"
    updated["action"] = strategy["strategy_label"]
    updated["recommended_action"] = "条件触发后轻仓" if conditional_ready else "高标观察"
    updated["invalidation"] = reason
    updated["monitor_state"] = "armed_high_board_research"
    updated["monitor_state_label"] = "高标待确认"
    updated["intraday_trigger"] = entry_condition
    if conditional_ready:
        updated["buy_signal"] = {
            "type": "high_board_confirmation",
            "label": "高标竞价换手二次确认",
            "status": "waiting_auction_and_turnover_confirmation",
            "position_before_trigger": 0,
            "position_after_trigger": planned_position,
            "requires_open_turnover_confirmation": True,
            "requires_vwap_reclaim": True,
            "requires_sector_front_row": True,
        }
    else:
        updated.pop("buy_signal", None)
    support = _safe_float(stats.get("support"))
    resistance = _safe_float(stats.get("resistance"))
    if support > 0:
        updated["support"] = round(support, 2)
        updated["support_detail"] = {
            "primary": round(support, 2),
            "secondary": 0,
            "source": "高标形态锚点",
        }
    if resistance > support > 0:
        updated["resistance"] = round(resistance, 2)
        updated["resistance_detail"] = {
            "primary": round(resistance, 2),
            "secondary": 0,
            "source": "高标确认上沿",
        }
    if plan.get("research_only") and stats.get("quote_source") == "tencent_live_fallback":
        updated["tech_summary"] = "ST当日报价已校验·历史日K待回补"
    if type_labels:
        updated["top_signals"] = list(dict.fromkeys(
            [f"高标类型:{'/'.join(type_labels)}"] + list(updated.get("top_signals") or [])
        ))[:8]
    note = "高标不按昨日涨停或静态高分直接买入；页面仓位仅代表盘中二次确认后的计划上限，触发前始终为0"
    for key in ("avoid_reasons", "risk_warnings", "notes"):
        updated[key] = list(dict.fromkeys(list(updated.get(key) or []) + [note]))
    return updated


def _apply_repair_followup_entry_protocol(plan: dict) -> dict:
    """已成功推送的A2强修复，次日仅输出二次确认条件，不按收盘价追入。"""
    source = str(plan.get("candidate_source") or "")
    if source not in {"sector_repair_reversal", "old_hot_oversold_repair"}:
        return plan
    if plan.get("is_tradeable") is False:
        return plan
    if not _market_allows_conditional_buy(plan):
        return _downgrade_next_day_plan_to_watch(
            plan,
            "市场处于中性/弱势，强修复只保留观察",
            entry_condition="等待市场趋势和板块资金同时恢复后重新确认",
        )

    stats = dict(plan.get("main_wave_stats") or {})
    support = _safe_float(stats.get("support"))
    resistance = _safe_float(stats.get("resistance"))
    price = _safe_float(plan.get("price"))
    if support <= 0 or resistance <= support or price <= 0:
        return plan

    breakout_trigger = round(resistance * 1.003, 2)
    support_gap_pct = (price / support - 1.0) * 100.0
    resistance_gap_pct = (price / resistance - 1.0) * 100.0
    if -1.5 <= support_gap_pct <= 3.0 and resistance_gap_pct < -1.5:
        entry_mode = "support_reclaim"
        entry_trigger = round(support * 1.005, 2)
        stop_loss = round(support * 0.97, 2)
        target_price = round(_real_resistance_target(plan, entry_trigger, breakout=False), 2)
        entry_condition = (
            f"回踩{support:.2f}附近止跌并重新站上{entry_trigger:.2f}与VWAP，"
            "量比0.8~1.8、5分钟动能转正，且板块共振/盘口承接至少一项继续成立"
        )
        entry_price_hint = f"回踩确认 {entry_trigger:.2f}"
    else:
        entry_mode = "breakout_reclaim"
        entry_trigger = breakout_trigger
        # 突破买点按触发价控制3%风险，不能沿用远处底部支撑造成赔率失真。
        stop_loss = round(max(support * 0.97, breakout_trigger * 0.97), 2)
        target_price = round(_real_resistance_target(plan, entry_trigger, breakout=True), 2)
        entry_condition = (
            f"放量站稳{breakout_trigger:.2f}且不跌回压力位{resistance:.2f}下方，"
            "量比1.3~2.5、涨幅不超过6.5%，且板块共振/主力净流入至少一项继续成立"
        )
        entry_price_hint = f"突破确认 {breakout_trigger:.2f}"
    if target_price <= entry_trigger:
        return _downgrade_next_day_plan_to_watch(
            plan,
            "缺少触发价上方的真实技术压力位，强修复赔率无法验证",
        )
    target_space_pct = (target_price / entry_trigger - 1.0) * 100.0
    risk_reward_ratio = round(
        max(target_price - entry_trigger, 0.0) / max(entry_trigger - stop_loss, 0.01),
        1,
    )
    if target_space_pct < 5.0 or risk_reward_ratio < 1.5:
        return _downgrade_next_day_plan_to_watch(
            plan,
            f"真实压力位空间{target_space_pct:.1f}% / 风险收益比{risk_reward_ratio:.1f}不足",
        )
    max_entry_price = round(entry_trigger * 1.01, 2)
    entry_condition += f"；成交价高于{max_entry_price:.2f}不追"
    current_change_pct = _safe_float(plan.get("change_pct"))
    current_amplitude = _safe_float(stats.get("current_amplitude"))
    current_support_strength = _safe_float(stats.get("current_support_strength"))
    current_orderbook_imbalance = _safe_float(stats.get("current_orderbook_imbalance"))
    live_blockers: list[str] = []
    if current_change_pct > 6.5:
        live_blockers.append(f"当前涨幅{current_change_pct:.1f}%超过6.5%追价上限")
    if current_amplitude > 7.5:
        live_blockers.append(f"当前振幅{current_amplitude:.1f}%偏大")
    if current_support_strength > 0 and (
        current_support_strength < 60 or current_orderbook_imbalance < 0
    ):
        live_blockers.append(
            f"盘口承接{current_support_strength:.0f}/失衡{current_orderbook_imbalance:+.2f}未确认"
        )
    if price > max_entry_price:
        live_blockers.append(f"现价{price:.2f}高于最高执行价{max_entry_price:.2f}")
    if live_blockers:
        reason = "；".join(live_blockers)
        downgraded = _downgrade_excess_direct_buy_plan(plan)
        downgraded["invalidation"] = reason
        downgraded["strategies"][0]["entry_condition"] = "A2形态仍保留，但当前实时风险已超出执行区间"
        downgraded["strategies"][0]["invalidation"] = reason
        for key in ("avoid_reasons", "risk_warnings", "notes"):
            values = [item for item in downgraded.get(key) or [] if "Top2回测上限" not in str(item)]
            values.append(reason)
            downgraded[key] = list(dict.fromkeys(values))
        return downgraded
    label = "产业链强修复二次确认" if source == "sector_repair_reversal" else "旧高标超跌修复二次确认"
    market_direct_ok = bool((plan.get("market_breadth") or {}).get("direct_buy_ok"))
    planned_position = _conditional_position_ratio(
        plan,
        hot_position="1/4仓",
        bull_position="1/6仓",
    )
    invalidation = (
        f"跌破{stop_loss:.2f}、回踩后不能重回VWAP、板块强度显著下降或主力转为持续净流出则取消"
    )
    strategy = {
        "strategy_type": "repair_followup_buy",
        "strategy_label": "🟠 强修复确认",
        "entry_condition": entry_condition,
        "entry_price_hint": entry_price_hint,
        "position_ratio": planned_position,
        "position_before_trigger": 0,
        "position_after_trigger": planned_position,
        "stop_loss": stop_loss,
        "stop_loss_pct": round((stop_loss / max(entry_trigger, 0.01) - 1.0) * 100.0, 1),
        "target_price": target_price,
        "target_price_pct": round((target_price / entry_trigger - 1.0) * 100.0, 1),
        "invalidation": invalidation,
        "risk_reward_ratio": risk_reward_ratio,
        "confidence": "中" if market_direct_ok else "低",
    }
    updated = dict(plan)
    updated["strategies"] = [strategy]
    updated["strategy"] = "repair_followup_buy"
    updated["action"] = "🟠 强修复确认"
    updated["invalidation"] = invalidation
    # 页面展示与实际触发口径保持一致，避免通用技术位覆盖修复形态的支撑/压力。
    updated["support"] = round(support, 2)
    updated["resistance"] = round(resistance, 2)
    updated["support_detail"] = {
        "primary": round(support, 2),
        "secondary": 0,
        "source": "强修复形态支撑",
    }
    updated["resistance_detail"] = {
        "primary": round(resistance, 2),
        "secondary": 0,
        "source": "强修复突破压力",
    }
    updated["buy_signal"] = {
        "type": "repair_followup_buy",
        "label": label,
        "status": "waiting_intraday_confirmation",
        "position_before_trigger": 0,
        "position_after_trigger": planned_position,
        "entry_mode": entry_mode,
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "trigger_price": entry_trigger,
        "max_entry_price": max_entry_price,
        "breakout_trigger": breakout_trigger,
        "requires_fund_or_sector_confirmation": True,
    }
    note = "该标的来自已成功发送的A2异动信号；次日必须二次确认，不按收盘价或高开价直接追入"
    for key in ("risk_warnings", "notes"):
        values = list(updated.get(key) or [])
        values.append(note)
        updated[key] = list(dict.fromkeys(values))
    return updated


def _downgrade_observe_only_plan(plan: dict) -> dict:
    """创业板/科创/北交所只保留观察，不输出买入仓位。"""
    if plan.get("is_tradeable") is not False:
        return plan

    downgraded = dict(plan)
    reason = str(downgraded.get("tag") or "观察标的")
    invalidation = f"{reason}，按交易规则不生成买入仓位"
    downgraded["strategies"] = [{
        "strategy_type": "watch",
        "strategy_label": "👁️ 仅观察",
        "entry_condition": "观察标的，不生成明日买入预案",
        "entry_price_hint": "--",
        "position_ratio": "0",
        "stop_loss": 0,
        "stop_loss_pct": 0,
        "target_price": 0,
        "target_price_pct": 0,
        "invalidation": invalidation,
        "risk_reward_ratio": 0,
        "confidence": "低",
    }]
    downgraded["strategy"] = "watch"
    downgraded["action"] = "👁️ 仅观察"
    downgraded["invalidation"] = invalidation
    avoid_reasons = list(downgraded.get("avoid_reasons") or [])
    avoid_reasons.append(invalidation)
    downgraded["avoid_reasons"] = list(dict.fromkeys(avoid_reasons))
    downgraded["notes"] = list(dict.fromkeys(
        list(downgraded.get("notes") or []) + [invalidation]
    ))
    return downgraded


def _next_day_source_cap(source: str, item_limit: int) -> int:
    ratio = _NEXT_DAY_SOURCE_SOFT_CAP_RATIO.get(source)
    if ratio is None:
        return item_limit
    return max(1, int(item_limit * ratio))


def _append_next_day_pool(
    *,
    pool: list[dict],
    selected: list[dict],
    selected_codes: set[str],
    source_counts: dict[str, int],
    item_limit: int,
) -> None:
    for item in pool:
        if len(selected) >= item_limit:
            return
        code = str(item.get("code") or "")
        if not code or code in selected_codes:
            continue
        source = str(item.get("candidate_source") or "")
        if source and source_counts.get(source, 0) >= _next_day_source_cap(source, item_limit):
            continue
        selected.append(item)
        selected_codes.add(code)
        if source:
            source_counts[source] = source_counts.get(source, 0) + 1


def _diversified_actionable_top(items: list[dict], item_limit: int) -> list[dict]:
    """先保证可买性，再做形态软分散，避免观察/不建议候选挤掉可执行票。"""
    if item_limit <= 0:
        return []

    ordered = sorted(items, key=_next_day_plan_sort_key)
    selected: list[dict] = []
    selected_codes: set[str] = set()
    source_counts: dict[str, int] = {}
    high_board_cap = min(item_limit, max(5, item_limit // 2))

    def _append_pool(pool: list[dict]) -> None:
        """Top20中高标研究席位最多占一半，剩余名额保留给低位条件买点。"""
        for pool_item in pool:
            if len(selected) >= item_limit:
                return
            code = str(pool_item.get("code") or "")
            if not code or code in selected_codes:
                continue
            is_high_board = bool(pool_item.get("is_high_board_candidate"))
            if is_high_board and sum(
                1 for selected_item in selected
                if selected_item.get("is_high_board_candidate")
            ) >= high_board_cap:
                continue
            _append_next_day_pool(
                pool=[pool_item],
                selected=selected,
                selected_codes=selected_codes,
                source_counts=source_counts,
                item_limit=item_limit,
            )

    actionable = [item for item in ordered if _next_day_plan_action_priority(item) <= 3]
    watch = [item for item in ordered if _next_day_plan_action_priority(item) == 4]
    blocked = [item for item in ordered if _next_day_plan_action_priority(item) >= 5]
    qualified_watch = [
        item for item in watch
        if item.get("candidate_source")
        or _safe_float(item.get("plan_priority_score")) >= 55.0
    ]
    weak_watch = [item for item in watch if item not in qualified_watch]
    qualified_blocked = [
        item for item in blocked
        if item.get("candidate_source")
        and _safe_float(item.get("plan_priority_score")) >= 55.0
    ]
    weak_blocked = [item for item in blocked if item not in qualified_blocked]

    # 盘后预案首先保留已具备触发价、失效条件和触发后计划仓位的条件买点。
    # 高标五类的研究覆盖不能再次把真正可执行的低位方案挤出Top20。
    _append_pool(actionable)

    # 高标不是单一“连板数榜”。五条驱动路线分别预留席位，避免事件/换手/
    # 记忆/业绩/二波候选再次被通用趋势分数和同源形态软上限挤出Top20。
    high_board_pool = [
        item for item in ordered
        if item.get("is_high_board_candidate") and not item.get("is_capacity_trend")
    ]
    st_research_lane = [item for item in high_board_pool if item.get("research_only")]
    _append_pool(st_research_lane[:min(2, item_limit)])
    current_relay_lane = [
        item for item in high_board_pool
        if _safe_int((item.get("main_wave_stats") or {}).get("board_count")) >= 2
    ]
    _append_pool(current_relay_lane[:min(4, item_limit)])
    lane_reservations = {
        "event_lock": 1,
        "theme_turnover": 1,
        "emotion_memory": 1,
        "earnings_surprise": 1,
        "second_wave": 1,
    }
    for high_board_type, reservation in lane_reservations.items():
        lane = [
            item for item in high_board_pool
            if high_board_type in (item.get("high_board_types") or [])
        ]
        added = 0
        for lane_item in lane:
            lane_code = str(lane_item.get("code") or "")
            if not lane_code or lane_code in selected_codes:
                continue
            _append_pool([lane_item])
            if lane_code in selected_codes:
                added += 1
            if added >= reservation or len(selected) >= item_limit:
                break

    # 盘后直接公告与低位趋势交叉是独立的预期差赛道。至少保留2个已通过
    # 技术闸门的零仓位席位，避免它们再次被同源纯形态候选的软上限挤掉。
    low_event_watch = [
        item for item in qualified_watch + qualified_blocked
        if str(item.get("candidate_source") or "") == "low_expectation_trend_watch"
        and str((item.get("main_wave_stats") or {}).get("news_event_grade") or "")
        in {"hard", "medium"}
    ]
    _append_pool(low_event_watch[:min(2, item_limit)])

    # 强趋势首阴/缩量十字星是用户可理解的独立低吸赛道。即使当日板块尚未
    # 共振，也应在Top20保留少量“零仓位等待”席位，供盘中60秒量能确认；
    # 但通过来源软上限限制为最多20%，不能反向挤占事件和主线候选。
    def _pullback_lane_stats(item: dict) -> dict:
        if str(item.get("dynamic_pool_source") or "") == "main_wave_pullback_pattern":
            return item.get("dynamic_pool_stats") or {}
        return item.get("main_wave_stats") or {}

    pullback_watch = sorted(
        [
            item for item in qualified_watch + qualified_blocked
            if (
                str(item.get("candidate_source") or "") == "main_wave_pullback_pattern"
                or str(item.get("dynamic_pool_source") or "") == "main_wave_pullback_pattern"
            )
            and str(_pullback_lane_stats(item).get("pullback_quality_tier") or "")
            in {"strict", "reclaim"}
        ],
        key=lambda item: (
            str(_pullback_lane_stats(item).get("pullback_quality_tier") or "") == "strict",
            _safe_float(item.get("plan_priority_score")),
            _safe_float(item.get("main_wave_score")),
        ),
        reverse=True,
    )
    _append_pool(pullback_watch[:min(3, item_limit)])

    # 首板冲二板是与低吸/二波并列的独立交易赛道。在Top20至少保留
    # 2个已过收盘质量闸门的零仓位条件席位，避免被高分观察形态全部挤掉。
    relay_watch = [
        item for item in qualified_watch
        if str(item.get("candidate_source") or "") == "second_board_relay"
    ]
    _append_pool(relay_watch[:min(2, item_limit)])

    # 有形态依据但暂被风控降级的候选，仍比“没有任何形态来源的普通榜单观察”
    # 更适合作为明日检测池；页面会继续明确显示其不建议/零仓位状态。
    for pool in (
        actionable,
        qualified_watch,
        weak_watch,
        qualified_blocked,
        weak_blocked,
    ):
        _append_pool(pool)
        if len(selected) >= item_limit:
            break
    # 只有所有动作层和形态来源都按软上限遍历完后仍不足，才用全局排序补齐。
    # 这样同一种“试盘洗盘”不会在第一层第二遍就吞掉全部席位。
    if len(selected) < item_limit:
        for item in ordered:
            code = str(item.get("code") or "")
            if not code or code in selected_codes:
                continue
            source = str(item.get("candidate_source") or "")
            # 隔夜板块补涨是唯一需要硬上限的批量通道：同一日常会产出几十只
            # 同质成分股，不能在“补齐Top20”阶段绕过前面的来源上限。
            if (
                source == "sector_core_laggard"
                and source_counts.get(source, 0) >= _next_day_source_cap(source, item_limit)
            ):
                continue
            if (
                item.get("is_high_board_candidate")
                and sum(
                    1 for selected_item in selected
                    if selected_item.get("is_high_board_candidate")
                ) >= high_board_cap
            ):
                continue
            selected.append(item)
            selected_codes.add(code)
            if source:
                source_counts[source] = source_counts.get(source, 0) + 1
            if len(selected) >= item_limit:
                break
    # 预留名额只负责“不要漏掉赛道”，不能让低质量零仓位接力票固定占据
    # 页面前两行；最终仍按可执行层级和校准质量分排序。
    return sorted(selected, key=_next_day_plan_sort_key)


# =========================================================================
# 技术指标纯函数 — EMA/MACD/RSI/KDJ (v3.0新增)
# =========================================================================

def _calc_ema(data, period: int):
    """计算EMA(指数移动平均) — 返回与data等长的numpy数组(前period-1个为NaN)"""
    import numpy as np
    if len(data) < period:
        return np.full_like(data, np.nan, dtype=float)
    alpha = 2.0 / (period + 1)
    ema = np.full_like(data, np.nan, dtype=float)
    ema[period - 1] = np.mean(data[:period])
    for i in range(period, len(data)):
        ema[i] = alpha * data[i] + (1 - alpha) * ema[i - 1]
    return ema


def _calc_rsi(closes, period: int = 14) -> float:
    """计算RSI — Wilder平滑法(与spot.py calc_rsi一致)"""
    import numpy as np
    n = len(closes)
    if n < period + 1:
        return 50.0
    # 使用全部可用数据计算,而非仅最后period+1个
    delta = np.diff(closes)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    # 第一个avg_gain/avg_loss用SMA方式
    avg_gain = float(np.mean(gain[1:period + 1]))
    avg_loss = float(np.mean(loss[1:period + 1]))
    # Wilder平滑递推
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gain[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i - 1]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _calc_kdj(highs, lows, closes, n: int = 9, m1: int = 3, m2: int = 3):
    """计算KDJ — 返回(K值数组, D值数组)"""
    import numpy as np
    bars = []
    for high, low, close in zip(highs, lows, closes):
        try:
            h = float(high)
            l = float(low)
            c = float(close)
        except (TypeError, ValueError):
            continue
        if not (np.isfinite(h) and np.isfinite(l) and np.isfinite(c)):
            continue
        if h <= 0 or l <= 0 or c <= 0:
            continue
        bars.append((h, l, c))

    if len(bars) < n:
        return None

    highs = np.array([item[0] for item in bars], dtype=float)
    lows = np.array([item[1] for item in bars], dtype=float)
    closes = np.array([item[2] for item in bars], dtype=float)
    
    k_vals = []
    d_vals = []
    k = 50.0
    d = 50.0
    
    for i in range(n - 1, len(closes)):
        high_n = highs[i - n + 1:i + 1]
        low_n = lows[i - n + 1:i + 1]
        if len(high_n) == 0 or len(low_n) == 0:
            return None
        h = np.max(high_n)
        l = np.min(low_n)
        if h == l:
            rsv = 50.0
        else:
            rsv = (closes[i] - l) / (h - l) * 100.0
        k = (m1 - 1) / m1 * k + 1 / m1 * rsv
        d = (m2 - 1) / m2 * d + 1 / m2 * k
        k_vals.append(k)
        d_vals.append(d)
    
    return k_vals, d_vals


def _format_data_source_label(source: str) -> str:
    mapping = {
        "eastmoney_main_fund": "东方财富主力资金",
        "eastmoney_main_fund_stale": "东方财富主力资金(旧快照)",
        "fund_flow": "已验证主力资金",
        "fund_flow_stale": "主力资金(旧快照)",
        "stock_spot": "Tencent 实时行情",
        "tencent_realtime": "Tencent 实时行情",
    }
    return mapping.get(source or "", source or "未知来源")


def _format_orderbook_volume(value: float) -> str:
    if value >= 10000:
        return f"{value / 10000:.1f}万手"
    return f"{value:.0f}手"


def _format_net_amount_yi(value: float) -> str:
    abs_yi = abs(float(value)) / 1e8
    return f"-{abs_yi:.1f}亿" if float(value) < 0 else f"{abs_yi:.1f}亿"


def _grade_rank(label: str | None) -> int:
    mapping = {
        "B类观察候选": 0,
        "A2 盘口确认后执行": 1,
        "A1 可直接执行": 2,
    }
    return mapping.get(label or "", -1)


def _entry_grade(score: float, direct_execute: bool = False) -> str:
    if direct_execute:
        return "A1 可直接执行"
    if score >= 5:
        return "A2 盘口确认后执行"
    return "B类观察候选"


def _resolve_setup_track(anomaly: dict, setup_grade: str | None) -> str:
    if setup_grade != "A1 可直接执行":
        return ""
    event_type = str(anomaly.get("event_type") or "")
    if event_type == "limit_up":
        return "打板型"
    if event_type in {"capital", "breakthrough", "limit_down", "low_absorb"}:
        return "趋势/资金型"
    return "趋势/资金型"


def _display_setup_grade(anomaly: dict, setup_grade: str | None) -> str | None:
    if not setup_grade:
        return None
    track = _resolve_setup_track(anomaly, setup_grade)
    if setup_grade == "A1 可直接执行" and track:
        return f"A1-{track}"
    return setup_grade


def _serialize_dragon_result(result) -> dict:
    return {
        "code": result.code,
        "name": result.name,
        "sector_code": result.sector_code,
        "sector_name": result.sector_name,
        "score": result.score,
        "level": result.level,
        "change_pct": result.change_pct,
        "main_net_inflow": result.main_net_inflow,
        "main_fund_status": result.main_fund_status,
        "main_fund_decision_at": result.main_fund_decision_at,
        "main_fund_display": copy.deepcopy(result.main_fund_display),
        "consecutive_days": result.consecutive_days,
        "turnover": result.turnover,
        "seal_amount": result.seal_amount,
        "reasons": result.reasons,
        "recognition_score": result.recognition_score,
        "tradability_score": result.tradability_score,
        "leader_type": result.leader_type,
        "link_role": result.link_role,
        "leader_code": result.leader_code,
        "leader_name": result.leader_name,
        "leader_recognition_score": result.leader_recognition_score,
        "leader_tradability_score": result.leader_tradability_score,
        "leader_origin_type": result.leader_origin_type,
        "linkage_score": result.linkage_score,
        "business_relevance_score": result.business_relevance_score,
        "follower_shape_score": result.follower_shape_score,
        "theme_alignment_score": result.theme_alignment_score,
        "leader_driver_reason": result.leader_driver_reason,
        "leader_industry": result.leader_industry,
        "follower_industry": result.follower_industry,
        "sector_limit_up_count": result.sector_limit_up_count,
        "sector_up_ratio": result.sector_up_ratio,
        "event_grade": result.event_grade,
        "event_score": result.event_score,
        "news_evidence": copy.deepcopy(result.news_evidence) if isinstance(getattr(result, "news_evidence", None), dict) else {},
        "trend_leadership_score": result.trend_leadership_score,
        "first_seal_time": result.first_seal_time,
        "break_count": result.break_count,
        "is_one_word": result.is_one_word,
    }


def _dedupe_dragons_by_code(items: list[dict], limit: int = 50) -> list[dict]:
    """龙头追踪列表按个股去重，保留最高分板块并附加相关板块。"""
    grouped: dict[str, list[dict]] = {}
    for item in items:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        grouped.setdefault(code, []).append(item)

    deduped = []
    level_rank = {"dragon": 2, "quasi_dragon": 1}
    for code, rows in grouped.items():
        rows = sorted(
            rows,
            key=lambda row: (
                1 if str(row.get("link_role") or "") == "leader_a" else 0,
                _safe_float(row.get("score")),
                level_rank.get(str(row.get("level") or ""), 0),
                1 if str(row.get("sector_name") or "").strip() else 0,
            ),
            reverse=True,
        )
        primary = dict(rows[0])
        related_sectors = []
        leader_links = []
        seen_sector_codes = set()
        for row in rows:
            sector_code = str(row.get("sector_code") or "").strip()
            if not sector_code or sector_code in seen_sector_codes:
                continue
            seen_sector_codes.add(sector_code)
            related_sectors.append({
                "sector_code": sector_code,
                "sector_name": str(row.get("sector_name") or sector_code),
                "score": _safe_float(row.get("score")),
            })
            if str(row.get("link_role") or "") == "follower_b" and row.get("leader_code"):
                leader_links.append({
                    "leader_code": str(row.get("leader_code") or ""),
                    "leader_name": str(row.get("leader_name") or ""),
                    "sector_code": sector_code,
                    "sector_name": str(row.get("sector_name") or sector_code),
                    "linkage_score": _safe_float(row.get("linkage_score")),
                    "leader_recognition_score": _safe_float(row.get("leader_recognition_score")),
                    "leader_tradability_score": _safe_float(row.get("leader_tradability_score")),
                    "leader_origin_type": str(row.get("leader_origin_type") or ""),
                    "business_relevance_score": _safe_float(row.get("business_relevance_score")),
                    "follower_shape_score": _safe_float(row.get("follower_shape_score")),
                    "theme_alignment_score": _safe_float(row.get("theme_alignment_score")),
                    "leader_driver_reason": str(row.get("leader_driver_reason") or ""),
                    "leader_industry": str(row.get("leader_industry") or ""),
                    "follower_industry": str(row.get("follower_industry") or ""),
                })
        primary["related_sectors"] = related_sectors
        primary["related_sector_count"] = len(related_sectors)
        primary["leader_links"] = sorted(
            leader_links,
            key=lambda item: _safe_float(item.get("linkage_score")),
            reverse=True,
        )[:3]
        if primary.get("leader_links") and str(primary.get("link_role") or "") != "leader_a":
            primary.update({
                "link_role": "follower_b",
                **primary["leader_links"][0],
            })
        deduped.append(primary)

    def _display_rank(row: dict) -> tuple[float, int, float]:
        return (
            _safe_float(
                row.get("linkage_score")
                if str(row.get("link_role") or "") == "follower_b"
                else row.get("recognition_score") or row.get("score")
            ),
            level_rank.get(str(row.get("level") or ""), 0),
            _safe_float(row.get("change_pct")),
        )

    leaders = sorted(
        [row for row in deduped if str(row.get("link_role") or "") == "leader_a"],
        key=_display_rank,
        reverse=True,
    )
    followers = sorted(
        [row for row in deduped if str(row.get("link_role") or "") == "follower_b"],
        key=_display_rank,
        reverse=True,
    )
    legacy_rows = sorted(
        [row for row in deduped if not str(row.get("link_role") or "")],
        key=_display_rank,
        reverse=True,
    )
    follower_limit = min(8, max(1, round(limit * 0.16)))
    selected = leaders[: max(limit - follower_limit, 0)]
    selected.extend(followers[: min(follower_limit, max(limit - len(selected), 0))])
    # 兼容历史快照：旧版龙头结果没有 link_role，不能在读缓存时整批消失。
    selected.extend(legacy_rows[: max(limit - len(selected), 0)])
    selected.sort(
        key=lambda row: (
            1 if str(row.get("link_role") or "") == "leader_a" else 0,
            *_display_rank(row),
        ),
        reverse=True,
    )
    return selected[:limit]


def _event_signal_label(event_type: str, fallback: str = "") -> str:
    if fallback:
        return fallback
    mapping = {
        "limit_up": "打板关注",
        "capital": "资金确认流入",
        "low_absorb": "低吸弱转强",
        "breakthrough": "突破买点",
        "limit_down": "低吸观察",
    }
    return mapping.get(event_type, event_type or "异动信号")


def _classify_anomaly_alert_tier(event_type: str, setup_grade: str | None) -> str:
    grade = str(setup_grade or "")
    if grade == "A1 可直接执行":
        return "strong"
    if grade == "A2 盘口确认后执行" and event_type in {"capital", "breakthrough", "low_absorb"}:
        return "light"
    return ""


def _resolve_speculation_expectation_context(anomaly: dict | None) -> dict:
    """把技术信号翻译成可区分的炒作预期，不把题材故事冒充买点。"""
    payload = anomaly or {}
    detail = payload.get("detail") or {}
    event_type = str(payload.get("event_type") or "")
    signal_type = str(detail.get("signal_type") or event_type)
    sector_name = ""
    for sector in detail.get("sector_factors") or []:
        if _is_causal_trade_driver_sector(sector):
            sector_name = str(sector.get("sector_name") or "").strip()
            if sector_name:
                break
    driver_label = sector_name or str(detail.get("driver_primary") or "").strip() or "个股量价"

    if signal_type in {
        "main_wave_green_open_reclaim",
        "main_wave_shape_pullback_reclaim",
        "main_wave_support_reclaim",
        "trend_driver_low_absorb",
        "low_base_low_absorb",
    }:
        return {
            "speculation_type": "趋势回踩",
            "speculation_logic": f"{driver_label}主升/趋势结构中的支撑回收，博弈缩量回踩后的延续",
            "continuation_condition": "支撑与VWAP不再失守，60秒增量成交、盘口承接和板块强度继续成立",
            "invalidation_condition": "重新跌破支撑或VWAP且放量、板块转弱或主力资金转为明显流出",
        }
    if signal_type in {"underwater_acceleration", "underwater_reversal"}:
        return {
            "speculation_type": "水下弱转强",
            "speculation_logic": f"{driver_label}内的绿盘下杀后资金回流，博弈日内弱转强而非翻红后追价",
            "continuation_condition": "回拉保持在VWAP附近、低点不再下移且板块至少维持正向扩散",
            "invalidation_condition": "回落跌破刚形成的日内低点、增量成交消失或板块同步走弱",
        }
    if signal_type == "second_wave_restart":
        return {
            "speculation_type": "高标二波",
            "speculation_logic": "前期高标完成充分调整后重新放量回收关键位，博弈二波启动",
            "continuation_condition": "前高记忆仍有效、回踩不破支撑且板块出现新一轮梯队扩散",
            "invalidation_condition": "反抽无量、再次跌破调整低点或同题材高标率先退潮",
        }
    if signal_type in {"event_relay_confirmation", "second_board_relay_confirmation"}:
        event_title = str(detail.get("event_title") or "").strip()
        return {
            "speculation_type": "事件接力",
            "speculation_logic": f"公司级事件催化后的资金接力{f'：{event_title[:48]}' if event_title else ''}",
            "continuation_condition": "事件为直接硬催化、竞价和换手承接正常，并有同产业链扩散",
            "invalidation_condition": "公告不及预期、竞价兑现、开板放量回落或板块没有跟随",
        }
    if signal_type == "leader_linkage_confirmation":
        leader_name = str(detail.get("leader_name") or detail.get("leader_code") or "龙头A")
        return {
            "speculation_type": "看A做B",
            "speculation_logic": f"{leader_name}维持前排后，博弈同产业链高相关低位标的补涨",
            "continuation_condition": "龙头不破位、业务映射真实且B股放量站稳VWAP",
            "invalidation_condition": "龙头开板转弱、板块资金回落或B股放量冲高回落",
        }
    if signal_type == "sector_core_laggard_ignition":
        return {
            "speculation_type": "主线补涨",
            "speculation_logic": f"{driver_label}已形成资金与涨停扩散，博弈多重产业映射的低位成员补涨",
            "continuation_condition": "板块维持至少3只涨停，个股守住VWAP且60秒增量成交继续成立",
            "invalidation_condition": "板块扩散收缩、资金转负，或个股急拉无量并跌回VWAP下方",
        }
    if signal_type == "sector_repair_reversal":
        return {
            "speculation_type": "板块超跌修复",
            "speculation_logic": f"{driver_label}形成多股修复，博弈低位反包的首日溢价",
            "continuation_condition": "板块涨停宽度、资金流和领涨股强度至少维持两项",
            "invalidation_condition": "修复仅剩单股、板块资金转负或个股冲高失守VWAP",
        }
    if signal_type == "old_hot_oversold_repair":
        return {
            "speculation_type": "旧高标修复",
            "speculation_logic": "历史辨识度高标深调后的情绪修复，只博弈修复段而非新主升",
            "continuation_condition": "旧题材重新出现梯队、个股换手健康且不进入加速追高区",
            "invalidation_condition": "孤立反抽、放量滞涨或当日涨幅进入高位兑现区",
        }
    if signal_type == "positive_acceleration":
        return {
            "speculation_type": "盘中动能观察",
            "speculation_logic": f"{driver_label}出现滚动60秒放量加速，只验证资金抢筹，不直接代表买入赔率",
            "continuation_condition": "首次回踩承接不破、成交保持且形态池随后给出正式买点",
            "invalidation_condition": "急拉后跌回VWAP、盘口撤单或量价背离",
        }
    if event_type == "limit_up":
        return {
            "speculation_type": "首板/连板情绪",
            "speculation_logic": f"{driver_label}封板后的情绪接力预期，属于打板观察而非低吸",
            "continuation_condition": "封单稳定、换手充分且同题材有梯队助攻",
            "invalidation_condition": "反复炸板、封单衰减或板块核心率先开板",
        }
    if event_type == "breakthrough":
        return {
            "speculation_type": "趋势突破",
            "speculation_logic": f"{driver_label}放量突破平台，博弈筹码换手后的趋势延续",
            "continuation_condition": "突破位回踩不破、量价不过热且资金/板块至少一项继续确认",
            "invalidation_condition": "跌回突破位下方、放量长上影或板块转弱",
        }
    if event_type == "capital":
        return {
            "speculation_type": "资金驱动",
            "speculation_logic": f"{driver_label}获得大单与盘口确认，博弈资金持续流入",
            "continuation_condition": "主力净流入保持、VWAP不破且量比处于健康区间",
            "invalidation_condition": "资金转为净流出、盘口承接下降或冲高放量回落",
        }
    return {
        "speculation_type": "综合信号",
        "speculation_logic": "量价、资金和板块的综合预期，需按消息内条件确认",
        "continuation_condition": "触发条件持续成立且风险项未出现",
        "invalidation_condition": "任一核心确认失效时取消，不追加追价",
    }


def _push_focus_score(message: PushMessage) -> float:
    extra = message.extra or {}
    evidence = extra.get("performance_evidence") or {}
    grade = str(extra.get("setup_grade") or "")
    event_type = str(extra.get("event_type") or "")
    signal_type = str(extra.get("signal_variant") or event_type)
    change_pct = _safe_float(extra.get("change_pct"))
    score = _grade_rank(grade) * 28.0
    score += 24.0 if extra.get("buypoint_pushable") else 0.0
    score += 18.0 if event_type == "low_absorb" else 8.0 if event_type == "breakthrough" else 2.0
    score += 10.0 if extra.get("detection_pool_member") else 0.0
    score += 8.0 if signal_type.startswith("main_wave_") else 0.0
    score += 10.0 if -2.5 <= change_pct <= 1.8 else 4.0 if change_pct <= 3.0 else -18.0
    score += min(_safe_float(extra.get("signal_score")), 100.0) * 0.12
    sample_count = _safe_int(evidence.get("sample_count"))
    if sample_count:
        score += _safe_float(evidence.get("win_rate_3d")) * 30.0
        score += min(max(_safe_float(evidence.get("avg_excess_return_3d")), -5.0), 5.0)
    if event_type in {"limit_up", "limit_down"} or extra.get("observation_only"):
        score -= 40.0
    return round(score, 2)


def _is_core_buy_point_message(message: PushMessage) -> bool:
    extra = message.extra or {}
    evidence = extra.get("performance_evidence") or {}
    event_type = str(extra.get("event_type") or "")
    signal_type = str(extra.get("signal_variant") or event_type)
    grade = str(extra.get("setup_grade") or "")
    change_pct = _safe_float(extra.get("change_pct"))
    if (
        not extra.get("buypoint_pushable")
        or extra.get("observation_only")
        or event_type in {"limit_up", "limit_down"}
        or _grade_rank(grade) < _grade_rank("A2 盘口确认后执行")
        or change_pct > _safe_float(settings.ANOMALY_PUSH_CORE_MAX_CHANGE_PCT, 3.0)
    ):
        return False
    sample_count = _safe_int(evidence.get("sample_count"))
    if sample_count >= max(int(settings.ANOMALY_EVAL_MIN_SAMPLES), 1):
        return bool(
            _safe_float(evidence.get("win_rate_3d"))
            >= _safe_float(settings.ANOMALY_PUSH_CORE_MIN_WIN_RATE, 0.62)
            and _safe_float(evidence.get("avg_net_return_3d")) > 0
            and _safe_float(evidence.get("avg_excess_return_3d")) > 0
        )
    structural_core = bool(
        event_type == "low_absorb"
        and (
            signal_type.startswith("main_wave_")
            or signal_type
            in {
                "underwater_acceleration",
                "underwater_reversal",
                "trend_driver_low_absorb",
                "low_base_low_absorb",
                "second_wave_restart",
            }
        )
    )
    return structural_core and bool(extra.get("detection_pool_member"))


def _annotate_automatic_push_focus(messages: list[PushMessage]) -> list[PushMessage]:
    """每轮最多突出一个重点买点，其余明确标成条件买点或异动观察。"""
    if not messages:
        return []
    core_limit = max(int(settings.ANOMALY_PUSH_CORE_MAX_PER_SCAN), 0)
    core_candidates = [
        message
        for message in sorted(messages, key=_push_focus_score, reverse=True)
        if _is_core_buy_point_message(message)
    ]
    core_ids = {id(message) for message in core_candidates[:core_limit]}

    focus_rank = {"core": 3, "conditional": 2, "observation": 1}
    annotated: list[PushMessage] = []
    for message in messages:
        extra = dict(message.extra or {})
        evidence = extra.get("performance_evidence") or {}
        if id(message) in core_ids:
            focus_tier = "core"
            focus_label = "🔥 重点买点"
            message.priority = max(int(message.priority), 10)
            message.msg_type = "warning"
        elif extra.get("buypoint_pushable") and not extra.get("observation_only"):
            focus_tier = "conditional"
            focus_label = "🟡 条件买点"
            message.priority = max(int(message.priority), 7)
        else:
            focus_tier = "observation"
            focus_label = "⚡ 异动观察"
            message.priority = min(int(message.priority), 5)

        if not str(message.title).startswith(focus_label):
            message.title = f"{focus_label}｜{message.title}"
        sample_count = _safe_int(evidence.get("sample_count"))
        if sample_count >= max(int(settings.ANOMALY_EVAL_MIN_SAMPLES), 1):
            evidence_text = (
                f"同路由历史{sample_count}次，3日净胜率"
                f"{_safe_float(evidence.get('win_rate_3d')):.0%}，"
                f"平均净超额{_safe_float(evidence.get('avg_excess_return_3d')):+.2f}%"
            )
        else:
            evidence_text = f"同路由历史样本{sample_count}次，仍在积累；本次按结构优先级区分，不虚报胜率"
        classification_lines = [
            "",
            "**🎯 买点分级**",
            f"• 级别: {focus_label}",
            f"• 历史验证: {evidence_text}",
            "",
            "**🧭 炒作预期**",
            f"• 类型: {extra.get('speculation_type') or '综合信号'}",
            f"• 逻辑: {extra.get('speculation_logic') or '量价、资金与板块综合确认'}",
            f"• 持续条件: {extra.get('continuation_condition') or '核心确认继续成立'}",
            f"• 失效条件: {extra.get('invalidation_condition') or '核心确认失效即取消'}",
        ]
        message.content = f"{message.content.rstrip()}\n" + "\n".join(classification_lines)
        extra.update({
            "focus_tier": focus_tier,
            "focus_label": focus_label,
            "focus_score": _push_focus_score(message),
            "performance_evidence_text": evidence_text,
        })
        message.extra = extra
        annotated.append(message)
    annotated.sort(
        key=lambda message: (
            focus_rank.get(str((message.extra or {}).get("focus_tier") or ""), 0),
            _push_focus_score(message),
            int(message.priority),
        ),
        reverse=True,
    )
    return annotated


def _is_early_anomaly_observation(detail: dict | None) -> bool:
    """急拉/抢跑/触支撑属于早期异动，不应直接等同可执行买点。"""
    signal_type = str((detail or {}).get("signal_type") or "")
    return signal_type in {
        "positive_acceleration",
        "underwater_acceleration",
        "underwater_reversal",
        "trend_support_touch",
    }


def _is_observation_only_anomaly(anomaly: dict | None) -> bool:
    detail = (anomaly or {}).get("detail") or {}
    if not _is_early_anomaly_observation(detail):
        return False
    # 支撑“刚到达”和正涨幅急拉始终只是观察；急拉证明动能，不证明
    # 当前价格有赔率。水下反转只有已在盘前形态池或人工重点池中，才允许
    # 沿用A2低吸确认门槛。
    return bool(
        str(detail.get("signal_type") or "")
        in {"trend_support_touch", "positive_acceleration"}
        or not detail.get("detection_pool_member")
    )


def _is_pushworthy_early_observation(anomaly: dict | None) -> bool:
    """早期异动可提醒门槛；只表达“看见异动”，不表达“可以买”。"""
    detail = (anomaly or {}).get("detail") or {}
    signal_type = str(detail.get("signal_type") or "")
    if not _is_observation_only_anomaly(anomaly):
        return False
    if signal_type == "positive_acceleration":
        return bool(
            _intraday_acceleration_volume_ready(detail)
            and not _has_acceleration_distribution_risk(detail)
            and (
                detail.get("positive_acceleration_catchup")
                or detail.get("positive_acceleration_pre_limit_up")
                or detail.get("positive_acceleration_limit_up")
                or str(detail.get("rolling_60s_tier") or "") == "strong"
                or (
                    str(detail.get("rolling_60s_tier") or "") == "medium"
                    and bool(detail.get("rolling_60s_alert_pushable"))
                    and _safe_int(detail.get("positive_acceleration_core_confirmation_count")) >= 2
                )
                or (
                    not detail.get("positive_acceleration_rolling60")
                    and _safe_float(detail.get("acceleration_pct")) >= 1.0
                    and _safe_int(detail.get("positive_acceleration_core_confirmation_count")) >= 3
                )
            )
        )
    if signal_type in {"underwater_acceleration", "underwater_reversal"}:
        return bool(
            _intraday_acceleration_volume_ready(detail)
            and not _has_acceleration_distribution_risk(detail)
            and _safe_int(detail.get("underwater_core_confirmation_count")) >= 3
            and _safe_float(detail.get("close_position")) >= 0.72
            and _has_strong_low_absorb_sector(detail)
        )
    # 触及支撑本身不自动推飞书；页面保留观察，止跌回收后由正式A2接管。
    return False


def _push_event_priority(event_type: str) -> int:
    mapping = {
        "low_absorb": 5,
        "limit_up": 4,
        "breakthrough": 3,
        "capital": 2,
        "limit_down": 1,
    }
    return mapping.get(event_type, 0)


def _sort_push_entries(entries: list[dict]) -> list[dict]:
    return sorted(
        entries,
        key=lambda item: (
            1 if item.get("alert_tier") == "strong" else 0,
            _grade_rank(item.get("setup_grade")),
            _push_event_priority(str(item.get("event_type") or "")),
            _safe_float((item.get("anomaly") or {}).get("score")),
        ),
        reverse=True,
    )


def _describe_push_entry(entry: dict) -> str:
    anomaly = entry.get("anomaly") or {}
    setup_grade = str(entry.get("setup_grade_display") or entry.get("setup_grade") or "").strip()
    signal_label = _event_signal_label(
        str(entry.get("event_type") or ""),
        str(entry.get("signal_label") or "").strip(),
    )
    score = _safe_float(anomaly.get("score"))
    description = str(anomaly.get("description") or "").strip()
    suffix = f" ｜ 评分{score:.0f}" if score > 0 else ""
    if description and description not in signal_label:
        return f"{setup_grade}｜{signal_label}{suffix} ｜ {description}"
    return f"{setup_grade}｜{signal_label}{suffix}"


def _serialize_push_entry(entry: dict) -> dict:
    anomaly = entry.get("anomaly") or {}
    return {
        "code": str(anomaly.get("code") or ""),
        "name": str(anomaly.get("name") or ""),
        "event_type": str(entry.get("event_type") or ""),
        "setup_grade": str(entry.get("setup_grade") or ""),
        "setup_grade_display": str(entry.get("setup_grade_display") or entry.get("setup_grade") or ""),
        "signal_label": _event_signal_label(
            str(entry.get("event_type") or ""),
            str(entry.get("signal_label") or "").strip(),
        ),
        "alert_tier": str(entry.get("alert_tier") or ""),
        "score": _safe_float(anomaly.get("score")),
        "description": str(anomaly.get("description") or ""),
    }


def _kline_chase_reasons(detail: dict) -> list[str]:
    """基于日K边界的追高风险提示，拦截高位动量买点。

    仅依赖 anomaly_scanner `_derive_kline_boundary` 写入 detail 的字段；
    缺字段(如上市不足对应周期)时不误伤，直接放行。
    """
    reasons: list[str] = []
    if "bias_to_ma20_pct" in detail:
        bias_ma20 = _safe_float(detail.get("bias_to_ma20_pct"))
        if bias_ma20 >= settings.ANOMALY_CHASE_MAX_BIAS_MA20_PCT:
            reasons.append(f"现价乖离MA20 {bias_ma20:+.1f}%，短线透支，追入易套")
    if "bias_to_ma60_pct" in detail:
        bias_ma60 = _safe_float(detail.get("bias_to_ma60_pct"))
        if bias_ma60 >= settings.ANOMALY_CHASE_MAX_BIAS_MA60_PCT:
            reasons.append(f"现价乖离MA60 {bias_ma60:+.1f}%，处于中高位")
    if "dist_to_high_60d_pct" in detail:
        dist_high_60d = _safe_float(detail.get("dist_to_high_60d_pct"))
        if dist_high_60d >= settings.ANOMALY_CHASE_NEAR_HIGH_GAP_PCT:
            reasons.append(f"距60日高点仅{abs(dist_high_60d):.1f}%，贴近箱体上沿")
    if "return_5d" in detail:
        return_5d = _safe_float(detail.get("return_5d"))
        if return_5d >= settings.ANOMALY_CHASE_MAX_RETURN_5D_PCT:
            reasons.append(f"近5日累计{return_5d:+.1f}%，短线涨幅过大")
    if "return_20d" in detail:
        return_20d = _safe_float(detail.get("return_20d"))
        if return_20d >= settings.ANOMALY_CHASE_MAX_RETURN_20D_PCT:
            reasons.append(f"近20日累计{return_20d:+.1f}%，处于主升高位")
    return reasons


def _is_chase_type_anomaly(anomaly: dict) -> bool:
    """只对真正的追涨型异动(资金/涨停/普通突破)做K线追高过滤。

    低位修复、联动、事件接力、补涨观察等已自带低位/质量约束，不重复拦截。
    """
    event_type = str(anomaly.get("event_type") or "")
    detail = anomaly.get("detail") or {}
    signal_type = str(detail.get("signal_type") or "")
    if event_type in {"capital", "limit_up", "pump_dump"}:
        return True
    if event_type == "breakthrough":
        return signal_type in {"", "breakthrough", "momentum_burst", "positive_acceleration"}
    return False


def _build_buypoint_continuity_context(entry: dict, peer_events: list[dict] | None = None) -> tuple[bool, list[str]]:
    anomaly = entry.get("anomaly") or {}
    detail = _signal_fund_detail(anomaly)
    event_type = str(entry.get("event_type") or "")
    setup_grade = str(entry.get("setup_grade") or "")
    peer_events = peer_events or []

    reasons: list[str] = []
    turnover = _safe_float(detail.get("turnover"))
    volume_ratio = _safe_float(detail.get("volume_ratio"), 1.0)
    amplitude = _safe_float(detail.get("amplitude"))
    change_pct = _safe_float(detail.get("change_pct"))
    support_strength = _safe_float(detail.get("support_strength_score"))
    seal_quality = _safe_float(detail.get("seal_quality_score"))
    consecutive_days = max(_safe_int(detail.get("consecutive_days"), 1), 1)
    quality_score = _safe_float(detail.get("quality_score"))
    signal_type = str(detail.get("signal_type") or "")
    days_near_pressure = _safe_int(detail.get("days_near_pressure"))
    pullback_probability = str(detail.get("pullback_probability") or "")
    has_positive_sector = _has_positive_driver_sector(detail)
    avg_price = _safe_float(detail.get("avg_price"))
    current_price = _safe_float(detail.get("price"))
    vwap_confirmed = bool(detail.get("vwap_reclaimed")) or (
        avg_price > 0 and current_price >= avg_price * 0.998
    ) or (
        "price_vs_avg_pct" in detail and _safe_float(detail.get("price_vs_avg_pct")) >= -0.2
    )
    has_breakthrough = any(
        str(item.get("event_type") or "") == "breakthrough" and _safe_float(item.get("score")) >= 75
        for item in peer_events
    )
    has_limit_up = any(str(item.get("event_type") or "") == "limit_up" for item in peer_events)
    has_capital = any(
        str(item.get("event_type") or "") == "capital"
        and _build_capital_confirmation_context(item, peer_events) is not None
        for item in peer_events
    )
    allow_early_light_push = setup_grade == "A2 盘口确认后执行" and event_type in {"capital", "breakthrough"}
    is_underwater_reversal = bool(
        event_type == "low_absorb"
        and signal_type in {"underwater_acceleration", "underwater_reversal"}
        and (
            detail.get("underwater_acceleration_confirmed")
            or detail.get("underwater_reversal_confirmed")
        )
    )
    is_positive_acceleration = bool(
        event_type == "breakthrough"
        and signal_type == "positive_acceleration"
        and detail.get("positive_acceleration_confirmed")
    )
    is_trend_support_touch = bool(
        event_type == "low_absorb"
        and signal_type == "trend_support_touch"
        and detail.get("trend_support_touch_confirmed")
    )
    is_main_wave_green_reclaim = bool(
        event_type == "low_absorb"
        and signal_type == "main_wave_green_open_reclaim"
        and detail.get("main_wave_green_open_reclaim_confirmed")
    )
    is_main_wave_pullback_reclaim = bool(
        event_type == "low_absorb"
        and signal_type
        in {"main_wave_shape_pullback_reclaim", "main_wave_support_reclaim"}
        and (
            detail.get("main_wave_shape_pullback_confirmed")
            or detail.get("main_wave_support_reclaim_confirmed")
        )
    )
    is_second_wave_restart = bool(
        event_type == "low_absorb"
        and signal_type == "second_wave_restart"
        and detail.get("second_wave_restart_confirmed")
    )
    is_event_relay_confirmation = bool(
        event_type == "breakthrough"
        and signal_type
        in {"event_relay_confirmation", "second_board_relay_confirmation"}
        and detail.get("event_relay_confirmed")
    )
    is_second_board_relay_confirmation = bool(
        is_event_relay_confirmation
        and signal_type == "second_board_relay_confirmation"
    )
    is_leader_linkage_confirmation = bool(
        event_type == "breakthrough"
        and signal_type == "leader_linkage_confirmation"
        and detail.get("leader_linkage_confirmed")
    )
    is_sector_core_laggard_ignition = bool(
        event_type == "breakthrough"
        and signal_type == "sector_core_laggard_ignition"
        and detail.get("sector_core_laggard_confirmed")
    )

    if _is_recent_high_board_risk(detail) and not (
        is_event_relay_confirmation or is_leader_linkage_confirmation
    ):
        reasons.append("近期高位连板/爆量，次日持续性偏弱")
    is_old_hot_repair = signal_type == "old_hot_oversold_repair"
    if (
        turnover >= 18
        and not is_old_hot_repair
        and not is_underwater_reversal
        and not is_positive_acceleration
        and not is_main_wave_green_reclaim
        and not is_main_wave_pullback_reclaim
    ):
        reasons.append(f"换手 {turnover:.1f}% 偏高，容易次日兑现")
    if (
        amplitude >= 8.5
        and not is_old_hot_repair
        and not is_underwater_reversal
        and not is_positive_acceleration
        and not is_main_wave_green_reclaim
        and not is_main_wave_pullback_reclaim
    ):
        reasons.append(f"振幅 {amplitude:.1f}% 偏大，承接稳定性不足")
    if volume_ratio > 3.2:
        reasons.append(f"量比 {volume_ratio:.1f} 偏高，脉冲特征过强")

    # 安全买入边际：追涨型信号叠加日K高位(乖离MA过大/近N日涨幅过大/贴近箱体上沿)
    # 时直接拦截，避免把主升高位当买点推送。总开关关闭即回退旧算法，用于灰度对比。
    if settings.ANOMALY_CHASE_FILTER_ENABLED and _is_chase_type_anomaly(anomaly):
        reasons.extend(_kline_chase_reasons(detail))

    if event_type == "limit_up":
        if consecutive_days >= 2:
            reasons.append("连板股更偏博弈，不纳入默认买点提醒")
        if support_strength < 62 or seal_quality < 75:
            reasons.append("封单/承接质量不足")
        if not (has_positive_sector or has_breakthrough or has_capital):
            reasons.append("缺少板块或资金共振，持续性不足")
    elif event_type == "capital":
        inflow_pct = _safe_float(detail.get("main_net_inflow_pct"))
        sector_ready = has_positive_sector or (allow_early_light_push and has_breakthrough)
        min_support_strength = 58 if allow_early_light_push else 62
        min_inflow_pct = 5.0 if allow_early_light_push else 6.0
        max_change_pct = 6.4 if allow_early_light_push else 5.6
        min_volume_ratio = 1.3 if allow_early_light_push else 1.5
        max_volume_ratio = 3.1 if allow_early_light_push else 2.8
        if not sector_ready:
            reasons.append("板块主驱动偏弱，资金异动持续性不足")
        if support_strength < min_support_strength:
            reasons.append("盘口承接不够强")
        if inflow_pct < min_inflow_pct:
            reasons.append("主力净额占成交额比偏低")
        if change_pct > max_change_pct:
            reasons.append("当日涨幅过大，次日兑现风险偏高")
        if volume_ratio < min_volume_ratio or volume_ratio > max_volume_ratio:
            reasons.append("量能不在健康区间")
        if not vwap_confirmed:
            reasons.append("尚未站稳VWAP，资金异动不推送买点")
    elif event_type == "breakthrough":
        if is_sector_core_laggard_ignition:
            confirmations = list(detail.get("sector_core_laggard_confirmations") or [])
            driver = detail.get("sector_core_driver") or {}
            if _safe_float(driver.get("strength_score")) < 70:
                reasons.append("主线强度不足70")
            if _safe_float(driver.get("fund_flow")) <= 0:
                reasons.append("主线资金已经转负")
            if _safe_int(driver.get("limit_up_count")) < 3:
                reasons.append("主线涨停扩散不足3家")
            if not _intraday_acceleration_volume_ready(detail):
                reasons.append("60秒拉升没有真实增量成交")
            if _safe_float(detail.get("rolling_60s_change_pct")) < settings.ANOMALY_ROLLING_60S_MIN_CHANGE_PCT:
                reasons.append("60秒价格动能不足")
            if not bool(detail.get("rolling_60s_path_confirmed", True)):
                reasons.append("窗口末端已经冲高回落")
            if not (-2.5 <= change_pct <= 4.8):
                reasons.append("已离开主线补涨的前置区间")
            if not (1.15 <= volume_ratio <= 3.2):
                reasons.append("量能不在健康点火区间")
            if amplitude > 8.0 or turnover > 15.0:
                reasons.append("振幅或换手过热")
            if not has_positive_sector:
                reasons.append("主线实时扩散已经失效")
            if not vwap_confirmed:
                reasons.append("尚未回到VWAP附近")
            if _has_acceleration_distribution_risk(detail):
                reasons.append("急拉伴随撤单或卖盘压制")
            if _safe_int(detail.get("sector_core_confirmation_count")) < 2:
                reasons.append("板块、盘口、资金确认不足两项")
            if support_strength < 65 and _safe_float(detail.get("main_net_inflow")) <= 0:
                reasons.append("盘口承接或实时资金不足")
            if len(confirmations) < 4:
                reasons.append("主线补涨确认项不足")
            return len(reasons) == 0, reasons
        if is_leader_linkage_confirmation:
            confirmations = list(detail.get("leader_linkage_confirmations") or [])
            if _safe_float(detail.get("leader_recognition_score")) < 68:
                reasons.append("龙头A辨识度不足")
            if _safe_float(detail.get("linkage_score")) < 72:
                reasons.append("A/B联动强度不足")
            if _safe_float(detail.get("business_relevance_score")) < 76:
                reasons.append("A/B主营业务不属于同一核心产业链")
            if _safe_float(detail.get("follower_shape_score")) < 68:
                reasons.append("B自身K线形态偏弱")
            if _safe_float(detail.get("theme_alignment_score")) < 75:
                reasons.append("龙头涨停归因与联动主题不一致")
            if len(confirmations) < 6:
                reasons.append("A稳定与B量价确认不足")
            if not bool(detail.get("leader_near_high")):
                reasons.append("龙头A已离开日内强势区")
            if not has_positive_sector:
                reasons.append("关联板块未保持正向扩散")
            if not vwap_confirmed:
                reasons.append("B尚未站稳VWAP")
            if support_strength < 60 and _safe_float(detail.get("main_net_inflow")) <= 0:
                reasons.append("B盘口承接/实时资金不足")
            return len(reasons) == 0, reasons
        if is_event_relay_confirmation:
            confirmations = list(detail.get("event_relay_confirmations") or [])
            if (
                is_second_board_relay_confirmation
                and _safe_float(detail.get("relay_quality_score")) < 68
            ):
                reasons.append("首板晋级质量不足")
            elif (
                not is_second_board_relay_confirmation
                and _safe_float(detail.get("event_score")) < 58
            ):
                reasons.append("事件硬度不足")
            if len(confirmations) < 6:
                reasons.append("竞价、换手、盘口和板块确认不足")
            if not has_positive_sector:
                reasons.append("事件票缺少板块前排共振")
            if not vwap_confirmed:
                reasons.append("事件票尚未站稳VWAP")
            if support_strength < 60 and _safe_float(detail.get("main_net_inflow")) <= 0:
                reasons.append("事件票盘口承接/实时资金不足")
            return len(reasons) == 0, reasons
        if is_positive_acceleration:
            is_rolling_60s = bool(detail.get("positive_acceleration_rolling60"))
            is_limit_up_acceleration = bool(
                detail.get("positive_acceleration_limit_up")
            )
            is_pre_limit_up_acceleration = bool(
                detail.get("positive_acceleration_pre_limit_up")
            )
            is_catchup = bool(
                detail.get("positive_acceleration_catchup")
                or is_pre_limit_up_acceleration
                or is_limit_up_acceleration
            )
            if is_limit_up_acceleration:
                max_change_pct = (
                    settings.ANOMALY_POSITIVE_ACCELERATION_LIMIT_UP_MAX_CHANGE_PCT
                )
                min_momentum_pct = (
                    settings.ANOMALY_POSITIVE_ACCELERATION_LIMIT_UP_MIN_MOMENTUM_PCT
                )
            elif is_pre_limit_up_acceleration:
                max_change_pct = (
                    settings.ANOMALY_POSITIVE_ACCELERATION_LIMIT_UP_MAX_CHANGE_PCT
                )
                min_momentum_pct = (
                    settings.ANOMALY_POSITIVE_ACCELERATION_PRE_LIMIT_MIN_MOMENTUM_PCT
                )
            elif is_catchup:
                max_change_pct = (
                    settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MAX_CHANGE_PCT
                )
                min_momentum_pct = (
                    settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MIN_MOMENTUM_PCT
                )
            else:
                max_change_pct = settings.ANOMALY_POSITIVE_ACCELERATION_MAX_CHANGE_PCT
                min_momentum_pct = (
                    settings.ANOMALY_ROLLING_60S_MIN_CHANGE_PCT
                    if is_rolling_60s
                    else settings.ANOMALY_POSITIVE_ACCELERATION_MIN_MOMENTUM_PCT
                )
            max_amplitude = (
                settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MAX_AMPLITUDE
                if is_catchup
                else 9.0
            )
            max_turnover = (
                settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MAX_TURNOVER
                if is_catchup
                else 18.0
            )
            acceleration_pct = (
                _safe_float(detail.get("rolling_60s_change_pct"))
                if is_rolling_60s
                else _safe_float(detail.get("scan_change_pct"))
            )
            if not (
                (
                    settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_CHANGE_PCT
                    if is_rolling_60s
                    else settings.ANOMALY_POSITIVE_ACCELERATION_MIN_CHANGE_PCT
                )
                <= change_pct
                <= max_change_pct
            ):
                reasons.append("已离开红盘二次加速提醒窗口")
            if acceleration_pct < min_momentum_pct:
                reasons.append("短周期加速动能已经回落")
            if not (
                (
                    settings.ANOMALY_ROLLING_60S_MIN_VOLUME_RATIO
                    if is_rolling_60s
                    else settings.ANOMALY_POSITIVE_ACCELERATION_MIN_VOLUME_RATIO
                )
                <= volume_ratio
                <= (
                    settings.ANOMALY_ROLLING_60S_MAX_VOLUME_RATIO
                    if is_rolling_60s
                    else settings.ANOMALY_POSITIVE_ACCELERATION_MAX_VOLUME_RATIO
                )
            ):
                reasons.append("二次加速量能不在健康区间")
            if (
                not is_rolling_60s
                and _safe_float(detail.get("near_high_ratio"))
                < settings.ANOMALY_POSITIVE_ACCELERATION_MIN_NEAR_HIGH_RATIO
            ):
                reasons.append("拉升后已经离开日内高位区")
            if is_rolling_60s and not bool(
                detail.get("rolling_60s_path_confirmed", True)
            ):
                reasons.append("拉升末端已冲高回落，未保持窗口高位")
            if _safe_float(detail.get("price_vs_avg_pct")) < (-0.8 if is_rolling_60s else 0.0):
                reasons.append("尚未站稳VWAP附近")
            if amplitude > max_amplitude:
                reasons.append("爆拉振幅过大，已超出追赶提醒窗口")
            if turnover > max_turnover:
                reasons.append("爆拉换手过热，筹码分歧过大")
            if not _intraday_acceleration_volume_ready(detail):
                reasons.append("拉升窗口没有真实增量成交确认")
            if _has_acceleration_distribution_risk(detail):
                reasons.append("拉升伴随撤单或卖盘压制，疑似诱多")
            if _safe_int(detail.get("positive_acceleration_core_confirmation_count")) < 2:
                reasons.append("资金、盘口、板块共振不足两项")
            if support_strength < 60 and _safe_float(detail.get("main_net_inflow")) <= 0:
                reasons.append("盘口承接或实时资金确认不足")
            return len(reasons) == 0, reasons
        is_sector_repair_reversal = bool(
            detail.get("sector_repair_confirmed")
            and str(detail.get("signal_type") or "") == "sector_repair_reversal"
        )
        is_old_hot_oversold_repair = bool(
            detail.get("old_hot_repair_confirmed")
            and str(detail.get("signal_type") or "") == "old_hot_oversold_repair"
        )
        is_trend_driver_breakthrough = bool(
            detail.get("trend_setup_confirmed")
            and str(detail.get("signal_type") or "") == "trend_driver_breakthrough"
        )
        if is_old_hot_oversold_repair:
            driver = detail.get("old_hot_repair_driver") or {}
            strong_extension_confirmed = bool(detail.get("strong_extension_confirmed"))
            if not (
                _safe_float(driver.get("fund_flow")) > 0
                and _safe_float(driver.get("change_pct")) >= 1.0
                and _safe_float(driver.get("strength_score")) >= 60
                and (
                    _safe_int(driver.get("limit_up_count")) >= 2
                    or _safe_int(driver.get("peer_count")) >= 2
                )
            ):
                reasons.append("旧高标修复缺少板块带动")
            if quality_score < 84:
                reasons.append("旧高标修复质量不足 84")
            if change_pct < 3.5 or change_pct > (9.3 if strong_extension_confirmed else 8.6):
                reasons.append("旧高标修复涨幅已离开首日确认区间")
            if volume_ratio < 0.8 or volume_ratio > (2.6 if strong_extension_confirmed else 2.4):
                reasons.append("旧高标修复量能不在温和区间")
            if turnover < 4.0 or turnover > 18.0 or amplitude > (12.0 if strong_extension_confirmed else 9.5):
                reasons.append("旧高标修复换手或振幅不合适")
            if _safe_int(detail.get("historical_board_like_count")) < 2:
                reasons.append("历史高标记忆不足")
            if _safe_int(detail.get("last_board_age")) < 8:
                reasons.append("距离上一涨停过近，属于追高而非超跌修复")
            if _safe_float(detail.get("return_20d")) > -15.0:
                reasons.append("前20日回撤不足，不属于深跌修复")
            if _safe_float(detail.get("position_120")) > 0.45:
                reasons.append("120日位置过高")
            if not (detail.get("near_intraday_high") and detail.get("vwap_reclaimed")):
                reasons.append("尚未同时确认日内强势和VWAP回收")
        elif is_sector_repair_reversal:
            if not has_positive_sector:
                reasons.append("强修复板块共振已经失效")
            if quality_score < 82:
                reasons.append("强修复启动质量不足 82")
            if change_pct < 1.5 or change_pct > 6.2:
                reasons.append("强修复启动涨幅已离开可执行区间")
            if volume_ratio < 0.85 or volume_ratio > 2.8:
                reasons.append("强修复启动量能不在健康区间")
            if turnover > 13.0 or amplitude > 7.5:
                reasons.append("换手或振幅过热，不适合反包跟随")
            if not (detail.get("near_intraday_high") or detail.get("vwap_reclaimed")):
                reasons.append("尚未确认日内强势或VWAP回收")
            if _safe_float(detail.get("return_20d")) > -6.0:
                reasons.append("不属于低位超跌修复结构")
        else:
            driver_ready = has_positive_sector or (allow_early_light_push and has_capital)
            min_quality_score = 78 if allow_early_light_push else 80
            min_days_near_pressure = 2 if allow_early_light_push else 3
            min_support_strength = 56 if allow_early_light_push else 60
            if not driver_ready:
                reasons.append("缺少板块主驱动")
            if quality_score < min_quality_score:
                reasons.append("突破质量不足 80")
            # 趋势驱动池已在盘后用完整日K验证蓄势结构；盘中只校验突破质量，
            # 不再复用旧突破检测器的 days_near_pressure 字段重复拦截。
            if not is_trend_driver_breakthrough and days_near_pressure < min_days_near_pressure:
                reasons.append("压力位蓄势时间不足")
            if pullback_probability != "low" and setup_grade == "A1 可直接执行":
                reasons.append("回踩概率不是低位，不适合直推")
            if support_strength < min_support_strength:
                reasons.append("盘口承接不够强")
            if not vwap_confirmed:
                reasons.append("突破后尚未站稳VWAP")
    elif event_type == "low_absorb":
        low_absorb_type = str(detail.get("low_absorb_type") or "")
        distance_to_ma5_pct = _safe_float(detail.get("distance_to_ma5_pct"))
        intraday_rebound_pct = _safe_float(detail.get("intraday_rebound_pct"))
        price_vs_avg_pct = _safe_float(detail.get("price_vs_avg_pct"))
        net_amount = _safe_float(detail.get("main_net_inflow"))
        inflow_pct = _safe_float(detail.get("main_net_inflow_pct"))
        strong_sector = _has_strong_low_absorb_sector(detail)
        if is_second_wave_restart:
            reset_stats = detail.get("second_wave_reset_stats") or {}
            reset_drawdown_pct = _safe_float(reset_stats.get("drawdown_pct"))
            acceleration_pct = max(
                _safe_float(detail.get("acceleration_pct")),
                _safe_float(detail.get("min5_change")),
                _safe_float(detail.get("scan_change_pct")),
            )
            if not (
                3 <= _safe_int(reset_stats.get("days_since_peak")) <= 15
                and -55.0 <= reset_drawdown_pct <= -12.0
            ):
                reasons.append("首波下杀记忆已离开二波窗口")
            if not (-2.8 <= change_pct <= 6.5):
                reasons.append("已离开二波启动的前置提醒区间")
            if not (1.05 <= volume_ratio <= 3.2):
                reasons.append("二波启动量能不在健康区间")
            if not (
                acceleration_pct >= 0.65
                or _safe_float(detail.get("intraday_rebound_pct")) >= 2.5
            ):
                reasons.append("止跌急拉动能不足")
            if not detail.get("near_intraday_high"):
                reasons.append("拉升后未保持在日内高位区")
            if not _intraday_acceleration_volume_ready(detail):
                reasons.append("二波急拉没有真实增量成交确认")
            if _has_acceleration_distribution_risk(detail):
                reasons.append("二波急拉伴随撤单或卖盘压制")
            if _safe_float(detail.get("price_vs_avg_pct")) < -0.8:
                reasons.append("仍明显低于VWAP")
            if _safe_int(detail.get("second_wave_core_confirmation_count")) < 2:
                reasons.append("盘口、资金、板块共振不足两项")
            if support_strength < 65 and _safe_float(detail.get("main_net_inflow")) <= 0:
                reasons.append("盘口承接或实时资金确认不足")
            return len(reasons) == 0, reasons
        if is_trend_support_touch:
            support_gap_pct = _safe_float(detail.get("support_gap_pct"))
            from_low_pct = _safe_float(detail.get("from_intraday_low_pct"))
            min5_change = _safe_float(detail.get("min5_change"))
            if not (
                settings.ANOMALY_TREND_SUPPORT_TOUCH_MIN_GAP_PCT
                <= support_gap_pct
                <= settings.ANOMALY_TREND_SUPPORT_TOUCH_MAX_GAP_PCT
            ):
                reasons.append("现价已离开趋势支撑到达区")
            if from_low_pct > settings.ANOMALY_TREND_SUPPORT_TOUCH_MAX_FROM_LOW_PCT:
                reasons.append("已从日内低点回升，等待支撑回收信号接管")
            if min5_change < settings.ANOMALY_TREND_SUPPORT_TOUCH_MIN_MIN5_CHANGE_PCT:
                reasons.append("仍在加速下跌，尚不能低吸")
            if not (0.65 <= volume_ratio <= 1.9):
                reasons.append("支撑附近量能不在健康区间")
            if _safe_int(detail.get("support_touch_core_confirmation_count")) < 2:
                reasons.append("支撑触及时盘口、资金、板块共振不足")
            if support_strength < 65 and _safe_float(detail.get("main_net_inflow")) <= 0:
                reasons.append("支撑位缺少盘口承接或实时资金确认")
            if (
                not bool(detail.get("is_stale"))
                and (
                    _safe_float(detail.get("main_net_inflow")) <= -1e8
                    or _safe_float(detail.get("main_net_inflow_pct")) <= -4.0
                )
            ):
                reasons.append("主力明显流出，支撑触及不能作为低吸提醒")
            return len(reasons) == 0, reasons
        if is_main_wave_green_reclaim:
            if not (
                settings.ANOMALY_MAIN_WAVE_GREEN_OPEN_LOWER_PCT
                <= _safe_float(detail.get("open_change_pct"))
                <= settings.ANOMALY_MAIN_WAVE_GREEN_OPEN_UPPER_PCT
            ):
                reasons.append("开盘未处于主升浪绿开错杀区间")
            if (
                _safe_float(detail.get("low_drop_pct"))
                > settings.ANOMALY_MAIN_WAVE_GREEN_MIN_LOW_DROP_PCT
            ):
                reasons.append("日内下探幅度不足，不能确认绿开下杀路径")
            if not (-3.5 <= change_pct <= 1.8):
                reasons.append("已离开主升浪绿开回收提醒窗口")
            if (
                _safe_float(detail.get("intraday_rebound_pct"))
                < settings.ANOMALY_MAIN_WAVE_GREEN_MIN_REBOUND_PCT
            ):
                reasons.append("从日内低点回抽不足")
            if (
                _safe_float(detail.get("short_momentum_pct"))
                < settings.ANOMALY_MAIN_WAVE_GREEN_MIN_MOMENTUM_PCT
            ):
                reasons.append("短周期止跌回收动能不足")
            if (
                _safe_float(detail.get("close_position"))
                < settings.ANOMALY_MAIN_WAVE_GREEN_MIN_CLOSE_POSITION
            ):
                reasons.append("现价未保持在日内区间上部")
            if (
                _safe_float(detail.get("pullback_from_high_pct"))
                < -settings.ANOMALY_MAIN_WAVE_GREEN_MAX_PULLBACK_FROM_HIGH_PCT
            ):
                reasons.append("回收后再次明显回落")
            if price_vs_avg_pct < -0.8:
                reasons.append("仍明显低于VWAP")
            if not (
                settings.ANOMALY_MAIN_WAVE_GREEN_MIN_VOLUME_RATIO
                <= volume_ratio
                <= settings.ANOMALY_MAIN_WAVE_GREEN_MAX_VOLUME_RATIO
            ):
                reasons.append("绿开回收量能不在健康区间")
            if (
                amplitude > settings.ANOMALY_MAIN_WAVE_GREEN_MAX_AMPLITUDE
                or turnover > settings.ANOMALY_MAIN_WAVE_GREEN_MAX_TURNOVER
            ):
                reasons.append("绿开回收振幅或换手过热")
            if not _intraday_acceleration_volume_ready(detail):
                reasons.append("止跌回收没有同窗口增量成交确认")
            if _has_acceleration_distribution_risk(detail):
                reasons.append("止跌回收伴随撤单或卖盘压制")
            if _safe_int(detail.get("main_wave_core_confirmation_count")) < 2:
                reasons.append("盘口、资金、板块共振不足两项")
            if support_strength < 65 and net_amount <= 0:
                reasons.append("盘口承接或实时资金确认不足")
            if (
                not bool(detail.get("is_stale"))
                and (net_amount <= -1e8 or inflow_pct <= -4.0)
            ):
                reasons.append("主力明显流出，不能把反抽当低吸买点")
            return len(reasons) == 0, reasons
        if is_main_wave_pullback_reclaim:
            anchor_low = _safe_float(detail.get("pullback_anchor_low"))
            support_break_pct = _safe_float(detail.get("support_break_pct"))
            rolling_change_pct = _safe_float(detail.get("rolling_60s_change_pct"))
            if anchor_low <= 0:
                reasons.append("缺少盘前主升支撑锚点")
            if support_break_pct < -settings.ANOMALY_MAIN_WAVE_PULLBACK_MAX_SUPPORT_BREAK_PCT:
                reasons.append("已有效跌破主升回踩支撑")
            if not (-4.5 <= change_pct <= 1.8):
                reasons.append("已离开主升回踩低吸窗口")
            if intraday_rebound_pct < settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_REBOUND_PCT:
                reasons.append("触及形态低点后回拉不足")
            if rolling_change_pct < settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_MOMENTUM_PCT:
                reasons.append("滚动60秒止跌动能不足")
            if not (
                settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_VOLUME_RATIO
                <= volume_ratio
                <= settings.ANOMALY_MAIN_WAVE_PULLBACK_MAX_VOLUME_RATIO
            ):
                reasons.append("回踩回收量能不在健康区间")
            if not _intraday_acceleration_volume_ready(detail):
                reasons.append("回拉没有滚动60秒增量成交确认")
            if _has_acceleration_distribution_risk(detail):
                reasons.append("回拉伴随撤单或卖盘压制")
            if _safe_int(detail.get("main_wave_core_confirmation_count")) < 2:
                reasons.append("盘口、资金、板块共振不足两项")
            if support_strength < 65 and net_amount <= 0:
                reasons.append("盘口承接或实时资金确认不足")
            if not bool(detail.get("is_stale")) and (net_amount <= -1e8 or inflow_pct <= -4.0):
                reasons.append("主力明显流出，不能把回抽当买点")
            return len(reasons) == 0, reasons
        if is_underwater_reversal:
            is_underwater_acceleration = signal_type == "underwater_acceleration"
            low_drop_pct = _safe_float(detail.get("low_drop_pct"))
            close_position = _safe_float(detail.get("close_position"))
            pullback_from_high_pct = _safe_float(detail.get("pullback_from_high_pct"))
            min5_change = _safe_float(detail.get("min5_change"))
            scan_change_pct = _safe_float(detail.get("scan_change_pct"))
            core_confirmation_count = _safe_int(detail.get("underwater_core_confirmation_count"))
            orderbook_confirmed = bool(
                support_strength >= 60
                and _safe_float(detail.get("orderbook_imbalance")) >= 0.05
                and _safe_float(detail.get("bid_depth_5"))
                > _safe_float(detail.get("ask_depth_5")) * 1.03
            )
            if low_drop_pct > -settings.ANOMALY_UNDERWATER_REVERSAL_MIN_LOW_DROP_PCT:
                reasons.append("日内水下幅度不足，不能确认反转路径")
            min_change_pct = (
                settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_CHANGE_PCT
                if is_underwater_acceleration
                else settings.ANOMALY_UNDERWATER_REVERSAL_MIN_CHANGE_PCT
            )
            max_change_pct = (
                settings.ANOMALY_UNDERWATER_REVERSAL_MIN_CHANGE_PCT
                if is_underwater_acceleration
                else settings.ANOMALY_UNDERWATER_REVERSAL_MAX_CHANGE_PCT
            )
            if not (min_change_pct <= change_pct <= max_change_pct):
                reasons.append("已离开水下翻红的前置提醒窗口")
            min_volume_ratio = (
                settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_VOLUME_RATIO
                if is_underwater_acceleration
                else settings.ANOMALY_UNDERWATER_REVERSAL_MIN_VOLUME_RATIO
            )
            if not (
                min_volume_ratio
                <= volume_ratio
                <= settings.ANOMALY_UNDERWATER_REVERSAL_MAX_VOLUME_RATIO
            ):
                reasons.append("水下反转量能不在健康区间")
            if amplitude > settings.ANOMALY_UNDERWATER_REVERSAL_MAX_AMPLITUDE:
                reasons.append("水下反转振幅过大，容易冲高回落")
            if turnover > settings.ANOMALY_UNDERWATER_REVERSAL_MAX_TURNOVER:
                reasons.append("水下反转换手过热")
            if (
                is_underwater_acceleration
                and price_vs_avg_pct
                < settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_VWAP_GAP_PCT
            ):
                reasons.append("急拉仍明显低于VWAP")
            elif not is_underwater_acceleration and (
                not vwap_confirmed or price_vs_avg_pct < 0.0
            ):
                reasons.append("尚未站稳VWAP")
            if close_position < settings.ANOMALY_UNDERWATER_REVERSAL_MIN_CLOSE_POSITION:
                reasons.append("现价未保持在日内高位区")
            if pullback_from_high_pct <= -settings.ANOMALY_UNDERWATER_REVERSAL_MAX_PULLBACK_FROM_HIGH_PCT:
                reasons.append("拉升后已明显回落")
            if not _intraday_acceleration_volume_ready(detail):
                reasons.append("水下急拉没有真实增量成交确认")
            if _has_acceleration_distribution_risk(detail):
                reasons.append("水下急拉伴随撤单或卖盘压制，疑似诱多")
            acceleration_momentum_ready = bool(
                intraday_rebound_pct
                >= settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_REBOUND_PCT
                and max(min5_change, scan_change_pct)
                >= settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_MOMENTUM_PCT
            )
            reversal_momentum_ready = bool(
                min5_change >= 0.45
                or scan_change_pct >= 0.45
                or detail.get("crossed_from_underwater")
                or intraday_rebound_pct >= 2.6
            )
            if not (
                acceleration_momentum_ready
                if is_underwater_acceleration
                else reversal_momentum_ready
            ):
                reasons.append("短周期反转动能尚未确认")
            if not orderbook_confirmed and _safe_float(detail.get("main_net_inflow")) <= 0:
                reasons.append("盘口承接或实时资金确认不足")
            if core_confirmation_count < 2:
                reasons.append("资金、盘口、板块共振不足两项")
            return len(reasons) == 0, reasons
        is_low_base_watchlist = _is_low_base_watchlist_anomaly(anomaly)
        ma20_slope_5d = _safe_float(detail.get("ma20_slope_5d"))
        return_20d = _safe_float(detail.get("return_20d"))
        ma5_ready = "ma5_pullback" in low_absorb_type and -1.3 <= distance_to_ma5_pct <= 2.6
        green_ready = "green_reversal" in low_absorb_type and intraday_rebound_pct >= 0.8
        trend_support_ready = bool(
            detail.get("trend_setup_confirmed")
            and low_absorb_type == "trend_driver_support_reclaim"
            and -1.5 <= _safe_float(detail.get("support_gap_pct")) <= 3.0
            and intraday_rebound_pct >= 0.8
        )
        if not (ma5_ready or green_ready or trend_support_ready):
            reasons.append("未形成MA5回踩或绿盘弱转强结构")
        if change_pct > 2.8 or change_pct < -3.5:
            reasons.append("低吸涨跌幅区间不合适")
        if volume_ratio < 0.75 or volume_ratio > 2.6:
            reasons.append("量能不在低吸健康区间")
        if amplitude > 6.8:
            reasons.append("振幅偏大，低吸承接不稳定")
        if turnover > 14.5:
            reasons.append("换手偏高，低吸可能变成分歧接力")
        if not is_low_base_watchlist and "ma20_slope_5d" in detail and ma20_slope_5d < 0:
            reasons.append("MA20仍在下行，趋势持续性不足")
        if not is_low_base_watchlist and "return_20d" in detail and not (0 <= return_20d <= 25):
            reasons.append("20日涨幅不在低位趋势健康区间")
        if support_strength < 60 and net_amount <= 0:
            reasons.append("盘口承接或资金确认不足")
        if price_vs_avg_pct < -0.4:
            reasons.append("尚未修复VWAP")
        if (net_amount <= -1e8 or inflow_pct <= -4.0) and not (strong_sector and support_strength >= 78):
            reasons.append("主力明显流出，低吸确认不足")
        if not (strong_sector or net_amount > 0 or inflow_pct >= 3.0):
            reasons.append("缺少板块或资金确认")
        if detail.get("tenbagger_pullback_confirmed"):
            if _safe_float(detail.get("tenbagger_score")) < 72.0:
                reasons.append("牛股潜力评分不足72")
            if change_pct > 1.8:
                reasons.append("已离开潜力股低吸窗口，不能追高")
            if not _intraday_acceleration_volume_ready(detail):
                reasons.append("潜力股回收缺少同窗60秒增量成交")
            if not vwap_confirmed:
                reasons.append("潜力股回收尚未修复VWAP")
            if _has_acceleration_distribution_risk(detail):
                reasons.append("潜力股回收伴随撤单或卖盘压制")
    elif event_type == "limit_down":
        if not has_positive_sector and not has_capital:
            reasons.append("缺少资金或板块反核共振")
        if support_strength < 78:
            reasons.append("承接强度不足 78")

    return len(reasons) == 0, reasons


def _merge_push_message_for_code(
    primary_entry: dict,
    current_entries: list[dict],
    changed_entries: list[dict],
):
    base_message = primary_entry.get("message")
    if base_message is None:
        return None

    alert_tier = "strong" if any(item.get("alert_tier") == "strong" for item in changed_entries) else "light"
    message = PushMessage(
        title=str(base_message.title),
        content=str(base_message.content),
        msg_type=str(base_message.msg_type),
        category=str(base_message.category),
        stock_code=str(base_message.stock_code),
        stock_name=str(base_message.stock_name),
        priority=int(base_message.priority),
        extra=dict(base_message.extra or {}),
    )

    if alert_tier == "light":
        message.title = f"💡 轻提醒｜{message.title}"
        message.msg_type = "info"
        message.priority = max(4, min(message.priority, 7) - 1)

    current_primary = _sort_push_entries(current_entries)[0] if current_entries else primary_entry
    current_primary_identity = str(current_primary.get("signal_identity") or "")
    primary_identity = str(primary_entry.get("signal_identity") or "")

    secondary_entries = [
        item for item in _sort_push_entries(current_entries)
        if str(item.get("signal_identity") or "") not in {current_primary_identity, primary_identity}
    ]

    merged_lines = [message.content, "", "**🧩 同股信号联动**"]
    merged_lines.append(f"提醒档位: {'强提醒' if alert_tier == 'strong' else '轻提醒'}")
    merged_lines.append("本次变化:")
    merged_lines.extend(
        f"• {_describe_push_entry(item)}"
        for item in _sort_push_entries(changed_entries)[:4]
    )
    if current_primary_identity and current_primary_identity != primary_identity:
        merged_lines.append(f"当前主信号: {_describe_push_entry(current_primary)}")
    if secondary_entries:
        merged_lines.append("同股副信号:")
        merged_lines.extend(
            f"• {_describe_push_entry(item)}"
            for item in secondary_entries[:4]
        )
    continuity_reasons = list((primary_entry.get("continuity_reasons") or []))
    if continuity_reasons:
        merged_lines.append("持续性观察:")
        merged_lines.extend(f"• {reason}" for reason in continuity_reasons[:3])

    message.content = "\n".join(merged_lines)
    message.extra.update(
        {
            "alert_tier": alert_tier,
            "changed_signals": [_serialize_push_entry(item) for item in _sort_push_entries(changed_entries)],
            "current_primary_signal": _serialize_push_entry(current_primary) if current_primary else {},
            "secondary_signals": [_serialize_push_entry(item) for item in secondary_entries],
            "merged_signal_count": len(current_entries),
            "continuity_reasons": continuity_reasons,
        }
    )
    return message


def _has_positive_driver_sector(detail: dict) -> bool:
    sector_factors = detail.get("sector_factors") or []
    return any(
        _is_causal_trade_driver_sector(item)
        and _safe_float(item.get("fund_flow")) > 0
        and _safe_float(item.get("change_pct")) > 0
        and (
            _safe_float(item.get("strength_score")) >= 45
            or _safe_int(item.get("limit_up_count")) >= 2
            or (
                "strength_score" not in item
                and "limit_up_count" not in item
                and _safe_float(item.get("fund_flow")) >= 5
                and _safe_float(item.get("change_pct")) >= 1.0
            )
        )
        for item in sector_factors
    )


def _has_strong_low_absorb_sector(detail: dict) -> bool:
    """低吸买点要求板块不是微涨陪跑，至少有强度或涨停家数确认。"""
    for item in detail.get("sector_factors") or []:
        if (
            _is_causal_trade_driver_sector(item)
            and _safe_float(item.get("fund_flow")) > 0
            and _safe_float(item.get("change_pct")) > 0
            and (
                _safe_float(item.get("strength_score")) >= 45
                or _safe_int(item.get("limit_up_count")) >= 2
            )
        ):
            return True
    return False


def _is_recent_high_board_risk(detail: dict) -> bool:
    max_recent_consecutive_days = _safe_int(detail.get("max_recent_consecutive_days"))
    turnover = _safe_float(detail.get("turnover"))
    volume_ratio = _safe_float(detail.get("volume_ratio"), 1.0)
    amplitude = _safe_float(detail.get("amplitude"))
    change_pct = _safe_float(detail.get("change_pct"))
    last_break_count = _safe_int(detail.get("last_break_count"))
    if max_recent_consecutive_days < 4:
        return False
    if turnover >= 18 and volume_ratio >= 3.0:
        return True
    if turnover >= 15 and amplitude >= 5.0 and change_pct < 5.0:
        return True
    if last_break_count >= 1 and turnover >= 15:
        return True
    return False


def _has_qualified_main_fund_source(detail: dict) -> bool:
    from app.data.main_fund import anomaly_main_fund_evidence

    return anomaly_main_fund_evidence(detail, decision_at=datetime.now()) is not None


def _signal_fund_detail(anomaly: dict) -> dict:
    """Owned numeric projection from ONE frozen frame; preserve quote clocks."""
    from app.data.main_fund import anomaly_main_fund_evidence, fund_order_breakdown

    detail = dict(anomaly.get("detail") or {})
    fund = anomaly_main_fund_evidence(
        detail, code=anomaly.get("code"), decision_at=datetime.now(),
    )
    detail.update({
        "main_net_inflow": (fund or {}).get("main_net_inflow"),
        "main_net_inflow_pct": (fund or {}).get("main_net_inflow_pct"),
        **fund_order_breakdown(fund or {}),
        "is_stale": fund is None,
        "fund_data_degraded": fund is None,
        "fund_as_of": str(fund["source_quote_at"]) if fund else None,
    })
    if fund:
        detail.update(source=("fund_flow" if fund["source"] == "tencent" else "eastmoney_main_fund"),
                      provider_source=fund["source"], source_version=fund["source_version"])
    else:
        detail["source"] = "unavailable"
    if "large_order_inflow_confirmed" in detail:
        large_order = _build_large_order_inflow_context(
            fund or {"is_stale": True}, traded_amount=_safe_float(detail.get("amount")),
        )
        large_order["large_order_inflow_confirmed"] = bool(
            detail["large_order_inflow_confirmed"] and large_order["large_order_inflow_confirmed"]
        )
        detail.update(large_order)
    return detail


def _build_capital_confirmation_context(anomaly: dict, peer_events: list[dict] | None = None) -> dict | None:
    detail = _signal_fund_detail(anomaly)
    source = str(detail.get("source") or "")
    as_of = str(detail.get("fund_as_of") or "")
    is_stale = bool(detail.get("is_stale"))
    if source not in {"eastmoney_main_fund", "fund_flow"} or is_stale:
        return None

    peer_events = peer_events or []
    net_amount = _safe_float(detail.get("main_net_inflow"))
    inflow_pct = _safe_float(detail.get("main_net_inflow_pct"))
    super_net_amount = _safe_float(detail.get("super_net_inflow"))
    super_inflow_pct = _safe_float(detail.get("super_net_inflow_pct"))
    big_net_amount = _safe_float(detail.get("big_net_inflow"))
    big_inflow_pct = _safe_float(detail.get("big_net_inflow_pct"))
    change_pct = _safe_float(detail.get("change_pct"))
    volume_ratio = _safe_float(detail.get("volume_ratio"), 1.0)
    amplitude = _safe_float(detail.get("amplitude"))
    turnover = _safe_float(detail.get("turnover"))
    capital_type = str(detail.get("capital_anomaly_type") or "")
    bid_depth_5 = _safe_float(detail.get("bid_depth_5"))
    ask_depth_5 = _safe_float(detail.get("ask_depth_5"))
    imbalance = _safe_float(detail.get("orderbook_imbalance"))
    support_strength = _safe_float(detail.get("support_strength_score"))
    seal_quality = _safe_float(detail.get("seal_quality_score"))
    withdrawal_ratio = _safe_float(detail.get("withdrawal_ratio"))
    event_type = anomaly.get("event_type") or ""

    has_limit_up = any(item.get("event_type") == "limit_up" for item in peer_events)
    has_limit_down = any(item.get("event_type") == "limit_down" for item in peer_events)
    has_breakthrough = any(
        item.get("event_type") == "breakthrough" and _safe_float(item.get("score")) >= 70
        for item in peer_events
    )
    has_pump_dump = any(
        item.get("event_type") == "pump_dump" and _safe_float(item.get("score")) >= 70
        for item in peer_events
    )

    reasons: list[str] = []
    confirmation_score = 0
    direction = "inflow" if net_amount >= 0 else "outflow"

    if event_type == "pump_dump" or net_amount <= 0:
        return None

    if direction == "inflow":
        if change_pct <= 0 or has_pump_dump:
            return None
        if _is_recent_high_board_risk(detail):
            return None
        if super_net_amount > 0 and big_net_amount > 0:
            confirmation_score += 1
            reasons.append("超大单/大单同向净流入")
        elif super_net_amount > 0 or big_net_amount > 0:
            confirmation_score += 1
            reasons.append("主力大单方向偏多")
        if super_inflow_pct >= 1.5 or big_inflow_pct >= 2.0:
            confirmation_score += 1
            reasons.append("主力资金占比提升")
        if has_breakthrough:
            confirmation_score += 2
            reasons.append("叠加真实突破信号")
        if support_strength >= 65 and imbalance >= 0.12 and bid_depth_5 > ask_depth_5 * 1.2:
            confirmation_score += 2
            reasons.append(
                f"盘口承接强({support_strength:.0f})，买五 {_format_orderbook_volume(bid_depth_5)} > 卖五 {_format_orderbook_volume(ask_depth_5)}"
            )
        elif seal_quality >= 70:
            confirmation_score += 2
            reasons.append(f"封单质量较强({seal_quality:.0f})")
        elif support_strength >= 58:
            confirmation_score += 1
            reasons.append(f"盘口承接偏强({support_strength:.0f})")
        if change_pct >= 3.0 and volume_ratio >= 1.8:
            confirmation_score += 2
            reasons.append(f"股价放量上行 {change_pct:+.1f}% / 量比 {volume_ratio:.1f}")
        elif change_pct >= 2.0 and volume_ratio >= 1.5:
            confirmation_score += 1
            reasons.append(f"股价同步转强 {change_pct:+.1f}% / 量比 {volume_ratio:.1f}")
        if abs(net_amount) >= 5e8:
            confirmation_score += 1
            reasons.append(f"资金净额 {_format_net_amount_yi(net_amount)}")
        if inflow_pct >= 8:
            confirmation_score += 1
            reasons.append(f"资金净额占成交额比 {inflow_pct:.1f}%")
        if capital_type == "consecutive":
            confirmation_score += 1
            reasons.append("连续净流入延续")
        if confirmation_score < 3:
            return None
        has_positive_driver = _has_positive_driver_sector(detail)
        if has_limit_up:
            reasons.append("同股叠加涨停信号，可与打板关注联动观察")
        return {
            "confirmed": True,
            "direction": "inflow",
            "label": "资金确认流入",
            "reasons": reasons,
            "source": source,
            "source_label": _format_data_source_label(source),
            "as_of": as_of,
            "entry_grade": _entry_grade(
                confirmation_score,
                direct_execute=(
                    _has_qualified_main_fund_source(detail)
                    and
                    confirmation_score >= 4
                    and change_pct <= 4.8
                    and volume_ratio >= 1.2
                    and volume_ratio <= 2.5
                    and amplitude <= 5.0
                    and turnover <= 12.0
                    and support_strength >= 70
                    and has_positive_driver
                ),
            ),
        }

    return None


def _build_anomaly_push_message_data(anomaly: dict, peer_events: list[dict] | None = None) -> dict | None:
    event_type = anomaly.get("event_type") or ""
    detail = _signal_fund_detail(anomaly)
    anomaly = {**anomaly, "detail": detail}
    code = anomaly.get("code") or ""
    name = anomaly.get("name") or code or "未知标的"
    description = anomaly.get("description") or ""
    level = anomaly.get("level") or "major"

    message = None
    setup_grade = ""
    signal_label = ""

    if event_type == "limit_up":
        entry = _build_limit_up_entry_context(anomaly, peer_events)
        if not entry:
            return None
        setup_grade = entry.get("entry_grade", "B类观察候选")
        signal_label = entry.get("label", "")
        message = limit_up_alert(
            code=code,
            name=name,
            price=_safe_float(detail.get("price")),
            consecutive_days=max(_safe_int(detail.get("consecutive_days"), 1), 1),
            seal_amount=_safe_float(detail.get("seal_amount")),
            volume_ratio=_safe_float(detail.get("volume_ratio"), 1.0),
            turnover_rate=_safe_float(detail.get("turnover")),
            reason=description,
            signal_label=signal_label,
            confirmation_reasons=entry.get("reasons", []),
            source="Tencent 实时行情",
            as_of=str(detail.get("as_of") or detail.get("updated_at") or ""),
            setup_grade=setup_grade,
        )
    elif event_type == "limit_down":
        entry = _build_limit_down_entry_context(anomaly, peer_events)
        if not entry:
            return None
        setup_grade = entry.get("entry_grade", "B类观察候选")
        signal_label = entry.get("label", "")
        message = limit_down_alert(
            code=code,
            name=name,
            price=_safe_float(detail.get("price")),
            consecutive_days=max(_safe_int(detail.get("consecutive_days"), 1), 1),
            volume_ratio=_safe_float(detail.get("volume_ratio"), 1.0),
            signal_label=signal_label,
            confirmation_reasons=entry.get("reasons", []),
            source="Tencent 实时行情",
            as_of=str(detail.get("as_of") or detail.get("updated_at") or ""),
            setup_grade=setup_grade,
            sector_factors=detail.get("sector_factors") or [],
            sector_components=detail.get("sector_components") or [],
        )
    elif event_type in {"capital", "pump_dump"}:
        confirmation = _build_capital_confirmation_context(anomaly, peer_events)
        if not confirmation or confirmation.get("direction") != "inflow":
            return None
        setup_grade = confirmation.get("entry_grade", "B类观察候选")
        signal_label = confirmation.get("label", "")
        message = capital_anomaly_alert(
            code=code,
            name=name,
            description=description or "资金异动",
            level=level,
            net_amount=_safe_float(detail.get("main_net_inflow")),
            price_change_pct=_safe_float(detail.get("change_pct")),
            volume_ratio=_safe_float(detail.get("volume_ratio"), 1.0),
            price=_safe_float(detail.get("price")),
            amplitude=_safe_float(detail.get("amplitude")),
            turnover=_safe_float(detail.get("turnover")),
            volume=_safe_float(detail.get("volume")),
            amount=_safe_float(detail.get("amount")),
            open_price=_safe_float(detail.get("open")),
            high_price=_safe_float(detail.get("high")),
            low_price=_safe_float(detail.get("low")),
            prev_close=_safe_float(detail.get("prev_close")),
            pe_ttm=_safe_float(detail.get("pe_ttm")),
            pb=_safe_float(detail.get("pb")),
            circ_market_cap=_safe_float(detail.get("circ_market_cap")),
            avg_price=_safe_float(detail.get("avg_price")),
            net_inflow_pct=_safe_float(detail.get("main_net_inflow_pct")),
            super_net_inflow=_safe_float(detail.get("super_net_inflow")),
            super_net_inflow_pct=_safe_float(detail.get("super_net_inflow_pct")),
            big_net_inflow=_safe_float(detail.get("big_net_inflow")),
            big_net_inflow_pct=_safe_float(detail.get("big_net_inflow_pct")),
            bid_depth_5=_safe_float(detail.get("bid_depth_5")),
            ask_depth_5=_safe_float(detail.get("ask_depth_5")),
            orderbook_imbalance=_safe_float(detail.get("orderbook_imbalance")),
            support_strength_score=_safe_float(detail.get("support_strength_score")),
            withdrawal_ratio=_safe_float(detail.get("withdrawal_ratio")),
            is_pump_and_dump=False,
            source=confirmation.get("source", ""),
            as_of=confirmation.get("as_of", ""),
            confirmation_label=signal_label,
            confirmation_reasons=confirmation.get("reasons", []),
            setup_grade=setup_grade,
            sector_factors=detail.get("sector_factors") or [],
            sector_components=detail.get("sector_components") or [],
        )
    elif event_type == "low_absorb":
        entry = _build_low_absorb_entry_context(anomaly, peer_events)
        if not entry:
            return None
        setup_grade = entry.get("entry_grade", "B类观察候选")
        signal_label = entry.get("label", "")
        support_price = (
            _safe_float(detail.get("trend_support"))
            or _safe_float(detail.get("ma5"))
            or _safe_float(detail.get("low"))
        )
        pressure_price = _safe_float(detail.get("high_20d")) or _safe_float(detail.get("high"))
        message = capital_anomaly_alert(
            code=code,
            name=name,
            description=description or signal_label or "低吸弱转强",
            level=level,
            net_amount=_safe_float(detail.get("main_net_inflow")),
            price_change_pct=_safe_float(detail.get("change_pct")),
            volume_ratio=_safe_float(detail.get("volume_ratio"), 1.0),
            price=_safe_float(detail.get("price")),
            amplitude=_safe_float(detail.get("amplitude")),
            turnover=_safe_float(detail.get("turnover")),
            volume=_safe_float(detail.get("volume")),
            amount=_safe_float(detail.get("amount")),
            open_price=_safe_float(detail.get("open")),
            high_price=_safe_float(detail.get("high")),
            low_price=_safe_float(detail.get("low")),
            prev_close=_safe_float(detail.get("prev_close")),
            pe_ttm=_safe_float(detail.get("pe_ttm")),
            pb=_safe_float(detail.get("pb")),
            circ_market_cap=_safe_float(detail.get("circ_market_cap")),
            avg_price=_safe_float(detail.get("avg_price")),
            net_inflow_pct=_safe_float(detail.get("main_net_inflow_pct")),
            super_net_inflow=_safe_float(detail.get("super_net_inflow")),
            super_net_inflow_pct=_safe_float(detail.get("super_net_inflow_pct")),
            big_net_inflow=_safe_float(detail.get("big_net_inflow")),
            big_net_inflow_pct=_safe_float(detail.get("big_net_inflow_pct")),
            bid_depth_5=_safe_float(detail.get("bid_depth_5")),
            ask_depth_5=_safe_float(detail.get("ask_depth_5")),
            orderbook_imbalance=_safe_float(detail.get("orderbook_imbalance")),
            support_strength_score=_safe_float(detail.get("support_strength_score")),
            withdrawal_ratio=_safe_float(detail.get("withdrawal_ratio")),
            is_pump_and_dump=False,
            next_day_support=support_price,
            next_day_pressure=pressure_price,
            source=str(detail.get("source") or ""),
            as_of=entry.get("as_of", ""),
            confirmation_label=signal_label,
            confirmation_reasons=(
                ([str(detail.get("core_growth_logic"))] if detail.get("core_growth_logic") else [])
                + list(detail.get("core_growth_confirmations") or [])[:2]
                + (
                    [
                        f"牛股潜力{_safe_float(detail.get('tenbagger_score')):.0f}分，只在支撑回收时提示",
                        f"滚动60秒{_safe_float(detail.get('rolling_60s_change_pct')):+.2f}%且成交增量确认",
                    ]
                    if detail.get("tenbagger_pullback_confirmed")
                    else []
                )
                + list(detail.get("low_absorb_confirmations") or [])[:2]
                + list(entry.get("reasons", []))
            )[:5],
            setup_grade=setup_grade,
            sector_factors=detail.get("sector_factors") or [],
            sector_components=detail.get("sector_components") or [],
        )
        message.title = f"🟢 {name} ({code}) {setup_grade}｜{signal_label}"
    elif event_type == "breakthrough":
        entry = _build_breakthrough_entry_context(anomaly, peer_events)
        if not entry:
            return None
        setup_grade = entry.get("entry_grade", "B类观察候选")
        signal_label = entry.get("label", "")
        message = breakthrough_alert(
            code=code,
            name=name,
            description=description or "突破信号",
            strength="strong" if _safe_float(anomaly.get("score")) >= 85 else "moderate",
            breakthrough_price=_safe_float(detail.get("price")),
            volume_ratio=_safe_float(detail.get("volume_ratio"), 1.0),
            ma_status=str(detail.get("ma_status") or ""),
            days_near_pressure=_safe_int(detail.get("days_near_pressure")),
            quality_score=_safe_float(detail.get("quality_score")),
            current_price=_safe_float(detail.get("price")),
            change_pct=_safe_float(detail.get("change_pct")),
            amplitude=_safe_float(detail.get("amplitude")),
            turnover=_safe_float(detail.get("turnover")),
            volume=_safe_float(detail.get("volume")),
            amount=_safe_float(detail.get("amount")),
            open_price=_safe_float(detail.get("open")),
            high_price=_safe_float(detail.get("high")),
            low_price=_safe_float(detail.get("low")),
            prev_close=_safe_float(detail.get("prev_close")),
            pe_ttm=_safe_float(detail.get("pe_ttm")),
            pb=_safe_float(detail.get("pb")),
            circ_market_cap=_safe_float(detail.get("circ_market_cap")),
            is_false_breakout=bool(detail.get("is_false_breakout")),
            pullback_probability=str(detail.get("pullback_probability") or ""),
            next_day_support=(
                _safe_float(detail.get("repair_support"))
                or _safe_float(detail.get("trend_support"))
            ),
            next_day_pressure=(
                _safe_float(detail.get("breakout_anchor"))
                or _safe_float(detail.get("trend_resistance"))
            ),
            signal_variant=str(detail.get("signal_type") or ""),
            signal_label=signal_label,
            confirmation_reasons=(
                ([str(detail.get("core_growth_logic"))] if detail.get("core_growth_logic") else [])
                + list(detail.get("core_growth_confirmations") or [])[:2]
                + list(detail.get("watchlist_confirmations") or [])[:2]
                + list(detail.get("acceleration_confirmations") or [])[:2]
                + list(detail.get("event_relay_confirmations") or [])[:3]
                + list(detail.get("leader_linkage_confirmations") or [])[:3]
                + list(detail.get("sector_repair_confirmations") or [])[:2]
                + list(detail.get("old_hot_repair_confirmations") or [])[:2]
                + list(entry.get("reasons", []))
            )[:5],
            source=entry.get("source_label", ""),
            as_of=entry.get("as_of", ""),
            setup_grade=setup_grade,
            sector_factors=detail.get("sector_factors") or [],
            sector_components=detail.get("sector_components") or [],
        )
    else:
        return None

    observation_only = _is_observation_only_anomaly(anomaly)
    if observation_only:
        setup_grade = "B类观察候选"
        # 模板正文仍保留量价事实，但明确改成观察，不进入买点/自动下单语义。
        message.title = f"⚡ 异动观察｜{name} ({code})｜{signal_label}"

    setup_track = _resolve_setup_track(anomaly, setup_grade)
    setup_grade_display = _display_setup_grade(anomaly, setup_grade) or setup_grade
    alert_tier = _classify_anomaly_alert_tier(str(event_type), setup_grade)
    if (
        str(event_type) == "low_absorb"
        and setup_grade != "A1 可直接执行"
        and not _has_qualified_main_fund_source(detail)
        and not (
            (
                detail.get("underwater_acceleration_confirmed")
                or detail.get("underwater_reversal_confirmed")
            )
            and detail.get("detection_pool_member")
            and str(detail.get("signal_type") or "")
            in {"underwater_acceleration", "underwater_reversal"}
        )
        and not (
            detail.get("trend_support_touch_confirmed")
            and detail.get("detection_pool_member")
            and str(detail.get("signal_type") or "") == "trend_support_touch"
        )
        and not (
            detail.get("main_wave_green_open_reclaim_confirmed")
            and detail.get("detection_pool_member")
            and str(detail.get("signal_type") or "")
            == "main_wave_green_open_reclaim"
        )
        and not (
            (
                detail.get("main_wave_shape_pullback_confirmed")
                or detail.get("main_wave_support_reclaim_confirmed")
            )
            and detail.get("detection_pool_member")
            and str(detail.get("signal_type") or "")
            in {"main_wave_shape_pullback_reclaim", "main_wave_support_reclaim"}
        )
        and not (
            detail.get("second_wave_restart_confirmed")
            and detail.get("detection_pool_member")
            and str(detail.get("signal_type") or "") == "second_wave_restart"
        )
    ):
        alert_tier = ""
    signal_identity = _anomaly_signal_identity(anomaly)
    buypoint_pushable, continuity_reasons = _build_buypoint_continuity_context(
        {
            "anomaly": anomaly,
            "event_type": str(event_type),
            "setup_grade": setup_grade,
        },
        peer_events,
    )
    if observation_only:
        buypoint_pushable = False
        continuity_reasons = list(continuity_reasons) + [
            "当前仅为异动观察，须等待形态池/支撑回收确认后才升级买点"
        ]
    is_watchlist_member = _is_watchlist_member_anomaly(anomaly)
    is_low_base_watchlist = _is_low_base_watchlist_anomaly(anomaly)
    if is_watchlist_member:
        message.title = f"⭐ 重点观察｜{message.title}"
    message.extra = dict(message.extra or {})
    is_rapid_rise_strong = bool(
        str(detail.get("signal_type") or "") == "positive_acceleration"
        and detail.get("positive_acceleration_rolling60")
        and str(detail.get("rolling_60s_tier") or "") == "strong"
        and _intraday_acceleration_volume_ready(detail)
        and not _has_acceleration_distribution_risk(detail)
    )
    is_pre_limit_up_acceleration = bool(
        str(detail.get("signal_type") or "") == "positive_acceleration"
        and detail.get("positive_acceleration_pre_limit_up")
        and _intraday_acceleration_volume_ready(detail)
        and not _has_acceleration_distribution_risk(detail)
    )
    signal_variant = str(detail.get("signal_type") or event_type)
    if is_rapid_rise_strong:
        signal_variant = "positive_acceleration_strong"
    elif is_pre_limit_up_acceleration:
        signal_variant = "positive_acceleration_pre_limit"
    speculation_context = _resolve_speculation_expectation_context(anomaly)
    message.extra.update(
        {
            "setup_grade": setup_grade,
            "setup_grade_display": setup_grade_display,
            "setup_track": setup_track,
            "signal_label": _event_signal_label(str(event_type), signal_label),
            "event_type": str(event_type),
            "alert_tier": alert_tier,
            "signal_identity": signal_identity,
            "buypoint_pushable": buypoint_pushable,
            "continuity_reasons": continuity_reasons,
            "watchlist_member": is_watchlist_member,
            "detection_pool_member": bool(detail.get("detection_pool_member")),
            "low_base_watchlist": is_low_base_watchlist,
            "low_base_setup_confirmed": is_low_base_watchlist,
            "signal_variant": signal_variant,
            "rapid_rise_strong_confirmed": is_rapid_rise_strong,
            "pre_limit_up_confirmed": is_pre_limit_up_acceleration,
            "signal_price": _safe_float(detail.get("price")),
            "signal_score": _safe_float(anomaly.get("score")),
            "change_pct": _safe_float(detail.get("change_pct")),
            # Preserve the event frame for audit; do not substitute displayed/latest funds.
            "fund_signal_evidence": detail.get("fund_signal_evidence"),
            **speculation_context,
        }
    )
    return {
        "anomaly": anomaly,
        "message": message,
        "event_type": str(event_type),
        "setup_grade": setup_grade,
        "setup_grade_display": setup_grade_display,
        "setup_track": setup_track,
        "signal_label": _event_signal_label(str(event_type), signal_label),
        "alert_tier": alert_tier,
        "signal_identity": signal_identity,
        "buypoint_pushable": buypoint_pushable,
        "continuity_reasons": continuity_reasons,
    }


def _has_confirmed_capital_inflow(peer_events: list[dict] | None = None) -> bool:
    peer_events = peer_events or []
    for item in peer_events:
        if (item.get("event_type") or "") != "capital":
            continue
        confirmation = _build_capital_confirmation_context(item, peer_events)
        if confirmation and confirmation.get("direction") == "inflow":
            return True
    return False


def _build_limit_up_entry_context(anomaly: dict, peer_events: list[dict] | None = None) -> dict | None:
    detail = anomaly.get("detail") or {}
    if bool(anomaly.get("is_one_word_board")):
        return None

    consecutive_days = max(_safe_int(detail.get("consecutive_days"), 1), 1)
    if consecutive_days >= 3:
        return None

    turnover = _safe_float(detail.get("turnover"))
    volume_ratio = _safe_float(detail.get("volume_ratio"), 1.0)
    support_strength = _safe_float(detail.get("support_strength_score"))
    seal_quality = _safe_float(detail.get("seal_quality_score"))
    imbalance = _safe_float(detail.get("orderbook_imbalance"))
    bid_depth_5 = _safe_float(detail.get("bid_depth_5"))
    ask_depth_5 = _safe_float(detail.get("ask_depth_5"))

    if turnover < 3 or turnover > 20:
        return None
    if volume_ratio < 1.0:
        return None

    reasons: list[str] = []
    confirmation_score = 0
    if seal_quality >= 75:
        confirmation_score += 2
        reasons.append(f"封单质量高({seal_quality:.0f})，适合观察打板承接")
    if support_strength >= 65 and imbalance >= 0.12 and bid_depth_5 > ask_depth_5 * 1.15:
        confirmation_score += 2
        reasons.append(f"盘口承接强({support_strength:.0f})，买五明显强于卖五")
    elif support_strength >= 58:
        confirmation_score += 1
        reasons.append(f"盘口承接偏强({support_strength:.0f})")
    if volume_ratio >= 1.5:
        confirmation_score += 1
        reasons.append(f"量比{volume_ratio:.1f}，放量封板")
    anomaly_score = _safe_float(anomaly.get("score"))
    if anomaly_score >= 90:
        confirmation_score += 2
        reasons.append("封板强度高，可纳入打板关注")
    elif anomaly_score >= 80:
        confirmation_score += 1
        reasons.append("封板强度尚可")
    if any((item.get("event_type") or "") == "breakthrough" and _safe_float(item.get("score")) >= 70 for item in (peer_events or [])):
        confirmation_score += 1
        reasons.append("叠加突破关注信号")
    if _has_confirmed_capital_inflow(peer_events):
        confirmation_score += 1
        reasons.append("伴随资金确认流入")

    threshold = 2 if consecutive_days == 1 else 3
    if confirmation_score < threshold:
        return None

    return {
        "label": "打板关注",
        "reasons": reasons,
        "entry_grade": _entry_grade(
            confirmation_score,
            direct_execute=(
                consecutive_days == 1
                and confirmation_score >= 4
                and turnover <= 15
                and volume_ratio <= 2.5
                and support_strength >= 65
                and seal_quality >= 75
            ),
        ),
    }


def _build_limit_down_entry_context(anomaly: dict, peer_events: list[dict] | None = None) -> dict | None:
    detail = anomaly.get("detail") or {}
    consecutive_days = max(_safe_int(detail.get("consecutive_days"), 1), 1)
    if consecutive_days >= 2:
        return None

    if not _has_confirmed_capital_inflow(peer_events):
        return None

    turnover = _safe_float(detail.get("turnover"))
    volume_ratio = _safe_float(detail.get("volume_ratio"), 1.0)
    support_strength = _safe_float(detail.get("support_strength_score"))
    imbalance = _safe_float(detail.get("orderbook_imbalance"))
    bid_depth_5 = _safe_float(detail.get("bid_depth_5"))
    ask_depth_5 = _safe_float(detail.get("ask_depth_5"))
    withdrawal_ratio = _safe_float(detail.get("withdrawal_ratio"))

    if turnover < 8 or volume_ratio < 2.0:
        return None
    if support_strength < 75 or imbalance < 0.18 or bid_depth_5 <= ask_depth_5 * 1.35:
        return None
    if withdrawal_ratio >= 0.12:
        return None

    return {
        "label": "低吸观察",
        "reasons": [
            f"盘口强承接({support_strength:.0f})，买五明显强于卖五",
            "同步出现资金确认流入",
            f"量比{volume_ratio:.1f}且换手{turnover:.1f}%，有撬板关注价值",
        ],
        "entry_grade": _entry_grade(
            5 if support_strength >= 82 and volume_ratio >= 2.5 else 4
        ),
    }


def _build_underwater_reversal_entry_context(
    anomaly: dict,
    peer_events: list[dict] | None = None,
) -> dict | None:
    """把水下急拉预警/翻红确认转换成可审计的A2/A1提醒。"""
    detail = anomaly.get("detail") or {}
    signal_type = str(detail.get("signal_type") or "")
    is_underwater_acceleration = signal_type == "underwater_acceleration"
    if not (
        (
            detail.get("underwater_acceleration_confirmed")
            or detail.get("underwater_reversal_confirmed")
        )
        and (
            detail.get("detection_pool_member")
            or detail.get("market_wide_detection")
        )
        and signal_type in {"underwater_acceleration", "underwater_reversal"}
    ):
        return None
    if _is_recent_high_board_risk(detail):
        return None
    if (
        not _intraday_acceleration_volume_ready(detail)
        or _has_acceleration_distribution_risk(detail)
    ):
        return None

    change_pct = _safe_float(detail.get("change_pct"))
    volume_ratio = _safe_float(detail.get("volume_ratio"), 1.0)
    amplitude = _safe_float(detail.get("amplitude"))
    turnover = _safe_float(detail.get("turnover"))
    support_strength = _safe_float(detail.get("support_strength_score"))
    imbalance = _safe_float(detail.get("orderbook_imbalance"))
    bid_depth_5 = _safe_float(detail.get("bid_depth_5"))
    ask_depth_5 = _safe_float(detail.get("ask_depth_5"))
    min5_change = _safe_float(detail.get("min5_change"))
    scan_change_pct = _safe_float(detail.get("scan_change_pct"))
    net_amount = _safe_float(detail.get("main_net_inflow"))
    inflow_pct = _safe_float(detail.get("main_net_inflow_pct"))
    low_drop_pct = _safe_float(detail.get("low_drop_pct"))
    intraday_rebound_pct = _safe_float(detail.get("intraday_rebound_pct"))
    price_vs_avg_pct = _safe_float(detail.get("price_vs_avg_pct"))
    close_position = _safe_float(detail.get("close_position"))
    pullback_from_high_pct = _safe_float(detail.get("pullback_from_high_pct"))
    core_confirmation_count = _safe_int(detail.get("underwater_core_confirmation_count"))
    source = str(detail.get("source") or "")
    as_of = str(detail.get("as_of") or detail.get("updated_at") or date.today())
    fund_data_degraded = bool(detail.get("fund_data_degraded"))
    fund_live = source in {"eastmoney_main_fund", "fund_flow"} and not fund_data_degraded

    if low_drop_pct > -settings.ANOMALY_UNDERWATER_REVERSAL_MIN_LOW_DROP_PCT:
        return None
    min_change_pct = (
        settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_CHANGE_PCT
        if is_underwater_acceleration
        else settings.ANOMALY_UNDERWATER_REVERSAL_MIN_CHANGE_PCT
    )
    max_change_pct = (
        settings.ANOMALY_UNDERWATER_REVERSAL_MIN_CHANGE_PCT
        if is_underwater_acceleration
        else settings.ANOMALY_UNDERWATER_REVERSAL_MAX_CHANGE_PCT
    )
    if not (min_change_pct <= change_pct <= max_change_pct):
        return None
    min_volume_ratio = (
        settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_VOLUME_RATIO
        if is_underwater_acceleration
        else settings.ANOMALY_UNDERWATER_REVERSAL_MIN_VOLUME_RATIO
    )
    if not (
        min_volume_ratio
        <= volume_ratio
        <= settings.ANOMALY_UNDERWATER_REVERSAL_MAX_VOLUME_RATIO
    ):
        return None
    if (
        amplitude > settings.ANOMALY_UNDERWATER_REVERSAL_MAX_AMPLITUDE
        or turnover > settings.ANOMALY_UNDERWATER_REVERSAL_MAX_TURNOVER
    ):
        return None
    min_price_vs_avg_pct = (
        settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_VWAP_GAP_PCT
        if is_underwater_acceleration
        else 0.0
    )
    if price_vs_avg_pct < min_price_vs_avg_pct:
        return None
    if close_position < settings.ANOMALY_UNDERWATER_REVERSAL_MIN_CLOSE_POSITION:
        return None
    if pullback_from_high_pct <= -settings.ANOMALY_UNDERWATER_REVERSAL_MAX_PULLBACK_FROM_HIGH_PCT:
        return None
    if core_confirmation_count < 2:
        return None
    if fund_live and (net_amount <= -1e8 or inflow_pct <= -4.0):
        return None

    if is_underwater_acceleration:
        momentum_confirmed = bool(
            intraday_rebound_pct
            >= settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_REBOUND_PCT
            and max(min5_change, scan_change_pct)
            >= settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_MOMENTUM_PCT
        )
    else:
        momentum_confirmed = bool(
            min5_change >= 0.45
            or scan_change_pct >= 0.45
            or detail.get("crossed_from_underwater")
            or intraday_rebound_pct >= 2.6
        )
    orderbook_confirmed = bool(
        support_strength >= 60
        and imbalance >= 0.05
        and bid_depth_5 > ask_depth_5 * 1.03
    )
    fund_confirmed = bool(
        fund_live
        and net_amount > 0
        and (inflow_pct >= 2.0 or net_amount >= 5e7)
    )
    sector_confirmed = _has_strong_low_absorb_sector(detail)
    if not momentum_confirmed or not (orderbook_confirmed or fund_confirmed):
        return None

    reasons: list[str] = [
        (
            f"仍在水下{change_pct:.1f}%，已从低点急拉{intraday_rebound_pct:.1f}%"
            if is_underwater_acceleration
            else f"最低{low_drop_pct:.1f}%后翻红，从低点回升{intraday_rebound_pct:.1f}%"
        ),
        (
            f"距离VWAP仅{price_vs_avg_pct:+.1f}%"
            if price_vs_avg_pct < 0
            else f"重新站回VWAP上方{price_vs_avg_pct:+.1f}%"
        ),
    ]
    if is_underwater_acceleration:
        reasons.insert(0, "尚未翻红，先发抢跑预警；翻红/VWAP确认后再升级")
    reasons.append(
        "拉升窗口成交速率"
        f"{_safe_float(detail.get('intraday_amount_pace_ratio')):.1f}倍，确认真实增量成交"
    )
    confirmation_score = 3
    if close_position >= 0.72 and pullback_from_high_pct >= -1.5:
        confirmation_score += 1
        reasons.append("价格保持在日内高位区，未明显冲高回落")
    if min5_change >= 0.45 or scan_change_pct >= 0.45:
        confirmation_score += 2
        reasons.append(f"短周期动能加速{max(min5_change, scan_change_pct):+.1f}%")
    elif detail.get("crossed_from_underwater"):
        confirmation_score += 1
        reasons.append("连续快照确认水下翻红")
    if orderbook_confirmed:
        confirmation_score += 2
        reasons.append(f"盘口承接强({support_strength:.0f})，买五强于卖五")
    if fund_confirmed:
        confirmation_score += 1
        reasons.append(f"实时主力净流入占比{inflow_pct:.1f}%")
    if net_amount >= 1.5e8 and fund_confirmed:
        confirmation_score += 1
        reasons.append(f"实时资金净额{_format_net_amount_yi(net_amount)}")
    if sector_confirmed:
        confirmation_score += 1
        reasons.append("主驱动板块同步转强")
    if fund_data_degraded:
        reasons.append("资金接口降级，本次仅按盘口与板块轻提醒")

    if confirmation_score < 6:
        return None

    direct_execute = bool(
        not is_underwater_acceleration
        and fund_live
        and fund_confirmed
        and orderbook_confirmed
        and sector_confirmed
        and confirmation_score >= 9
        and change_pct <= 4.2
        and 1.0 <= volume_ratio <= 2.4
        and amplitude <= 8.5
        and turnover <= 13.0
        and support_strength >= 70
        and close_position >= 0.70
        and pullback_from_high_pct >= -1.5
        and (min5_change >= 0.6 or scan_change_pct >= 0.6)
    )

    return {
        "label": str(
            detail.get("signal_label")
            or (
                "水下放量急拉预警"
                if is_underwater_acceleration
                else "水下翻红快速启动"
            )
        ),
        "reasons": reasons[:7],
        "source_label": _format_data_source_label(source),
        "as_of": as_of,
        "entry_grade": _entry_grade(confirmation_score, direct_execute=direct_execute),
    }


def _build_second_wave_restart_entry_context(anomaly: dict) -> dict | None:
    """高标/主升急杀后的首个止跌急拉，只给A2二波启动提醒。"""
    detail = anomaly.get("detail") or {}
    if not (
        detail.get("second_wave_restart_confirmed")
        and detail.get("trend_setup_confirmed")
        and detail.get("detection_pool_member")
        and str(detail.get("signal_type") or "") == "second_wave_restart"
    ):
        return None
    if _is_recent_high_board_risk(detail):
        return None
    if (
        not _intraday_acceleration_volume_ready(detail)
        or _has_acceleration_distribution_risk(detail)
    ):
        return None

    reset_stats = detail.get("second_wave_reset_stats") or {}
    reset_type = str(detail.get("second_wave_reset_type") or reset_stats.get("reset_type") or "")
    drawdown_pct = _safe_float(reset_stats.get("drawdown_pct"))
    days_since_peak = _safe_int(reset_stats.get("days_since_peak"))
    change_pct = _safe_float(detail.get("change_pct"))
    volume_ratio = _safe_float(detail.get("volume_ratio"), 1.0)
    amplitude = _safe_float(detail.get("amplitude"))
    turnover = _safe_float(detail.get("turnover"))
    intraday_rebound_pct = _safe_float(detail.get("intraday_rebound_pct"))
    acceleration_pct = max(
        _safe_float(detail.get("acceleration_pct")),
        _safe_float(detail.get("min5_change")),
        _safe_float(detail.get("scan_change_pct")),
    )
    price_vs_avg_pct = _safe_float(detail.get("price_vs_avg_pct"))
    support_strength = _safe_float(detail.get("support_strength_score"))
    imbalance = _safe_float(detail.get("orderbook_imbalance"))
    bid_depth_5 = _safe_float(detail.get("bid_depth_5"))
    ask_depth_5 = _safe_float(detail.get("ask_depth_5"))
    net_amount = _safe_float(detail.get("main_net_inflow"))
    inflow_pct = _safe_float(detail.get("main_net_inflow_pct"))
    source = str(detail.get("source") or "")
    is_stale = bool(detail.get("is_stale"))
    core_confirmation_count = _safe_int(
        detail.get("second_wave_core_confirmation_count")
    )

    if not (
        reset_type in {"high_board_reset", "trend_cascade_reset"}
        and 3 <= days_since_peak <= 15
        and -55.0 <= drawdown_pct <= -12.0
        and -2.8 <= change_pct <= 6.5
        and 1.05 <= volume_ratio <= 3.2
        and amplitude <= 12.0
        and turnover <= 20.0
        and detail.get("near_intraday_high")
        and price_vs_avg_pct >= -0.8
        and (
            acceleration_pct >= 0.65
            or intraday_rebound_pct >= 2.5
        )
        and core_confirmation_count >= 2
    ):
        return None

    fund_live = source in {"eastmoney_main_fund", "fund_flow"} and not is_stale
    if fund_live and (net_amount <= -1e8 or inflow_pct <= -4.0):
        return None
    orderbook_confirmed = bool(
        support_strength >= 65
        and imbalance >= 0.05
        and bid_depth_5 > ask_depth_5 * 1.05
    )
    fund_confirmed = bool(
        fund_live
        and net_amount > 0
        and (inflow_pct >= 2.5 or net_amount >= 5e7)
    )
    sector_confirmed = _has_strong_low_absorb_sector(detail)
    if not (orderbook_confirmed or fund_confirmed):
        return None
    if sum((orderbook_confirmed, fund_confirmed, sector_confirmed)) < 2:
        return None

    reasons = [
        "这是二波启动确认，不在连续下跌途中直接抄底",
        (
            f"首波{_safe_int(reset_stats.get('max_board_streak'))}连板后"
            if reset_type == "high_board_reset"
            else f"首波主升{_safe_float(reset_stats.get('first_wave_gain_pct')):.1f}%后"
        )
        + f"下杀{abs(drawdown_pct):.1f}%",
        f"从日内低点急拉{intraday_rebound_pct:.1f}%并保持日内强势",
        (
            f"短周期动能{acceleration_pct:+.1f}% / "
            f"成交速率{_safe_float(detail.get('intraday_amount_pace_ratio')):.1f}倍"
        ),
    ]
    confirmation_score = 3
    if orderbook_confirmed:
        confirmation_score += 2
        reasons.append(f"盘口承接强({support_strength:.0f})，买五强于卖五")
    if fund_confirmed:
        confirmation_score += 1
        reasons.append(f"实时主力净流入占比{inflow_pct:.1f}%")
    if sector_confirmed:
        confirmation_score += 1
        reasons.append("主驱动板块同步转强")

    return {
        "label": "高标下杀二波启动预警",
        "reasons": reasons[:7],
        "source_label": _format_data_source_label(source),
        "as_of": str(detail.get("as_of") or detail.get("updated_at") or date.today()),
        "entry_grade": _entry_grade(confirmation_score, direct_execute=False),
    }


def _build_trend_support_touch_entry_context(anomaly: dict) -> dict | None:
    """趋势股支撑到达只生成B类观察；止跌回收后才升级为真实A2。"""
    detail = anomaly.get("detail") or {}
    if not (
        detail.get("trend_support_touch_confirmed")
        and detail.get("trend_setup_confirmed")
        and detail.get("detection_pool_member")
        and str(detail.get("signal_type") or "") == "trend_support_touch"
    ):
        return None
    if _is_recent_high_board_risk(detail):
        return None

    change_pct = _safe_float(detail.get("change_pct"))
    volume_ratio = _safe_float(detail.get("volume_ratio"), 1.0)
    amplitude = _safe_float(detail.get("amplitude"))
    turnover = _safe_float(detail.get("turnover"))
    support_gap_pct = _safe_float(detail.get("support_gap_pct"))
    from_low_pct = _safe_float(detail.get("from_intraday_low_pct"))
    min5_change = _safe_float(detail.get("min5_change"))
    support_strength = _safe_float(detail.get("support_strength_score"))
    imbalance = _safe_float(detail.get("orderbook_imbalance"))
    bid_depth_5 = _safe_float(detail.get("bid_depth_5"))
    ask_depth_5 = _safe_float(detail.get("ask_depth_5"))
    net_amount = _safe_float(detail.get("main_net_inflow"))
    inflow_pct = _safe_float(detail.get("main_net_inflow_pct"))
    source = str(detail.get("source") or "")
    is_stale = bool(detail.get("is_stale"))
    core_confirmation_count = _safe_int(
        detail.get("support_touch_core_confirmation_count")
    )

    if not (
        -3.5 <= change_pct <= 1.5
        and settings.ANOMALY_TREND_SUPPORT_TOUCH_MIN_GAP_PCT
        <= support_gap_pct
        <= settings.ANOMALY_TREND_SUPPORT_TOUCH_MAX_GAP_PCT
        and from_low_pct <= settings.ANOMALY_TREND_SUPPORT_TOUCH_MAX_FROM_LOW_PCT
        and min5_change >= settings.ANOMALY_TREND_SUPPORT_TOUCH_MIN_MIN5_CHANGE_PCT
        and 0.65 <= volume_ratio <= 1.9
        and amplitude <= 6.5
        and turnover <= 12.5
        and core_confirmation_count >= 2
    ):
        return None

    fund_live = source in {"eastmoney_main_fund", "fund_flow"} and not is_stale
    if fund_live and (net_amount <= -1e8 or inflow_pct <= -4.0):
        return None
    orderbook_confirmed = bool(
        support_strength >= 65
        and imbalance >= 0.05
        and bid_depth_5 > ask_depth_5 * 1.05
    )
    fund_confirmed = bool(
        fund_live
        and net_amount > 0
        and (inflow_pct >= 2.5 or net_amount >= 5e7)
    )
    sector_confirmed = _has_strong_low_absorb_sector(detail)
    if not (orderbook_confirmed or fund_confirmed):
        return None
    if sum((orderbook_confirmed, fund_confirmed, sector_confirmed)) < 2:
        return None

    support = _safe_float(detail.get("trend_support"))
    reasons = [
        f"日内最低已触及趋势支撑 {support:.2f}",
        f"现价距日内低点仅 {from_low_pct:.1f}%",
        "当前仅为到达预警，等待止跌回拉后再升级",
    ]
    confirmation_score = 3
    if orderbook_confirmed:
        confirmation_score += 2
        reasons.append(f"盘口承接强({support_strength:.0f})，买五强于卖五")
    if fund_confirmed:
        confirmation_score += 1
        reasons.append(f"实时主力净流入占比{inflow_pct:.1f}%")
    if sector_confirmed:
        confirmation_score += 1
        reasons.append("主驱动板块仍保持正向")

    return {
        "label": "趋势支撑到达预警",
        "reasons": reasons[:7],
        "source_label": _format_data_source_label(source),
        "as_of": str(detail.get("as_of") or detail.get("updated_at") or date.today()),
        "entry_grade": "B类观察候选",
    }


def _build_main_wave_green_open_entry_context(anomaly: dict) -> dict | None:
    """主升浪绿开下杀后的止跌回收，只给A2并保留增量成交审计。"""
    detail = anomaly.get("detail") or {}
    if not (
        detail.get("main_wave_green_open_reclaim_confirmed")
        and detail.get("trend_setup_confirmed")
        and detail.get("detection_pool_member")
        and str(detail.get("signal_type") or "")
        == "main_wave_green_open_reclaim"
    ):
        return None
    if _is_recent_high_board_risk(detail):
        return None

    change_pct = _safe_float(detail.get("change_pct"))
    open_change_pct = _safe_float(detail.get("open_change_pct"))
    low_drop_pct = _safe_float(detail.get("low_drop_pct"))
    intraday_rebound_pct = _safe_float(detail.get("intraday_rebound_pct"))
    short_momentum_pct = _safe_float(detail.get("short_momentum_pct"))
    close_position = _safe_float(detail.get("close_position"))
    pullback_from_high_pct = _safe_float(detail.get("pullback_from_high_pct"))
    price_vs_avg_pct = _safe_float(detail.get("price_vs_avg_pct"))
    volume_ratio = _safe_float(detail.get("volume_ratio"), 1.0)
    amplitude = _safe_float(detail.get("amplitude"))
    turnover = _safe_float(detail.get("turnover"))
    support_strength = _safe_float(detail.get("support_strength_score"))
    net_amount = _safe_float(detail.get("main_net_inflow"))
    inflow_pct = _safe_float(detail.get("main_net_inflow_pct"))
    source = str(detail.get("source") or "")
    is_stale = bool(detail.get("is_stale"))
    if not (
        settings.ANOMALY_MAIN_WAVE_GREEN_OPEN_LOWER_PCT
        <= open_change_pct
        <= settings.ANOMALY_MAIN_WAVE_GREEN_OPEN_UPPER_PCT
        and low_drop_pct <= settings.ANOMALY_MAIN_WAVE_GREEN_MIN_LOW_DROP_PCT
        and -3.5 <= change_pct <= 1.8
        and intraday_rebound_pct >= settings.ANOMALY_MAIN_WAVE_GREEN_MIN_REBOUND_PCT
        and short_momentum_pct >= settings.ANOMALY_MAIN_WAVE_GREEN_MIN_MOMENTUM_PCT
        and close_position >= settings.ANOMALY_MAIN_WAVE_GREEN_MIN_CLOSE_POSITION
        and pullback_from_high_pct
        >= -settings.ANOMALY_MAIN_WAVE_GREEN_MAX_PULLBACK_FROM_HIGH_PCT
        and price_vs_avg_pct >= -0.8
        and settings.ANOMALY_MAIN_WAVE_GREEN_MIN_VOLUME_RATIO
        <= volume_ratio
        <= settings.ANOMALY_MAIN_WAVE_GREEN_MAX_VOLUME_RATIO
        and amplitude <= settings.ANOMALY_MAIN_WAVE_GREEN_MAX_AMPLITUDE
        and turnover <= settings.ANOMALY_MAIN_WAVE_GREEN_MAX_TURNOVER
        and _safe_int(detail.get("main_wave_core_confirmation_count")) >= 2
        and _intraday_acceleration_volume_ready(detail)
        and not _has_acceleration_distribution_risk(detail)
    ):
        return None

    fund_live = source in {"eastmoney_main_fund", "fund_flow"} and not is_stale
    if fund_live and (net_amount <= -1e8 or inflow_pct <= -4.0):
        return None
    orderbook_confirmed = bool(
        support_strength >= 65
        and _safe_float(detail.get("orderbook_imbalance")) >= 0.05
        and _safe_float(detail.get("bid_depth_5"))
        > _safe_float(detail.get("ask_depth_5")) * 1.05
    )
    fund_confirmed = bool(
        fund_live
        and net_amount > 0
        and (inflow_pct >= 2.5 or net_amount >= 5e7)
    )
    sector_confirmed = _has_strong_low_absorb_sector(detail)
    if not (orderbook_confirmed or fund_confirmed):
        return None
    if sum((orderbook_confirmed, fund_confirmed, sector_confirmed)) < 2:
        return None

    reasons = [
        f"主升浪绿开{open_change_pct:.1f}%，最低下探{low_drop_pct:.1f}%",
        f"从日内低点回抽{intraday_rebound_pct:.1f}%，短周期动能{short_momentum_pct:+.1f}%",
        (
            f"滚动60秒增量成交{_safe_float(detail.get('rolling_60s_amount_delta')) / 1e6:.0f}百万元"
            if detail.get("main_wave_green_open_reclaim_rolling60")
            else f"扫描窗口成交速率{_safe_float(detail.get('intraday_amount_pace_ratio')):.1f}倍"
        ),
    ]
    confirmation_score = 4
    if orderbook_confirmed:
        confirmation_score += 2
        reasons.append(f"盘口承接强({support_strength:.0f})，买五强于卖五")
    if fund_confirmed:
        confirmation_score += 1
        reasons.append(f"实时主力净流入占比{inflow_pct:.1f}%")
    if sector_confirmed:
        confirmation_score += 1
        reasons.append("主驱动板块同步转强")
    reasons.append("仅为止跌回收确认，不在绿开继续下杀时抄底")
    return {
        "label": "主升浪绿开下杀回收",
        "reasons": reasons[:7],
        "source_label": _format_data_source_label(source),
        "as_of": str(detail.get("as_of") or detail.get("updated_at") or date.today()),
        "entry_grade": _entry_grade(confirmation_score, direct_execute=False),
    }


def _build_main_wave_shape_pullback_entry_context(anomaly: dict) -> dict | None:
    """盘前主升支撑（含首阴/缩量十字星）触及后的放量回拉。"""
    detail = anomaly.get("detail") or {}
    signal_type = str(detail.get("signal_type") or "")
    if not (
        (
            detail.get("main_wave_shape_pullback_confirmed")
            or detail.get("main_wave_support_reclaim_confirmed")
        )
        and detail.get("trend_setup_confirmed")
        and detail.get("detection_pool_member")
        and signal_type
        in {"main_wave_shape_pullback_reclaim", "main_wave_support_reclaim"}
    ):
        return None
    if _is_recent_high_board_risk(detail):
        return None
    if not _intraday_acceleration_volume_ready(detail) or _has_acceleration_distribution_risk(detail):
        return None
    is_shape_signal = signal_type == "main_wave_shape_pullback_reclaim"
    if is_shape_signal and not (
        detail.get("main_wave_strict_pullback_confirmed")
        and detail.get("main_wave_core_sector_confirmed")
        and detail.get("large_order_inflow_confirmed")
    ):
        return None

    change_pct = _safe_float(detail.get("change_pct"))
    rebound_pct = _safe_float(detail.get("intraday_rebound_pct"))
    rolling_change_pct = _safe_float(detail.get("rolling_60s_change_pct"))
    support_break_pct = _safe_float(detail.get("support_break_pct"))
    volume_ratio = _safe_float(detail.get("volume_ratio"), 1.0)
    support_strength = _safe_float(detail.get("support_strength_score"))
    net_amount = _safe_float(detail.get("main_net_inflow"))
    inflow_pct = _safe_float(detail.get("main_net_inflow_pct"))
    source = str(detail.get("source") or "")
    is_stale = bool(detail.get("is_stale"))
    if not (
        -4.5 <= change_pct <= 1.8
        and support_break_pct >= -settings.ANOMALY_MAIN_WAVE_PULLBACK_MAX_SUPPORT_BREAK_PCT
        and rebound_pct >= settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_REBOUND_PCT
        and rolling_change_pct >= settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_MOMENTUM_PCT
        and settings.ANOMALY_MAIN_WAVE_PULLBACK_MIN_VOLUME_RATIO
        <= volume_ratio
        <= settings.ANOMALY_MAIN_WAVE_PULLBACK_MAX_VOLUME_RATIO
        and _safe_int(detail.get("main_wave_core_confirmation_count")) >= 2
    ):
        return None
    fund_live = source in {"eastmoney_main_fund", "fund_flow"} and not is_stale
    if fund_live and (net_amount <= -1e8 or inflow_pct <= -4.0):
        return None

    shape_label = str(detail.get("pullback_shape_label") or "主升浪回踩")
    anchor_label = (
        f"{shape_label}低点"
        if signal_type == "main_wave_shape_pullback_reclaim"
        else shape_label
    )
    result_label = (
        f"{shape_label}低点回收"
        if signal_type == "main_wave_shape_pullback_reclaim"
        else "主升浪支撑回收"
    )
    reasons = [
        f"{anchor_label}{_safe_float(detail.get('pullback_anchor_low')):.2f}已触及",
        f"日内低点回拉{rebound_pct:.1f}%",
        f"滚动60秒上涨{rolling_change_pct:+.2f}%并有增量成交",
        "形态低点为盘前锚点，不把事后全天最低点作为信号",
    ]
    if support_strength >= 65:
        reasons.append(f"盘口承接强({support_strength:.0f})")
    if fund_live and net_amount > 0:
        reasons.append(f"实时主力净流入占比{inflow_pct:.1f}%")
    if is_shape_signal and detail.get("large_order_inflow_confirmed"):
        reasons.append(
            f"大单净流入{_safe_float(detail.get('large_order_net_inflow')) / 1e6:.0f}百万元"
        )
    core_driver = detail.get("main_wave_core_sector_driver") or {}
    if is_shape_signal and detail.get("main_wave_core_sector_confirmed"):
        reasons.append(
            f"{core_driver.get('sector_name') or '主驱动板块'}处于正向周期并形成扩散"
        )
    elif _has_strong_low_absorb_sector(detail):
        reasons.append("主驱动板块同步转强")
    return {
        "label": result_label,
        "reasons": reasons[:7],
        "source_label": _format_data_source_label(source),
        "as_of": str(detail.get("as_of") or detail.get("updated_at") or date.today()),
        "entry_grade": _entry_grade(7, direct_execute=False),
    }


def _build_low_absorb_entry_context(anomaly: dict, peer_events: list[dict] | None = None) -> dict | None:
    detail = _signal_fund_detail(anomaly)
    anomaly = {**anomaly, "detail": detail}
    if _is_recent_high_board_risk(detail):
        return None
    if str(detail.get("signal_type") or "") in {
        "underwater_acceleration",
        "underwater_reversal",
    }:
        return _build_underwater_reversal_entry_context(anomaly, peer_events)
    if str(detail.get("signal_type") or "") == "trend_support_touch":
        return _build_trend_support_touch_entry_context(anomaly)
    if str(detail.get("signal_type") or "") == "main_wave_green_open_reclaim":
        return _build_main_wave_green_open_entry_context(anomaly)
    if str(detail.get("signal_type") or "") in {
        "main_wave_shape_pullback_reclaim",
        "main_wave_support_reclaim",
    }:
        return _build_main_wave_shape_pullback_entry_context(anomaly)
    if str(detail.get("signal_type") or "") == "second_wave_restart":
        return _build_second_wave_restart_entry_context(anomaly)

    change_pct = _safe_float(detail.get("change_pct"))
    volume_ratio = _safe_float(detail.get("volume_ratio"), 1.0)
    amplitude = _safe_float(detail.get("amplitude"))
    turnover = _safe_float(detail.get("turnover"))
    support_strength = _safe_float(detail.get("support_strength_score"))
    imbalance = _safe_float(detail.get("orderbook_imbalance"))
    bid_depth_5 = _safe_float(detail.get("bid_depth_5"))
    ask_depth_5 = _safe_float(detail.get("ask_depth_5"))
    min5_change = _safe_float(detail.get("min5_change"))
    net_amount = _safe_float(detail.get("main_net_inflow"))
    inflow_pct = _safe_float(detail.get("main_net_inflow_pct"))
    distance_to_ma5_pct = _safe_float(detail.get("distance_to_ma5_pct"))
    intraday_rebound_pct = _safe_float(detail.get("intraday_rebound_pct"))
    price_vs_avg_pct = _safe_float(detail.get("price_vs_avg_pct"))
    low_absorb_type = str(detail.get("low_absorb_type") or "")
    source = str(detail.get("source") or "")
    as_of = str(detail.get("as_of") or detail.get("updated_at") or date.today())
    is_stale = bool(detail.get("is_stale"))

    if is_stale:
        return None
    if change_pct < -3.5 or change_pct > 2.8:
        return None
    if volume_ratio < 0.75 or volume_ratio > 2.6:
        return None
    if amplitude > 6.8 or turnover > 14.5:
        return None

    ma5_pullback = "ma5_pullback" in low_absorb_type
    green_reversal = "green_reversal" in low_absorb_type
    trend_support_reclaim = bool(
        detail.get("trend_setup_confirmed")
        and low_absorb_type == "trend_driver_support_reclaim"
    )
    is_pre_board_reclaim = bool(
        trend_support_reclaim
        and str(detail.get("trend_setup_source") or "").startswith("pre_board_")
    )
    is_tenbagger_pullback = bool(
        trend_support_reclaim and detail.get("tenbagger_pullback_confirmed")
    )
    if not (ma5_pullback or green_reversal or trend_support_reclaim):
        return None

    peer_events = peer_events or []
    has_positive_driver = _has_positive_driver_sector(detail)
    has_strong_low_absorb_driver = _has_strong_low_absorb_sector(detail)
    heavy_outflow = net_amount <= -1e8 or inflow_pct <= -4.0
    if heavy_outflow:
        return None
    if support_strength < 58 and net_amount <= 0:
        return None
    has_breakthrough = any(
        str(item.get("event_type") or "") == "breakthrough" and _safe_float(item.get("score")) >= 75
        for item in peer_events
    )
    has_confirmed_capital = _has_confirmed_capital_inflow(peer_events)

    reasons: list[str] = []
    confirmation_score = 0
    if ma5_pullback and -1.2 <= distance_to_ma5_pct <= 2.4:
        confirmation_score += 2
        reasons.append(f"上升通道回踩MA5，偏离{distance_to_ma5_pct:+.1f}%")
    if green_reversal and intraday_rebound_pct >= 0.8:
        confirmation_score += 2
        reasons.append(f"绿盘下探后回抽{intraday_rebound_pct:.1f}%")
    if trend_support_reclaim and -1.5 <= _safe_float(detail.get("support_gap_pct")) <= 3.0:
        confirmation_score += 2
        reasons.append(
            f"趋势支撑{_safe_float(detail.get('trend_support')):.2f}回踩后收回"
        )
    if support_strength >= 68 and imbalance >= 0.10 and bid_depth_5 > ask_depth_5 * 1.10:
        confirmation_score += 2
        reasons.append(f"盘口承接强({support_strength:.0f})，买五明显强于卖五")
    elif support_strength >= 60 and bid_depth_5 > ask_depth_5:
        confirmation_score += 1
        reasons.append(f"盘口承接偏强({support_strength:.0f})")
    if price_vs_avg_pct >= -0.1:
        confirmation_score += 1
        reasons.append("重新站回VWAP附近")
    if min5_change >= 0.5:
        confirmation_score += 1
        reasons.append(f"5分钟动能转正 {min5_change:+.1f}%")
    if net_amount > 0 and inflow_pct >= 3.0:
        confirmation_score += 1
        reasons.append(f"主力净流入占比 {inflow_pct:.1f}%")
    if net_amount >= 1.5e8 or inflow_pct >= 5.0:
        confirmation_score += 1
        reasons.append(f"资金净额 {_format_net_amount_yi(net_amount)}")
    if has_strong_low_absorb_driver:
        confirmation_score += 1
        reasons.append("主驱动板块同步转强")
    if has_breakthrough:
        confirmation_score += 1
        reasons.append("同股叠加突破预备信号")
    if has_confirmed_capital:
        confirmation_score += 1
        reasons.append("同股资金确认流入")
    if is_pre_board_reclaim:
        reasons.insert(
            0,
            f"{detail.get('long_cycle_regime_label') or '长周期准备态'}，形态只负责入池",
        )

    if confirmation_score < 4:
        return None
    if is_pre_board_reclaim and (
        not detail.get("rolling_60s_confirmed")
        or not detail.get("rolling_60s_path_confirmed", True)
        or _safe_float(detail.get("rolling_60s_amount_pace_ratio"))
        < settings.ANOMALY_ACCELERATION_MIN_AMOUNT_PACE_RATIO
        or price_vs_avg_pct < -0.1
        or change_pct > 3.8
        or _has_acceleration_distribution_risk(detail)
        or (
            bool(detail.get("long_cycle_regime"))
            and not detail.get("long_cycle_shape_ready")
        )
        or sum((
            support_strength >= 65,
            net_amount > 0 and inflow_pct >= 2.5,
            has_strong_low_absorb_driver,
        )) < 2
    ):
        return None
    if is_tenbagger_pullback and (
        _safe_float(detail.get("tenbagger_score")) < 72.0
        or change_pct > 1.8
        or not _intraday_acceleration_volume_ready(detail)
        or price_vs_avg_pct < -0.1
        or _has_acceleration_distribution_risk(detail)
        or sum((
            support_strength >= 65,
            net_amount > 0 and inflow_pct >= 2.5,
            has_strong_low_absorb_driver,
        )) < 2
    ):
        return None

    direct_execute = (
        not is_tenbagger_pullback
        and not is_pre_board_reclaim
        and
        confirmation_score >= 6
        and support_strength >= 68
        and -2.5 <= change_pct <= 1.8
        and volume_ratio <= 2.1
        and amplitude <= 5.2
        and _has_qualified_main_fund_source(detail)
        and (net_amount >= 5e7 or inflow_pct >= 5.0)
        and (has_strong_low_absorb_driver or net_amount > 0 or has_confirmed_capital)
        and not heavy_outflow
    )

    label = str(detail.get("signal_label") or "")
    if not label:
        if green_reversal and ma5_pullback:
            label = "绿盘回踩MA5弱转强"
        elif green_reversal:
            label = "绿盘弱转强低吸"
        elif trend_support_reclaim:
            label = "趋势驱动支撑回收"
        else:
            label = "上升通道回踩MA5"

    return {
        "label": label,
        "reasons": reasons,
        "source_label": _format_data_source_label(source),
        "as_of": as_of,
        "entry_grade": _entry_grade(confirmation_score, direct_execute=direct_execute),
    }


def _build_positive_acceleration_entry_context(anomaly: dict) -> dict | None:
    """把红盘放量二次加速转换成A2轻提醒，避免等到高位再追。"""
    detail = anomaly.get("detail") or {}
    if not (
        detail.get("positive_acceleration_confirmed")
        and (
            detail.get("detection_pool_member")
            or detail.get("market_wide_detection")
        )
        and str(detail.get("signal_type") or "") == "positive_acceleration"
    ):
        return None
    if _is_recent_high_board_risk(detail) or bool(detail.get("is_false_breakout")):
        return None
    if (
        not _intraday_acceleration_volume_ready(detail)
        or _has_acceleration_distribution_risk(detail)
    ):
        return None

    change_pct = _safe_float(detail.get("change_pct"))
    volume_ratio = _safe_float(detail.get("volume_ratio"), 1.0)
    amplitude = _safe_float(detail.get("amplitude"))
    turnover = _safe_float(detail.get("turnover"))
    acceleration_pct = (
        _safe_float(detail.get("rolling_60s_change_pct"))
        if detail.get("positive_acceleration_rolling60")
        else _safe_float(detail.get("scan_change_pct"))
    )
    near_high_ratio = _safe_float(detail.get("near_high_ratio"))
    price_vs_avg_pct = _safe_float(detail.get("price_vs_avg_pct"))
    quality_score = _safe_float(detail.get("quality_score"))
    support_strength = _safe_float(detail.get("support_strength_score"))
    imbalance = _safe_float(detail.get("orderbook_imbalance"))
    bid_depth_5 = _safe_float(detail.get("bid_depth_5"))
    ask_depth_5 = _safe_float(detail.get("ask_depth_5"))
    net_amount = _safe_float(detail.get("main_net_inflow"))
    inflow_pct = _safe_float(detail.get("main_net_inflow_pct"))
    source = str(detail.get("source") or "")
    fund_data_degraded = bool(detail.get("fund_data_degraded"))
    core_confirmation_count = _safe_int(
        detail.get("positive_acceleration_core_confirmation_count")
    )
    is_limit_up_acceleration = bool(detail.get("positive_acceleration_limit_up"))
    is_pre_limit_up_acceleration = bool(
        detail.get("positive_acceleration_pre_limit_up")
    )
    is_rolling_60s = bool(detail.get("positive_acceleration_rolling60"))
    is_catchup = bool(
        detail.get("positive_acceleration_catchup")
        or is_pre_limit_up_acceleration
        or is_limit_up_acceleration
    )
    if is_limit_up_acceleration:
        max_change_pct = (
            settings.ANOMALY_POSITIVE_ACCELERATION_LIMIT_UP_MAX_CHANGE_PCT
        )
        min_momentum_pct = (
            settings.ANOMALY_POSITIVE_ACCELERATION_LIMIT_UP_MIN_MOMENTUM_PCT
        )
    elif is_pre_limit_up_acceleration:
        max_change_pct = (
            settings.ANOMALY_POSITIVE_ACCELERATION_LIMIT_UP_MAX_CHANGE_PCT
        )
        min_momentum_pct = (
            settings.ANOMALY_POSITIVE_ACCELERATION_PRE_LIMIT_MIN_MOMENTUM_PCT
        )
    elif is_catchup:
        max_change_pct = settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MAX_CHANGE_PCT
        min_momentum_pct = (
            settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MIN_MOMENTUM_PCT
        )
    else:
        max_change_pct = settings.ANOMALY_POSITIVE_ACCELERATION_MAX_CHANGE_PCT
        min_momentum_pct = (
            settings.ANOMALY_ROLLING_60S_MIN_CHANGE_PCT
            if is_rolling_60s
            else settings.ANOMALY_POSITIVE_ACCELERATION_MIN_MOMENTUM_PCT
        )
    max_amplitude = (
        settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MAX_AMPLITUDE
        if is_catchup
        else 9.0
    )
    max_turnover = (
        settings.ANOMALY_POSITIVE_ACCELERATION_CATCHUP_MAX_TURNOVER
        if is_catchup
        else 18.0
    )

    if not (
        (
            settings.ANOMALY_UNDERWATER_ACCELERATION_MIN_CHANGE_PCT
            if is_rolling_60s
            else settings.ANOMALY_POSITIVE_ACCELERATION_MIN_CHANGE_PCT
        )
        <= change_pct
        <= max_change_pct
        and acceleration_pct >= min_momentum_pct
        and (
            settings.ANOMALY_ROLLING_60S_MIN_VOLUME_RATIO
            if is_rolling_60s
            else settings.ANOMALY_POSITIVE_ACCELERATION_MIN_VOLUME_RATIO
        )
        <= volume_ratio
        <= (
            settings.ANOMALY_ROLLING_60S_MAX_VOLUME_RATIO
            if is_rolling_60s
            else settings.ANOMALY_POSITIVE_ACCELERATION_MAX_VOLUME_RATIO
        )
        and (
            bool(detail.get("rolling_60s_path_confirmed", True))
            if is_rolling_60s
            else near_high_ratio
            >= settings.ANOMALY_POSITIVE_ACCELERATION_MIN_NEAR_HIGH_RATIO
        )
        and price_vs_avg_pct >= (-0.8 if is_rolling_60s else 0.0)
        and quality_score >= 68
        and amplitude <= max_amplitude
        and turnover <= max_turnover
    ):
        return None

    fund_live = source in {"eastmoney_main_fund", "fund_flow"} and not fund_data_degraded
    orderbook_confirmed = bool(
        support_strength >= 60
        and imbalance >= 0.05
        and bid_depth_5 > ask_depth_5 * 1.03
    )
    fund_confirmed = bool(
        fund_live
        and net_amount > 0
        and (inflow_pct >= 2.0 or net_amount >= 5e7)
    )
    sector_confirmed = _has_positive_driver_sector(detail)
    strong_tape_confirmed = bool(
        is_rolling_60s
        and str(detail.get("rolling_60s_tier") or "") == "strong"
        and _intraday_acceleration_volume_ready(detail)
        and bool(detail.get("rolling_60s_path_confirmed"))
    )
    minimum_core_confirmations = max(
        1,
        _safe_int(
            detail.get("positive_acceleration_min_core_confirmation_count"),
            2,
        ),
    )
    if core_confirmation_count < minimum_core_confirmations:
        return None

    fund_flow_divergence = bool(
        fund_live and (net_amount <= -1e8 or inflow_pct <= -4.0)
    )
    # 资金快照的采样频率低于逐笔盘口。只有同窗强急拉、增量成交、买盘承接，
    # 且扫描层已确认形态池、板块共振或极强盘口之一时，才允许把负资金
    # 读数降级为“快照背离”；该路径仍然只是B类观察，不生成追涨执行指令。
    fund_flow_divergence_overridden = bool(
        detail.get("fund_flow_divergence_overridden")
        and strong_tape_confirmed
        and orderbook_confirmed
    )
    if fund_flow_divergence and not fund_flow_divergence_overridden:
        return None
    if not (orderbook_confirmed or fund_confirmed):
        return None

    previous_change_pct = _safe_float(detail.get("previous_change_pct"))
    reasons = [
        (
            f"滚动{_safe_float(detail.get('rolling_60s_interval_sec')):.0f}秒"
            f"累计上涨{_safe_float(detail.get('rolling_60s_change_pct')):+.2f}%"
            if is_rolling_60s
            else f"涨幅从{previous_change_pct:+.1f}%加速至{change_pct:+.1f}%"
        ),
        (
            f"短周期动能{acceleration_pct:+.1f}% / "
            f"成交速率{_safe_float(detail.get('rolling_60s_amount_pace_ratio')):.1f}倍"
            if is_rolling_60s
            else f"短周期动能{acceleration_pct:+.1f}% / "
            f"成交速率{_safe_float(detail.get('intraday_amount_pace_ratio')):.1f}倍"
        ),
        (
            "拉升末端保持在60秒窗口高位且站稳VWAP附近"
            if is_rolling_60s
            else "贴近日内高点且站稳VWAP附近"
        ),
        (
            "极速封板只作强度确认；不开板追价，等待次日承接或首次回踩"
            if is_limit_up_acceleration
            else "涨停前加速仅作强度预警；不追价，等待回踩承接或次日确认"
            if is_pre_limit_up_acceleration
            else "快速跨越常规窗口，仅作追赶预警，等待首次回踩确认"
            if is_catchup
            else "属于抢跑观察，不等涨停也不作为追板指令"
        ),
    ]
    confirmation_score = 3
    if orderbook_confirmed:
        confirmation_score += 2
        reasons.append(f"盘口承接强({support_strength:.0f})，买五强于卖五")
    if fund_confirmed:
        confirmation_score += 1
        reasons.append(f"实时主力净流入占比{inflow_pct:.1f}%")
    if sector_confirmed:
        confirmation_score += 1
        reasons.append("主驱动板块同步转强")
    if fund_data_degraded:
        reasons.append("资金接口降级，本次仅按盘口与板块轻提醒")
    elif fund_flow_divergence_overridden:
        reasons.append("资金快照滞后于即时量价，本次仅作强急拉观察")

    # 0.3%~0.6%若没有三项核心共振只在页面观察；0.6%以上沿用两项确认。
    if is_rolling_60s and not bool(detail.get("rolling_60s_alert_pushable")):
        confirmation_score = min(confirmation_score, 4)

    return {
        "label": str(detail.get("signal_label") or "红盘放量二次加速预警"),
        "reasons": reasons[:7],
        "source_label": _format_data_source_label(source),
        "as_of": str(detail.get("as_of") or detail.get("updated_at") or date.today()),
        "entry_grade": _entry_grade(confirmation_score, direct_execute=False),
    }


def _build_breakthrough_entry_context(anomaly: dict, peer_events: list[dict] | None = None) -> dict | None:
    detail = _signal_fund_detail(anomaly)
    anomaly = {**anomaly, "detail": detail}
    signal_type = str(detail.get("signal_type") or "")
    if signal_type == "positive_acceleration":
        return _build_positive_acceleration_entry_context(anomaly)
    if signal_type == "momentum_burst" and (
        not _intraday_acceleration_volume_ready(detail)
        or _has_acceleration_distribution_risk(detail)
    ):
        return None
    quality_score = _safe_float(detail.get("quality_score"))
    volume_ratio = _safe_float(detail.get("volume_ratio"), 1.0)
    change_pct = _safe_float(detail.get("change_pct"))
    amplitude = _safe_float(detail.get("amplitude"))
    turnover = _safe_float(detail.get("turnover"))
    support_strength = _safe_float(detail.get("support_strength_score"))
    imbalance = _safe_float(detail.get("orderbook_imbalance"))
    bid_depth_5 = _safe_float(detail.get("bid_depth_5"))
    ask_depth_5 = _safe_float(detail.get("ask_depth_5"))
    ma_status = str(detail.get("ma_status") or "")
    pullback_probability = str(detail.get("pullback_probability") or "")
    days_near_pressure = _safe_int(detail.get("days_near_pressure"))
    is_false_breakout = bool(detail.get("is_false_breakout"))
    source_label = "Tencent 实时行情"
    as_of = str(detail.get("as_of") or detail.get("updated_at") or date.today())
    is_pre_board_breakthrough = bool(
        signal_type == "trend_driver_breakthrough"
        and str(detail.get("trend_setup_source") or "").startswith("pre_board_")
    )
    if signal_type == "sector_core_laggard_ignition":
        confirmations = list(detail.get("sector_core_laggard_confirmations") or [])
        driver = detail.get("sector_core_driver") or {}
        if (
            not detail.get("sector_core_laggard_confirmed")
            or quality_score < 80
            or _safe_float(driver.get("strength_score")) < 70
            or _safe_float(driver.get("fund_flow")) <= 0
            or _safe_int(driver.get("limit_up_count")) < 3
            or not detail.get("rolling_60s_confirmed")
            or not detail.get("rolling_60s_path_confirmed", True)
            or _safe_float(detail.get("rolling_60s_change_pct"))
            < settings.ANOMALY_ROLLING_60S_MIN_CHANGE_PCT
            or _safe_float(detail.get("rolling_60s_amount_pace_ratio"))
            < settings.ANOMALY_ACCELERATION_MIN_AMOUNT_PACE_RATIO
            or not (-2.5 <= change_pct <= 4.8)
            or not (1.15 <= volume_ratio <= 3.2)
            or amplitude > 8.0
            or turnover > 15.0
            or _safe_float(detail.get("price_vs_avg_pct")) < -0.2
            or _has_acceleration_distribution_risk(detail)
            or len(confirmations) < 4
        ):
            return None
        return {
            "label": "主线核心补涨点火",
            "reasons": confirmations[:7],
            "source_label": source_label,
            "as_of": as_of,
            "entry_grade": "A2 盘口确认后执行",
        }
    if is_pre_board_breakthrough and (
        not detail.get("rolling_60s_confirmed")
        or not detail.get("rolling_60s_path_confirmed", True)
        or _safe_float(detail.get("rolling_60s_amount_pace_ratio"))
        < settings.ANOMALY_ACCELERATION_MIN_AMOUNT_PACE_RATIO
        or _safe_float(detail.get("price_vs_avg_pct")) < -0.1
        or change_pct > 6.5
        or _has_acceleration_distribution_risk(detail)
        or (
            bool(detail.get("long_cycle_regime"))
            and not detail.get("long_cycle_shape_ready")
        )
    ):
        return None

    if signal_type == "leader_linkage_confirmation":
        confirmations = list(detail.get("leader_linkage_confirmations") or [])
        if (
            not detail.get("leader_linkage_confirmed")
            or _safe_float(detail.get("leader_recognition_score")) < 68
            or _safe_float(detail.get("linkage_score")) < 72
            or _safe_float(detail.get("business_relevance_score")) < 76
            or _safe_float(detail.get("follower_shape_score")) < 68
            or _safe_float(detail.get("theme_alignment_score")) < 75
            or len(confirmations) < 6
            or not _has_positive_driver_sector(detail)
            or not bool(detail.get("vwap_reclaimed"))
            or not bool(detail.get("leader_near_high"))
            or (
                support_strength < 60
                and _safe_float(detail.get("main_net_inflow")) <= 0
            )
        ):
            return None
        leader_name = str(detail.get("leader_name") or detail.get("leader_code") or "龙头A")
        return {
            "label": f"看{leader_name}做B二次确认",
            "reasons": confirmations[:7],
            "source_label": source_label,
            "as_of": as_of,
            "entry_grade": "A2 盘口确认后执行",
        }

    if signal_type in {"event_relay_confirmation", "second_board_relay_confirmation"}:
        is_second_board_relay = signal_type == "second_board_relay_confirmation"
        confirmations = list(detail.get("event_relay_confirmations") or [])
        if (
            not detail.get("event_relay_confirmed")
            or (
                is_second_board_relay
                and _safe_float(detail.get("relay_quality_score")) < 68
            )
            or (
                not is_second_board_relay
                and _safe_float(detail.get("event_score")) < 58
            )
            or len(confirmations) < 6
            or not _has_positive_driver_sector(detail)
            or not bool(detail.get("vwap_reclaimed"))
            or (
                support_strength < 60
                and _safe_float(detail.get("main_net_inflow")) <= 0
            )
        ):
            return None
        event_title = str(detail.get("event_title") or "")
        reasons = ([f"直接事件: {event_title[:54]}"] if event_title else []) + confirmations[:6]
        return {
            "label": "首板冲二板二次确认" if is_second_board_relay else "重大事件接力二次确认",
            "reasons": reasons[:7],
            "source_label": source_label,
            "as_of": as_of,
            "entry_grade": "A2 盘口确认后执行",
        }

    if signal_type == "old_hot_oversold_repair":
        driver = detail.get("old_hot_repair_driver") or {}
        strong_extension_confirmed = bool(detail.get("strong_extension_confirmed"))
        driver_ready = bool(
            detail.get("old_hot_repair_confirmed")
            and _safe_float(driver.get("fund_flow")) > 0
            and _safe_float(driver.get("change_pct")) >= 1.0
            and _safe_float(driver.get("strength_score")) >= 60
            and (
                _safe_int(driver.get("limit_up_count")) >= 2
                or _safe_int(driver.get("peer_count")) >= 2
            )
        )
        if (
            not driver_ready
            or quality_score < 84
            or change_pct < 3.5
            or change_pct > (9.3 if strong_extension_confirmed else 8.6)
            or volume_ratio < 0.8
            or volume_ratio > (2.6 if strong_extension_confirmed else 2.4)
            or turnover < 4.0
            or turnover > 18.0
            or amplitude > (12.0 if strong_extension_confirmed else 9.5)
            or _safe_int(detail.get("historical_board_like_count")) < 2
            or _safe_int(detail.get("last_board_age")) < 8
            or _safe_float(detail.get("return_20d")) > -15.0
            or _safe_float(detail.get("position_120")) > 0.45
            or not (detail.get("near_intraday_high") and detail.get("vwap_reclaimed"))
        ):
            return None

        reasons = [
            f"历史高标记忆{_safe_int(detail.get('historical_board_like_count'))}次强板",
            f"前20日回撤{_safe_float(detail.get('return_20d')):.1f}% / 120日位置{_safe_float(detail.get('position_120')):.0%}",
            (
                f"{driver.get('sector_name') or '主驱动板块'}"
                f"涨{_safe_float(driver.get('change_pct')):.1f}% / "
                f"涨停{_safe_int(driver.get('limit_up_count'))}家"
            ),
            f"首日温和修复，量比{volume_ratio:.1f} / 换手{turnover:.1f}%",
        ]
        reasons.extend(list(detail.get("old_hot_repair_confirmations") or [])[:2])
        return {
            "label": "旧高标超跌修复买点",
            "reasons": reasons[:6],
            "source_label": source_label,
            "as_of": as_of,
            "entry_grade": _entry_grade(max(5, len(reasons)), direct_execute=False),
        }

    if signal_type == "sector_repair_reversal":
        driver = detail.get("sector_repair_driver") or {}
        driver_ready = bool(
            detail.get("sector_repair_confirmed")
            and _safe_float(driver.get("fund_flow")) > 0
            and _safe_float(driver.get("change_pct")) >= 2.0
            and _safe_float(driver.get("strength_score")) >= 65
            and _safe_int(driver.get("limit_up_count")) >= 3
            and _safe_int(driver.get("peer_count")) >= 2
        )
        if (
            not driver_ready
            or quality_score < 82
            or change_pct < 1.5
            or change_pct > 6.2
            or volume_ratio < 0.85
            or volume_ratio > 2.8
            or turnover > 13.0
            or amplitude > 7.5
            or _safe_float(detail.get("return_20d")) > -6.0
            or not (detail.get("near_intraday_high") or detail.get("vwap_reclaimed"))
        ):
            return None

        reasons = [
            (
                f"{driver.get('sector_name') or '主驱动板块'}"
                f"涨{_safe_float(driver.get('change_pct')):.1f}% / "
                f"涨停{_safe_int(driver.get('limit_up_count'))}家"
            ),
            f"前20日回撤{_safe_float(detail.get('return_20d')):.1f}%，处于低位修复区",
            f"温和放量，量比{volume_ratio:.1f}",
        ]
        reasons.extend(list(detail.get("sector_repair_confirmations") or [])[:2])
        if detail.get("fundamental_confirmed"):
            reasons.append("实时估值/增速字段通过初筛，定期报告仍需复核")
        # 超跌反包仍有均线下行风险，只给A2轻提醒，不能伪装成A1趋势突破。
        return {
            "label": "强修复启动买点",
            "reasons": reasons[:6],
            "source_label": source_label,
            "as_of": as_of,
            "entry_grade": _entry_grade(max(5, len(reasons)), direct_execute=False),
        }

    if is_false_breakout:
        return None
    if _is_recent_high_board_risk(detail):
        return None
    if quality_score < 72:
        return None
    if volume_ratio < 1.5:
        return None
    if ma_status not in {"long", "multi_long"}:
        return None
    if pullback_probability == "high":
        return None
    if change_pct > 7.5 or amplitude > 10.0:
        return None

    reasons: list[str] = []
    confirmation_score = 0
    if quality_score >= 85:
        confirmation_score += 2
        reasons.append(f"突破质量高({quality_score:.0f})")
    elif quality_score >= 78:
        confirmation_score += 1
        reasons.append(f"突破质量较好({quality_score:.0f})")
    if pullback_probability == "low":
        confirmation_score += 2
        reasons.append("回踩概率低，适合等回踩确认买点")
    elif pullback_probability == "medium":
        confirmation_score += 1
        reasons.append("回踩概率中等，可等确认后介入")
    if support_strength >= 60 and imbalance >= 0.08 and bid_depth_5 > ask_depth_5 * 1.1:
        confirmation_score += 1
        reasons.append(f"盘口承接偏强({support_strength:.0f})，买五强于卖五")
    if days_near_pressure >= 3:
        confirmation_score += 1
        reasons.append(f"压力位附近蓄势 {days_near_pressure} 天")
    if _has_confirmed_capital_inflow(peer_events):
        confirmation_score += 1
        reasons.append("伴随资金确认流入")
    if change_pct <= 5.5:
        confirmation_score += 1
        reasons.append(f"涨幅 {change_pct:+.1f}% 尚未过热")
    if is_pre_board_breakthrough:
        reasons.insert(
            0,
            f"{detail.get('long_cycle_regime_label') or '长周期准备态'}，仅在60秒增量成交确认后生效",
        )

    if confirmation_score < 4:
        return None
    has_positive_driver = _has_positive_driver_sector(detail)

    return {
        "label": "突破买点",
        "reasons": reasons,
        "source_label": source_label,
        "as_of": as_of,
        "entry_grade": _entry_grade(
            confirmation_score,
            direct_execute=(
                confirmation_score >= 5
                and quality_score >= 85
                and pullback_probability == "low"
                and change_pct <= 4.8
                and amplitude <= 5.0
                and turnover <= 15.0
                and volume_ratio <= 2.5
                and support_strength >= 65
                and has_positive_driver
            ),
        ),
    }


def _build_anomaly_push_message(anomaly: dict, peer_events: list[dict] | None = None):
    payload = _build_anomaly_push_message_data(anomaly, peer_events)
    return payload.get("message") if payload else None


def _build_early_observation_push_message(
    anomaly: dict,
    peer_events: list[dict] | None = None,
) -> PushMessage | None:
    """为全市场强异动生成独立观察消息，不复用买点推送标记。"""
    if not _is_pushworthy_early_observation(anomaly):
        return None
    entry = _build_anomaly_push_message_data(anomaly, peer_events or [])
    if not entry or entry.get("message") is None:
        return None
    detail = anomaly.get("detail") or {}
    message = entry["message"]
    message.title = f"⚡ 异动观察｜{anomaly.get('name') or ''} ({anomaly.get('code') or ''})｜{entry.get('signal_label') or '量价异动'}"
    message.msg_type = "info"
    message.priority = 5
    message.content = message.content.replace(
        "执行等级: A2 盘口确认后执行",
        "执行等级: B类观察候选",
    )
    message.extra = dict(message.extra or {})
    original_signal_variant = str(message.extra.get("signal_variant") or "")
    rapid_rise_strong_confirmed = bool(
        message.extra.get("rapid_rise_strong_confirmed")
        and original_signal_variant == "positive_acceleration_strong"
    )
    pre_limit_up_confirmed = bool(
        message.extra.get("pre_limit_up_confirmed")
        and original_signal_variant == "positive_acceleration_pre_limit"
    )
    message.extra.update({
        "setup_grade": "B类观察候选",
        "setup_grade_display": "B类观察候选",
        "alert_tier": "observation",
        "buypoint_pushable": False,
        "observation_only": True,
        "signal_identity": f"{_anomaly_signal_identity(anomaly)}|observation",
        # 保留strong身份才能使用独立保留额度；observation_only仍明确禁止
        # 被绩效/前端解释成追涨买点。
        "signal_variant": (
            "positive_acceleration_strong"
            if rapid_rise_strong_confirmed
            else "positive_acceleration_pre_limit"
            if pre_limit_up_confirmed
            else f"{str(detail.get('signal_type') or 'anomaly')}_observation"[:40]
        ),
        "rapid_rise_strong_confirmed": rapid_rise_strong_confirmed,
        "pre_limit_up_confirmed": pre_limit_up_confirmed,
    })
    message.content += (
        "\n\n**⚠️ 定位**\n"
        "这是量价异动/持仓管理观察，不是追涨买点；未持仓等待回踩支撑后止跌回收，"
        "已有持仓若进入压力区并冲高回落，应考虑分批减仓。"
    )
    return message


def _resolve_setup_grade(anomaly: dict, peer_events: list[dict] | None = None) -> str | None:
    event_type = anomaly.get("event_type") or ""
    if event_type == "limit_up":
        entry = _build_limit_up_entry_context(anomaly, peer_events)
        return entry.get("entry_grade") if entry else None
    if event_type == "limit_down":
        entry = _build_limit_down_entry_context(anomaly, peer_events)
        return entry.get("entry_grade") if entry else None
    if event_type in {"capital", "pump_dump"}:
        confirmation = _build_capital_confirmation_context(anomaly, peer_events)
        if not confirmation or confirmation.get("direction") != "inflow":
            return None
        return confirmation.get("entry_grade")
    if event_type == "low_absorb":
        entry = _build_low_absorb_entry_context(anomaly, peer_events)
        grade = entry.get("entry_grade") if entry else None
        return "B类观察候选" if grade and _is_observation_only_anomaly(anomaly) else grade
    if event_type == "breakthrough":
        entry = _build_breakthrough_entry_context(anomaly, peer_events)
        grade = entry.get("entry_grade") if entry else None
        return "B类观察候选" if grade and _is_observation_only_anomaly(anomaly) else grade
    return None


def _select_pushworthy_anomalies(
    current_anomalies: list[dict],
    previous_anomalies: list[dict] | None = None,
    min_grade: str = "A1 可直接执行",
    watchlist_a2_only: bool = False,
) -> list[dict]:
    previous_anomalies = previous_anomalies or []

    def _group_by_code(items: list[dict]) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for item in items:
            code = str(item.get("code") or "")
            if code:
                grouped.setdefault(code, []).append(item)
        return grouped

    current_by_code = _group_by_code(current_anomalies)
    previous_by_code = _group_by_code(previous_anomalies)

    previous_state_by_identity: dict[str, dict] = {}
    for item in previous_anomalies:
        identity = _anomaly_signal_identity(item)
        code = str(item.get("code") or "")
        peer_events = previous_by_code.get(code, [])
        previous_entry = _build_anomaly_push_message_data(item, peer_events)
        previous_grade = (
            previous_entry.get("setup_grade")
            if previous_entry
            else _resolve_setup_grade(item, peer_events)
        )
        previous_state_by_identity[identity] = {
            "grade": previous_grade,
            "buy_point_pushable": bool(
                previous_entry
                and previous_entry.get("alert_tier")
                and previous_entry.get("buypoint_pushable")
            ),
        }

    selected: list[dict] = []
    for item in current_anomalies:
        if (item.get("score") or 0) < 50:
            continue
        identity = _anomaly_signal_identity(item)
        code = str(item.get("code") or "")
        peer_events = current_by_code.get(code, [])
        current_entry = _build_anomaly_push_message_data(item, peer_events)
        current_grade = (
            current_entry.get("setup_grade")
            if current_entry
            else _resolve_setup_grade(item, peer_events)
        )
        current_buy_point_pushable = bool(
            current_entry
            and current_entry.get("alert_tier")
            and current_entry.get("buypoint_pushable")
        )
        previous_state = previous_state_by_identity.get(identity) or {}
        current_rank = _grade_rank(current_grade)
        previous_rank = _grade_rank(previous_state.get("grade"))
        previous_buy_point_pushable = bool(previous_state.get("buy_point_pushable"))
        if current_rank < _grade_rank(min_grade):
            continue
        if (
            watchlist_a2_only
            and current_rank < _grade_rank("A1 可直接执行")
            and not _is_actionable_detection_pool_anomaly(item)
        ):
            continue
        reached_new_buy_point = current_buy_point_pushable and not previous_buy_point_pushable
        if current_rank <= previous_rank and not reached_new_buy_point:
            continue
        selected.append(item)
    selected.sort(
        key=lambda item: (
            1 if _is_actionable_detection_pool_anomaly(item) else 0,
            _grade_rank(_resolve_setup_grade(item, current_by_code.get(str(item.get("code") or ""), []))),
            _safe_float(item.get("score")),
        ),
        reverse=True,
    )
    return selected


def _build_push_message_candidates(
    anomalies: list[dict],
    min_grade: str = "A1 可直接执行",
    allow_limit_up: bool = True,
    peer_anomalies: list[dict] | None = None,
) -> list[dict]:
    peer_anomalies = peer_anomalies or anomalies

    candidates_by_code: dict[str, list[dict]] = {}
    for item in anomalies:
        code = str(item.get("code") or "")
        if not code:
            continue
        candidates_by_code.setdefault(code, []).append(item)

    peer_by_code: dict[str, list[dict]] = {}
    for item in peer_anomalies:
        code = str(item.get("code") or "")
        if not code:
            continue
        peer_by_code.setdefault(code, []).append(item)

    priority_order = {
        "low_absorb": 0,
        "limit_up": 1,
        "limit_down": 2,
        "pump_dump": 3,
        "breakthrough": 4,
        "capital": 5,
    }

    selections: list[dict] = []
    sorted_codes = sorted(
        candidates_by_code.keys(),
        key=lambda code: min(
            (
                priority_order.get(str(item.get("event_type") or ""), 99),
                -_safe_float(item.get("score")),
            )
            for item in candidates_by_code.get(code, [])
        ),
    )

    for code in sorted_codes:
        peer_events = peer_by_code.get(code, [])
        if not peer_events:
            continue

        entry_cache: dict[str, dict | None] = {}

        def _entry_for(item: dict) -> dict | None:
            identity = _anomaly_signal_identity(item)
            if identity not in entry_cache:
                entry_cache[identity] = _build_anomaly_push_message_data(item, peer_events)
            return entry_cache[identity]

        changed_entries: list[dict] = []
        for item in candidates_by_code.get(code, []):
            if not allow_limit_up and str(item.get("event_type") or "") == "limit_up":
                continue
            entry = _entry_for(item)
            if entry is None:
                continue
            if min_grade and _grade_rank(entry.get("setup_grade")) < _grade_rank(min_grade):
                continue
            if not entry.get("alert_tier"):
                continue
            if not entry.get("buypoint_pushable"):
                continue
            changed_entries.append(entry)
        if not changed_entries:
            continue

        current_entries: list[dict] = []
        seen_signals: set[str] = set()
        for item in peer_events:
            entry = _entry_for(item)
            if entry is None:
                continue
            if min_grade and _grade_rank(entry.get("setup_grade")) < _grade_rank(min_grade):
                continue
            if not entry.get("alert_tier"):
                continue
            if not entry.get("buypoint_pushable"):
                continue
            signal_identity = str(entry.get("signal_identity") or "")
            if signal_identity in seen_signals:
                continue
            seen_signals.add(signal_identity)
            current_entries.append(entry)
        if not current_entries:
            continue

        changed_entries = _sort_push_entries(changed_entries)
        primary_entry = changed_entries[0]
        message = _merge_push_message_for_code(primary_entry, current_entries, changed_entries)
        if message is None:
            continue
        selections.append(
            {
                "anomaly": primary_entry.get("anomaly"),
                "anomalies": [item.get("anomaly") for item in changed_entries],
                "message": message,
                "alert_tier": message.extra.get("alert_tier") if message.extra else "",
                "primary_entry": _sort_push_entries(current_entries)[0],
                "changed_entries": changed_entries,
                "secondary_entries": [
                    item
                    for item in _sort_push_entries(current_entries)[1:]
                ],
            }
        )
    return selections


def _build_push_messages_for_anomalies(
    anomalies: list[dict],
    min_grade: str = "A1 可直接执行",
    allow_limit_up: bool = True,
    peer_anomalies: list[dict] | None = None,
) -> list:
    return [
        selection["message"]
        for selection in _build_push_message_candidates(
            anomalies,
            min_grade=min_grade,
            allow_limit_up=allow_limit_up,
            peer_anomalies=peer_anomalies,
        )
    ]


def _resolve_push_channels(channel: str):
    normalized = (channel or "feishu").lower()
    if normalized in {"all", "both"}:
        return [feishu_channel, ws_channel]
    if normalized in {"websocket", "ws"}:
        return [ws_channel]
    return [feishu_channel]


async def _push_messages_direct(messages: list, channel: str) -> list[dict]:
    targets = _resolve_push_channels(channel)
    results = []
    for message in messages:
        try:
            result = await push_scheduler.push_to_channels(
                message,
                targets,
                use_throttle=False,
            )
        except Exception as exc:
            logger.error(f"手动异动推送失败: {exc}")
            result = {
                "sent": False,
                "channels": {target.channel_name: False for target in targets},
                "throttled": False,
                "disabled": False,
                "status": "failed",
            }
        results.append({
            "title": message.title,
            "stock_code": message.stock_code,
            "sent": result.get("sent", False),
            "channels": result.get("channels", {}),
            "throttled": result.get("throttled", False),
            "disabled": result.get("disabled", False),
            "status": result.get("status", ""),
        })
    return results


def _anomaly_signal_record_id(trade_date: date, code: str, identity: str) -> str:
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]
    return f"an{trade_date:%y%m%d}{code}{digest}"


def _base_anomaly_message_identity(value: str) -> str:
    suffix = "|observation"
    return value[:-len(suffix)] if value.endswith(suffix) else value


async def _persist_anomaly_candidate_records(
    db: AsyncSession | None,
    trade_date: date,
    anomalies: list[dict],
    *,
    evaluated: bool,
    messages: list[PushMessage] | None = None,
    results: list[dict] | None = None,
    gate_trace: dict[str, dict] | None = None,
) -> dict:
    """Latest compatibility projection plus new immutable capture, never push quota."""
    if db is None or not anomalies:
        return {"status": "not_recorded", "reason": "no_database_or_candidates"}
    seen_at = datetime.now()
    if seen_at.tzinfo is not None or seen_at.date() != trade_date:
        # A historical refresh is not evidence of an observation in that session.
        return {"status": "not_recorded", "reason": "historical_or_invalid_capture_clock"}
    from app.signal.candidate_evidence import append_evaluations, freeze
    capture_id = uuid.uuid4().hex
    capture = {"status": "unavailable", "capture_id": capture_id}
    peer_by_code: dict[str, list[dict]] = {}
    for item in anomalies:
        code = str(item.get("code") or "")
        if code:
            peer_by_code.setdefault(code, []).append(item)

    observations: dict[str, dict] = {}
    for item in anomalies:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        identity = _anomaly_signal_identity(item)
        record_id = _anomaly_signal_record_id(trade_date, code, identity)
        buy_fields = _resolve_anomaly_buy_point_fields(item, peer_by_code.get(code, []))
        observation = {
            "record_id": record_id,
            "identity": identity,
            "code": code,
            "name": str(item.get("name") or code),
            "event_type": str(item.get("event_type") or ""),
            "score": _safe_float(item.get("score")),
            "grade": str(
                buy_fields.get("buy_point_grade")
                or item.get("setup_grade")
                or ""
            ),
            "pushable": bool(buy_fields.get("buy_point_pushable")),
            "blockers": [
                str(reason)
                for reason in (buy_fields.get("buy_point_blockers") or [])
                if str(reason)
            ],
            "snapshot": {
                "anomaly": item,
                "buy_point": buy_fields,
            },
        }
        previous = observations.get(record_id)
        if previous is None or observation["score"] >= previous["score"]:
            observations[record_id] = observation
    if not observations:
        return {"status": "not_recorded", "reason": "no_candidate_identities"}
    frozen_snapshots, frozen_gates = None, {}
    try:
        # Capture before the first DB await; later cache/list mutations cannot
        # replace the original candidate evidence in this call.
        frozen_snapshots = {key: json.loads(freeze(obs["snapshot"])) for key, obs in observations.items()}
        frozen_gates = json.loads(freeze(gate_trace if gate_trace is not None else {}))
        if not isinstance(frozen_gates, dict) or any(not isinstance(value, dict) for value in frozen_gates.values()):
            raise ValueError("invalid gate trace mapping")
    except (TypeError, ValueError, OverflowError):
        frozen_snapshots, frozen_gates = None, {}
        capture["reason"] = "non_json_or_nonfinite_candidate_evidence"
    attempts_by_identity = {}
    for index, message in enumerate(messages or []):
        raw_identity = str((message.extra or {}).get("signal_identity") or "")
        base_identity = _base_anomaly_message_identity(raw_identity)
        result = results[index] if results is not None and index < len(results) else None
        result = result if isinstance(result, dict) else None
        channels = (result or {}).get("channels")
        channels = channels if isinstance(channels, dict) else {}
        attempt = {
            "message_index": index, "signal_identity": raw_identity,
            "stock_code": str(message.stock_code or ""),
            "message_kind": "observation" if raw_identity.endswith("|observation") else "signal",
            "result_available": result is not None,
            "status": str((result or {}).get("status") or "")[:120],
            "channels": {str(k): v for k, v in channels.items() if isinstance(v, bool)},
        }
        for key in ("sent", "throttled", "disabled"):
            value = (result or {}).get(key)
            attempt[key] = value if isinstance(value, bool) else None
        attempts_by_identity.setdefault(base_identity, []).append(attempt)

    outcome_by_identity: dict[str, dict] = {}
    for message, result in zip(messages or [], results or []):
        if not isinstance(result, dict):
            continue  # Unknown delivery is captured separately; never invent a receipt.
        raw_identity = str((message.extra or {}).get("signal_identity") or "")
        identity = _base_anomaly_message_identity(raw_identity)
        if not identity:
            continue
        current = outcome_by_identity.get(identity)
        # 同身份若出现多个渠道结果，以成功优先，其次节流，再次失败。
        if current is None or bool(result.get("sent")) or (
            bool(result.get("throttled")) and not bool(current.get("sent"))
        ):
            outcome_by_identity[identity] = dict(result)

    record_ids = list(observations)
    base_signal_id_to_record: dict[str, str] = {}
    for record_id, observation in observations.items():
        identity = observation["identity"]
        code = observation["code"]
        base_signal_id_to_record[
            _anomaly_signal_record_id(trade_date, code, identity)
        ] = record_id
        base_signal_id_to_record[
            _anomaly_signal_record_id(trade_date, code, f"{identity}|observation")
        ] = record_id

    try:
        existing_rows = list((await db.execute(
            select(AnomalyCandidateRecord).where(
                AnomalyCandidateRecord.record_id.in_(record_ids)
            )
        )).scalars().all())
        existing_by_id = {row.record_id: row for row in existing_rows}
        sent_rows = list((await db.execute(
            select(SignalPerformance).where(
                SignalPerformance.signal_id.in_(list(base_signal_id_to_record))
            )
        )).scalars().all())
        sent_by_record: dict[str, SignalPerformance] = {}
        for row in sent_rows:
            record_id = base_signal_id_to_record.get(str(row.signal_id or ""))
            if record_id and record_id not in sent_by_record:
                sent_by_record[record_id] = row

        evidence_rows = []
        for record_id, observation in observations.items():
            row = existing_by_id.get(record_id)
            historical_sent = sent_by_record.get(record_id)
            pushed_before = bool(row is not None and row.pushed)
            outcome = outcome_by_identity.get(observation["identity"])
            blockers = list(observation["blockers"])
            status = "candidate"
            pushed_now = False
            pushed_at = None
            if outcome is not None:
                if outcome.get("sent"):
                    status = "pushed"
                    pushed_now = True
                    pushed_at = seen_at
                elif outcome.get("throttled"):
                    status = "throttled"
                    blockers.append("推送限频")
                else:
                    status = "failed"
                    blockers.append(
                        "推送渠道禁用或发送失败"
                        if outcome.get("disabled")
                        else "推送发送失败"
                    )
            elif historical_sent is not None:
                status = "pushed"
                pushed_now = True
                pushed_at = historical_sent.signal_time
            elif evaluated:
                status = "rejected"
                if observation["pushable"]:
                    blockers.append("本轮经历史绩效、单轮上限或小时预算门控后未发送")
            if row is None:
                row = AnomalyCandidateRecord(
                    record_id=record_id,
                    trade_date=trade_date,
                    code=observation["code"],
                    identity=observation["identity"],
                    first_seen_at=seen_at,
                    created_at=seen_at,
                )
                db.add(row)
                row.seen_count = 1
            else:
                row.seen_count = int(row.seen_count or 0) + 1
            row.name = observation["name"][:30]
            row.event_type = observation["event_type"][:30]
            row.last_seen_at = seen_at
            row.last_score = observation["score"]
            row.last_grade = observation["grade"][:30]
            row.reject_reasons_json = json.dumps(
                list(dict.fromkeys(blockers)),
                ensure_ascii=False,
            )
            row.snapshot_json = json.dumps(
                observation["snapshot"],
                ensure_ascii=False,
                default=str,
            )
            if bool(row.pushed) or pushed_now:
                row.pushed = True
                row.pushed_at = row.pushed_at or pushed_at or seen_at
                row.status = "pushed"
            else:
                row.pushed = False
                row.status = status
            row.updated_at = seen_at
            attempts = attempts_by_identity.get(observation["identity"], [])
            if any(item["sent"] is True for item in attempts):
                delivery_status = "reported_sent"
            elif any(not item["result_available"] or item["sent"] is None for item in attempts):
                delivery_status = "unknown"
            elif attempts:
                delivery_status = "throttled" if any(item["throttled"] is True for item in attempts) else "failed"
            else:
                delivery_status = "not_dispatched" if evaluated else "not_evaluated"
            evidence_rows.append({
                "record_id": record_id, "code": observation["code"],
                "identity": observation["identity"], "evaluated": bool(evaluated),
                "snapshot": (frozen_snapshots or {}).get(record_id),
                "candidate_blockers": observation["blockers"],
                "candidate_pushable": observation["pushable"],
                "gate_trace": frozen_gates.get(observation["identity"], {}),
                "gate_reason_basis": "recorded_stage_membership" if gate_trace is not None else "legacy_unspecified",
                "delivery": {"status": delivery_status, "attempts": attempts,
                             "unmatched_result_count": max(0, len(results or []) - len(messages or []))},
                "pushed_before_capture": pushed_before,
                "pushed_before_basis": "prior_compatibility_projection_only",
                "signal_ledger_match_at_capture": ({
                    "signal_id": historical_sent.signal_id,
                    "signal_time": historical_sent.signal_time.isoformat() if historical_sent.signal_time else None,
                    "basis": "may_include_current_delivery_not_a_new_send_receipt",
                } if historical_sent is not None else None),
                "compatibility_status_after_capture": row.status,
                "snapshot_version": ANOMALY_SNAPSHOT_VERSION,
            })
        if frozen_snapshots is not None:
            try:
                # Optional audit failure must neither erase existing projection
                # writes nor imply successful immutable capture.
                async with db.begin_nested():
                    capture = await append_evaluations(db, capture_id=capture_id,
                        trade_date=trade_date, captured_at=seen_at, observations=evidence_rows)
            except (SQLAlchemyError, TypeError, ValueError, OverflowError) as exc:
                capture.update(status="unavailable", reason=type(exc).__name__)
                logger.warning(f"异动不可变候选证据未写入: {type(exc).__name__}")
        await db.commit()
        return capture
    except OperationalError as exc:
        await db.rollback()
        logger.warning(f"异动候选生命周期持久化跳过（数据库待迁移）: {exc}")
        return {"status": "unavailable", "capture_id": capture_id, "reason": "database_error"}


async def _select_unsent_push_recovery_candidates(
    db: AsyncSession | None,
    trade_date: date,
    current_anomalies: list[dict],
    selected_candidates: list[dict],
) -> list[dict]:
    """补发当前仍有效但当天从未成功发送的信号，成功后由绩效记录永久去重。"""
    if db is None:
        return []

    eligible = _select_pushworthy_anomalies(
        current_anomalies,
        [],
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=bool(settings.ANOMALY_PUSH_WATCHLIST_A2_ONLY),
    )
    peer_by_code: dict[str, list[dict]] = {}
    for item in current_anomalies:
        code = str(item.get("code") or "")
        if code:
            peer_by_code.setdefault(code, []).append(item)
    selected_ids = {_anomaly_signal_identity(item) for item in selected_candidates}
    pending: list[tuple[dict, str]] = []
    for item in eligible:
        identity = _anomaly_signal_identity(item)
        code = str(item.get("code") or "")
        if not code or identity in selected_ids:
            continue
        entry = _build_anomaly_push_message_data(item, peer_by_code.get(code, []))
        if not entry or not entry.get("alert_tier") or not entry.get("buypoint_pushable"):
            continue
        if (
            str(item.get("event_type") or "") == "limit_up"
            and not settings.ANOMALY_PUSH_ALLOW_LIMIT_UP
        ):
            continue
        pending.append((item, _anomaly_signal_record_id(trade_date, code, identity)))
    if not pending:
        return []

    result = await db.execute(
        select(SignalPerformance.signal_id).where(
            SignalPerformance.signal_id.in_([signal_id for _, signal_id in pending])
        )
    )
    sent_ids = set(result.scalars().all())
    return [item for item, signal_id in pending if signal_id not in sent_ids]


async def _select_unsent_observation_recovery_messages(
    db: AsyncSession | None,
    trade_date: date,
    current_anomalies: list[dict],
    selected_messages: list[PushMessage],
) -> list[PushMessage]:
    """补发仍有效但因预算、重启或渠道失败未送达的B类强异动。"""
    if db is None:
        return []

    peer_by_code: dict[str, list[dict]] = {}
    for item in current_anomalies:
        code = str(item.get("code") or "")
        if code:
            peer_by_code.setdefault(code, []).append(item)
    selected_identities = {
        str((message.extra or {}).get("signal_identity") or "")
        for message in selected_messages
    }
    pending: list[tuple[PushMessage, str]] = []
    for item in current_anomalies:
        code = str(item.get("code") or "")
        if not code:
            continue
        message = _build_early_observation_push_message(
            item,
            peer_by_code.get(code, []),
        )
        if message is None:
            continue
        identity = str((message.extra or {}).get("signal_identity") or "")
        if not identity or identity in selected_identities:
            continue
        pending.append(
            (message, _anomaly_signal_record_id(trade_date, code, identity))
        )
    if not pending:
        return []

    result = await db.execute(
        select(SignalPerformance.signal_id).where(
            SignalPerformance.signal_id.in_([signal_id for _, signal_id in pending])
        )
    )
    sent_ids = set(result.scalars().all())
    return [
        message
        for message, signal_id in pending
        if signal_id not in sent_ids
    ]


async def _record_sent_anomaly_signals(
    db: AsyncSession | None,
    trade_date: date,
    messages: list[PushMessage],
    results: list[dict],
) -> None:
    """持久化真正发出的异动信号，供后续收益结算和阈值校准。"""
    if db is None:
        return

    now = datetime.combine(trade_date, datetime.now().time())
    changed = False
    for message, result in zip(messages, results):
        if not result.get("sent") or not message.stock_code:
            continue
        extra = dict(message.extra or {})
        observation_only = bool(extra.get("observation_only"))
        identity = str(extra.get("signal_identity") or f"{message.stock_code}|{message.title}")
        signal_id = _anomaly_signal_record_id(trade_date, message.stock_code, identity)
        existing = await db.get(SignalPerformance, signal_id)
        if existing is not None:
            continue
        event_type = str(extra.get("event_type") or "signal")
        is_watchlist_member = bool(extra.get("watchlist_member"))
        signal_type = "observation" if observation_only else (
            f"watch_{event_type}" if is_watchlist_member else f"anomaly_{event_type}"
        )[:20]
        db.add(SignalPerformance(
            signal_id=signal_id,
            stock_code=message.stock_code,
            signal_time=now,
            signal_price=_safe_float(extra.get("signal_price")),
            signal_score=int(round(_safe_float(extra.get("signal_score")))),
            signal_type=signal_type,
            signal_variant=str(extra.get("signal_variant") or event_type)[:40],
            setup_grade=str(extra.get("setup_grade") or "")[:30],
            top_factor=str(extra.get("signal_label") or event_type)[:30],
            board_tag="watchlist" if is_watchlist_member else "market",
            # B类观察仅用于恢复小时预算与同身份去重，不进入下方买点收益结算。
            evaluation_version=(
                "anomaly_observation_v1" if observation_only else "anomaly_v2"
            ),
        ))
        changed = True
    if changed:
        await db.commit()


async def _settle_anomaly_signal_performance(
    db: AsyncSession | None,
    trade_date: date,
) -> None:
    """回填原始、扣费、全市场基准和净超额收益。"""
    if db is None:
        return

    result = await db.execute(
        select(SignalPerformance).where(
            SignalPerformance.signal_type.like("anomaly_%")
            | SignalPerformance.signal_type.like("watch_%"),
            SignalPerformance.signal_time < datetime.combine(trade_date, datetime.min.time()),
            SignalPerformance.return_10d.is_(None),
        ).limit(200)
    )
    records = result.scalars().all()
    if not records:
        return

    market_return_cache: dict[date, float] = {}

    async def market_daily_return(trade_day: date) -> float:
        if trade_day not in market_return_cache:
            value = await db.scalar(
                select(func.avg(StockKline.change_pct)).where(
                    StockKline.trade_date == trade_day,
                    StockKline.change_pct.is_not(None),
                    StockKline.change_pct >= -21.0,
                    StockKline.change_pct <= 21.0,
                )
            )
            market_return_cache[trade_day] = _safe_float(value)
        return market_return_cache[trade_day]

    round_trip_cost_pct = max(_safe_float(settings.ANOMALY_EVAL_ROUND_TRIP_COST_PCT), 0.0)
    changed = False
    for record in records:
        signal_day = record.signal_time.date()
        bars_result = await db.execute(
            select(StockKline)
            .where(
                StockKline.code == record.stock_code,
                StockKline.trade_date > signal_day,
                StockKline.trade_date <= trade_date,
            )
            .order_by(StockKline.trade_date)
            .limit(10)
        )
        bars = bars_result.scalars().all()
        signal_price = _safe_float(record.signal_price)
        if signal_price <= 0 or not bars:
            continue

        for days in (1, 3, 5, 10):
            return_field = f"return_{days}d"
            net_field = f"net_return_{days}d"
            benchmark_field = f"benchmark_return_{days}d"
            excess_field = f"excess_return_{days}d"
            if len(bars) >= days:
                close = _safe_float(bars[days - 1].close)
                if close > 0:
                    gross_return = round((close / signal_price - 1.0) * 100.0, 4)
                    net_return = round(gross_return - round_trip_cost_pct, 4)
                    benchmark_nav = 1.0
                    for bar in bars[:days]:
                        benchmark_nav *= 1.0 + await market_daily_return(bar.trade_date) / 100.0
                    benchmark_return = round((benchmark_nav - 1.0) * 100.0, 4)
                    setattr(record, return_field, gross_return)
                    setattr(record, net_field, net_return)
                    setattr(record, benchmark_field, benchmark_return)
                    setattr(record, excess_field, round(net_return - benchmark_return, 4))
                    changed = True

        observed = bars[:10]
        highs = [_safe_float(item.high) for item in observed if _safe_float(item.high) > 0]
        lows = [_safe_float(item.low) for item in observed if _safe_float(item.low) > 0]
        if highs:
            record.max_return = round((max(highs) / signal_price - 1.0) * 100.0, 4)
            changed = True
        if lows:
            record.max_drawdown = round((min(lows) / signal_price - 1.0) * 100.0, 4)
            changed = True
        if record.net_return_3d is not None and record.excess_return_3d is not None:
            first_three_lows = [
                _safe_float(item.low)
                for item in bars[:3]
                if _safe_float(item.low) > 0
            ]
            mae_3d = (
                (min(first_three_lows) / signal_price - 1.0) * 100.0
                if first_three_lows
                else 0.0
            )
            reasons = []
            if record.net_return_3d <= 0:
                reasons.append("3日扣费收益未转正")
            if record.excess_return_3d <= 0:
                reasons.append("3日未跑赢全市场等权基准")
            if mae_3d < _safe_float(settings.ANOMALY_EVAL_MAX_ADVERSE_PCT, -5.0):
                reasons.append(f"3日最大不利波动{mae_3d:.1f}%超限")
            record.is_correct = not reasons
            record.failure_reason = "；".join(reasons)
            record.evaluation_version = "anomaly_v2"
            changed = True

    if changed:
        await db.commit()


async def _filter_push_messages_by_performance(
    db: AsyncSession | None,
    trade_date: date,
    messages: list[PushMessage],
) -> list[PushMessage]:
    """样本不足时影子放行；样本成熟后自动收紧低质量信号。"""
    if not messages:
        return []
    if db is None:
        return messages

    result = await db.execute(
        select(SignalPerformance).where(
            SignalPerformance.signal_time < datetime.combine(trade_date, datetime.min.time()),
            SignalPerformance.evaluation_version == "anomaly_v2",
            SignalPerformance.is_correct.is_not(None),
            SignalPerformance.net_return_3d.is_not(None),
            SignalPerformance.excess_return_3d.is_not(None),
        )
    )
    grouped: dict[tuple[str, str, str], list[SignalPerformance]] = {}
    for record in result.scalars().all():
        key = (
            str(record.signal_type or ""),
            str(record.signal_variant or ""),
            str(record.setup_grade or ""),
        )
        grouped.setdefault(key, []).append(record)

    min_samples = max(int(settings.ANOMALY_EVAL_MIN_SAMPLES), 1)
    min_win_rate = min(max(_safe_float(settings.ANOMALY_EVAL_MIN_WIN_RATE), 0.0), 1.0)
    early_stop_samples = min(
        max(int(settings.ANOMALY_EVAL_EARLY_STOP_MIN_SAMPLES), 1),
        min_samples,
    )
    early_stop_win_rate = min(
        max(_safe_float(settings.ANOMALY_EVAL_EARLY_STOP_MAX_WIN_RATE), 0.0),
        1.0,
    )
    accepted: list[PushMessage] = []
    for message in messages:
        extra = dict(message.extra or {})
        event_type = str(extra.get("event_type") or "signal")
        scope = "watch" if extra.get("watchlist_member") else "anomaly"
        signal_type = f"{scope}_{event_type}"[:20]
        signal_variant = str(extra.get("signal_variant") or event_type)[:40]
        setup_grade = str(extra.get("setup_grade") or "")[:30]
        records = grouped.get((signal_type, signal_variant, setup_grade), [])
        sample_count = len(records)
        evidence = {
            "sample_count": sample_count,
            "shadow_mode": sample_count < min_samples,
        }
        blocked = False
        if sample_count >= early_stop_samples:
            win_rate = sum(1 for item in records if item.is_correct) / sample_count
            avg_net_return_3d = sum(_safe_float(item.net_return_3d) for item in records) / sample_count
            avg_excess_return_3d = sum(_safe_float(item.excess_return_3d) for item in records) / sample_count
            evidence.update({
                "win_rate_3d": round(win_rate, 4),
                "avg_net_return_3d": round(avg_net_return_3d, 4),
                "avg_excess_return_3d": round(avg_excess_return_3d, 4),
            })
            early_stop = (
                win_rate < early_stop_win_rate
                and avg_net_return_3d <= 0
                and avg_excess_return_3d <= 0
            )
            if early_stop:
                blocked = True
                evidence["early_stop"] = True
            elif setup_grade.startswith("A2") and sample_count >= min_samples:
                blocked = (
                    win_rate < min_win_rate
                    or avg_net_return_3d <= 0
                    or avg_excess_return_3d <= 0
                )
            elif setup_grade.startswith("A1") and sample_count >= min_samples:
                blocked = (
                    win_rate < min_win_rate
                    or avg_net_return_3d <= 0
                    or avg_excess_return_3d <= 0
                )

        evidence["blocked"] = blocked
        extra["performance_evidence"] = evidence
        message.extra = extra
        if blocked:
            logger.info(
                "异动推送被历史绩效门控: "
                f"code={message.stock_code} type={signal_type}/{signal_variant} "
                f"grade={setup_grade} samples={sample_count}"
            )
            continue
        accepted.append(message)
    return accepted


def _cap_automatic_push_messages(
    anomaly_messages: list[PushMessage],
    b1_messages: list[PushMessage],
) -> list[PushMessage]:
    """主异动与B1共用同股去重和单轮总上限。"""
    ranked = [
        (message, 1)
        for message in anomaly_messages
    ] + [
        (message, 0)
        for message in b1_messages
    ]
    ranked.sort(
        key=lambda item: (
            {"core": 3, "conditional": 2, "observation": 1}.get(
                str((item[0].extra or {}).get("focus_tier") or ""),
                0,
            ),
            _safe_float((item[0].extra or {}).get("focus_score")),
            _grade_rank((item[0].extra or {}).get("setup_grade")),
            1 if (item[0].extra or {}).get("watchlist_member") else 0,
            1 if (item[0].extra or {}).get("low_base_setup_confirmed") else 0,
            int(item[0].priority),
            item[1],
        ),
        reverse=True,
    )

    selected: list[PushMessage] = []
    seen_stocks: set[str] = set()
    seen_titles: set[str] = set()
    for message, _source_rank in ranked:
        code = str(message.stock_code or "")
        if code and code in seen_stocks:
            continue
        if not code and message.title in seen_titles:
            continue
        if code:
            seen_stocks.add(code)
        seen_titles.add(message.title)
        selected.append(message)
        if len(selected) >= max(int(settings.ANOMALY_PUSH_MAX_PER_SCAN), 1):
            break
    return selected


async def _cap_automatic_push_messages_by_persisted_budget(
    db: AsyncSession,
    target_date: date,
    messages: list[PushMessage],
    *,
    now: datetime | None = None,
) -> list[PushMessage]:
    """用绩效表恢复滚动预算，并平滑A2额度以避免整点集中推送。

    滚动60秒>=1%且同窗成交确认的强急拉使用独立小时保留额度，
    避免被支撑触达等普通A2提前耗尽令牌；仍然受全局飞书限频约束。
    """
    if not messages:
        return []
    if db is None:
        return messages
    now = now or datetime.combine(target_date, datetime.now().time())
    window_start = now - timedelta(hours=1)
    session_start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now < session_start:
        session_start = now
    result = await db.execute(
        select(
            SignalPerformance.stock_code,
            SignalPerformance.signal_time,
            SignalPerformance.setup_grade,
            SignalPerformance.signal_variant,
        ).where(
            SignalPerformance.signal_time >= session_start,
            SignalPerformance.signal_time <= now,
            (
                SignalPerformance.signal_type.like("anomaly_%")
                | SignalPerformance.signal_type.like("watch_%")
                | (SignalPerformance.signal_type == "observation")
            ),
        ).order_by(SignalPerformance.signal_time.asc())
    )
    sent_rows = list(result.all())
    recent_rows = [row for row in sent_rows if row.signal_time >= window_start]
    persisted_codes = {str(row.stock_code or "") for row in recent_rows if row.stock_code}
    persisted_count = len(persisted_codes)
    persisted_a1_count = len({
        str(row.stock_code or "")
        for row in recent_rows
        if row.stock_code and str(row.setup_grade or "").startswith("A1")
    })
    # 老版记录的variant只有positive_acceleration，无法确定当时是否
    # strong档；不猜测历史额度，新版从可验证的strong variant开始计数。
    persisted_rapid_strong_count = len({
        str(row.stock_code or "")
        for row in recent_rows
        if row.stock_code
        and str(row.signal_variant or "")
        in {"positive_acceleration_strong", "positive_acceleration_pre_limit"}
    })
    hourly_limit = max(1, int(settings.ANOMALY_PUSH_MAX_PER_HOUR))
    burst_capacity = max(
        1,
        min(int(settings.ANOMALY_PUSH_BURST_CAPACITY), hourly_limit),
    )
    refill_per_second = hourly_limit / 3600.0
    available_tokens = float(burst_capacity)
    token_cursor = session_start
    token_counted_codes: set[str] = set()
    for row in sent_rows:
        signal_time = row.signal_time
        if signal_time < token_cursor or signal_time > now:
            continue
        stock_code = str(row.stock_code or "")
        if stock_code and stock_code in token_counted_codes:
            continue
        available_tokens = min(
            float(burst_capacity),
            available_tokens + (signal_time - token_cursor).total_seconds() * refill_per_second,
        )
        available_tokens = max(0.0, available_tokens - 1.0)
        token_cursor = signal_time
        if stock_code:
            token_counted_codes.add(stock_code)
    available_tokens = min(
        float(burst_capacity),
        available_tokens + (now - token_cursor).total_seconds() * refill_per_second,
    )
    general_remaining = max(0, hourly_limit - persisted_count)
    smoothed_remaining = min(general_remaining, int(available_tokens + 1e-9))
    a1_reserve_remaining = max(
        0,
        int(settings.ANOMALY_PUSH_A1_RESERVE_PER_HOUR) - persisted_a1_count,
    )
    rapid_strong_reserve_remaining = max(
        0,
        int(settings.ANOMALY_PUSH_RAPID_STRONG_RESERVE_PER_HOUR)
        - persisted_rapid_strong_count,
    )
    accepted: list[PushMessage] = []
    evaluated_codes: set[str] = set()
    rejected_labels: list[str] = []
    for message in messages:
        extra = message.extra or {}
        code = str(message.stock_code or "")
        if code and code in evaluated_codes:
            rejected_labels.append(f"{code}:batch_duplicate")
            continue
        if code:
            evaluated_codes.add(code)
        setup_grade = str(extra.get("setup_grade") or "")
        rapid_strong_eligible = bool(
            (
                str(extra.get("signal_variant") or "")
                == "positive_acceleration_strong"
                and extra.get("rapid_rise_strong_confirmed")
            )
            or (
                str(extra.get("signal_variant") or "")
                == "positive_acceleration_pre_limit"
                and extra.get("pre_limit_up_confirmed")
            )
        )
        # 强急拉优先消耗独立保留额度，不抢占普通A2的平滑令牌。
        if rapid_strong_eligible and rapid_strong_reserve_remaining > 0:
            accepted.append(message)
            rapid_strong_reserve_remaining -= 1
            continue
        if general_remaining > 0 and smoothed_remaining > 0:
            accepted.append(message)
            general_remaining -= 1
            smoothed_remaining -= 1
            continue
        reserve_eligible = bool(
            (
                setup_grade.startswith("A1")
                or str(extra.get("focus_tier") or "") == "core"
            )
            and (
                (message.extra or {}).get("watchlist_member")
                or (message.extra or {}).get("detection_pool_member")
                or (message.extra or {}).get("low_base_watchlist")
            )
        )
        if reserve_eligible and a1_reserve_remaining > 0:
            accepted.append(message)
            a1_reserve_remaining -= 1
            continue
        rejected_labels.append(f"{code or message.title}:budget")

    if len(accepted) < len(messages):
        logger.info(
            "异动推送持久化平滑预算生效: "
            f"past_hour={persisted_count} a1={persisted_a1_count} limit={hourly_limit} "
            f"tokens={available_tokens:.2f}/{burst_capacity} "
            f"a1_reserve={int(settings.ANOMALY_PUSH_A1_RESERVE_PER_HOUR)} "
            f"rapid_reserve={int(settings.ANOMALY_PUSH_RAPID_STRONG_RESERVE_PER_HOUR)} "
            f"pending={len(messages)} accepted={len(accepted)} "
            f"rejected={','.join(rejected_labels[:20])}"
        )
    return accepted


async def _ensure_dynamic_trend_pool(db: AsyncSession, trade_date: date) -> bool:
    """保证后台异动扫描不依赖用户先打开“明日预案”页面。"""
    if db is not None:
        await _refresh_intraday_sector_core_pool(db, trade_date)
    if anomaly_scanner.dynamic_trend_pool:
        return True
    try:
        await prewarm_next_day_plan_snapshot(
            db,
            trade_date,
            force_refresh=False,
            limit=20,
        )
    except Exception as exc:
        logger.warning(f"动态趋势驱动池自动恢复失败: {exc}")
        return False
    ready = bool(anomaly_scanner.dynamic_trend_pool)
    if not ready:
        logger.warning("动态趋势驱动池自动恢复后仍为空，本轮仅运行通用异动扫描")
    return ready


async def refresh_and_push_anomaly_snapshot(
    db: AsyncSession,
    trade_date: date | None = None,
) -> dict:
    target_date = await _resolve_anomaly_trade_date(db, trade_date)
    await _ensure_dynamic_trend_pool(db, target_date)
    await _settle_anomaly_signal_performance(db, target_date)
    previous = _get_cached_anomaly_snapshot(target_date)
    if previous is None:
        previous = await _get_latest_persisted_anomaly_snapshot(db, target_date)

    payload = await prewarm_anomaly_snapshot(db, target_date, force_refresh=True)

    previous_anomalies = _filter_main_board_items((previous or {}).get("anomalies", []))
    prev_ids = {_anomaly_signal_identity(item) for item in previous_anomalies}
    current_anomalies = _filter_main_board_items(payload.get("anomalies") or [])
    current_b1_states = _filter_main_board_items(payload.get("b1_states") or [])
    current_main_summary = _summarize_anomalies(current_anomalies)
    new_anomalies = [
        item for item in current_anomalies
        if _anomaly_signal_identity(item) not in prev_ids and (item.get("score") or 0) >= 50
    ]
    summary_changed = _summarize_anomalies(previous_anomalies) != current_main_summary

    if new_anomalies or summary_changed:
        from app.api.v1.ws import ws_manager

        await ws_manager.push_anomaly({
            "summary": current_main_summary,
            "snapshot_time": payload.get("snapshot_time"),
            "new_anomalies": new_anomalies[:20],
        })
    if not await trade_calendar.is_trading_hours():
        capture = await _persist_anomaly_candidate_records(
            db,
            target_date,
            current_anomalies,
            evaluated=False,
        )
        logger.debug("异动飞书推送跳过: 当前非交易时段，仅刷新快照")
        return {**payload, "candidate_evidence_capture": capture}

    push_candidates = _select_pushworthy_anomalies(
        current_anomalies,
        previous_anomalies,
        min_grade="A2 盘口确认后执行",
        watchlist_a2_only=bool(settings.ANOMALY_PUSH_WATCHLIST_A2_ONLY),
    )
    recovered_candidates = await _select_unsent_push_recovery_candidates(
        db,
        target_date,
        current_anomalies,
        push_candidates,
    )
    if recovered_candidates:
        push_candidates.extend(recovered_candidates)
    anomaly_messages: list[PushMessage] = []
    if previous is not None and push_candidates:
        anomaly_messages = _build_push_messages_for_anomalies(
            push_candidates,
            min_grade="A2 盘口确认后执行",
            allow_limit_up=bool(settings.ANOMALY_PUSH_ALLOW_LIMIT_UP),
            peer_anomalies=current_anomalies,
        )

    # 盘前池之外的强急拉仍要及时看见，但单独标成B类异动观察，不能写入
    # A2买点绩效，也不能让页面显示“飞书可执行”。同身份只在首次出现时提醒。
    observation_messages: list[PushMessage] = []
    if previous is not None:
        current_by_code: dict[str, list[dict]] = {}
        for item in current_anomalies:
            current_by_code.setdefault(str(item.get("code") or ""), []).append(item)
        for item in new_anomalies:
            message = _build_early_observation_push_message(
                item,
                current_by_code.get(str(item.get("code") or ""), []),
            )
            if message is not None:
                observation_messages.append(message)
    recovered_observation_messages = await _select_unsent_observation_recovery_messages(
        db,
        target_date,
        current_anomalies,
        observation_messages,
    )
    if recovered_observation_messages:
        observation_messages.extend(recovered_observation_messages)

    previous_b1_states = _filter_main_board_items((previous or {}).get("b1_states") or [])
    b1_state_changes = _select_b1_state_changes(current_b1_states, previous_b1_states)
    b1_messages: list[PushMessage] = []
    if previous is not None and b1_state_changes:
        b1_messages = [
            message
            for message in (_build_b1_state_push_message(change) for change in b1_state_changes[:20])
            if message is not None
        ]

    candidate_messages = await _filter_push_messages_by_performance(
        db,
        target_date,
        anomaly_messages + b1_messages,
    )
    def message_identities(items):
        return {_base_anomaly_message_identity(str((message.extra or {}).get("signal_identity") or ""))
                for message in items}
    performance_passed_ids = message_identities(candidate_messages)
    # B类观察没有已结算的“买点绩效”口径，不经过A1/A2历史门控；它仍受
    # 单轮上限、小时令牌和同股冷却约束。
    candidate_messages.extend(observation_messages)
    candidate_messages = _annotate_automatic_push_focus(candidate_messages)
    anomaly_message_ids = {id(message) for message in anomaly_messages + observation_messages}
    eligible_anomaly_messages = [
        message for message in candidate_messages if id(message) in anomaly_message_ids
    ]
    eligible_b1_messages = [
        message for message in candidate_messages if id(message) not in anomaly_message_ids
    ]
    push_messages = _cap_automatic_push_messages(
        eligible_anomaly_messages,
        eligible_b1_messages,
    )
    round_budget_passed_ids = message_identities(push_messages)
    push_messages = await _cap_automatic_push_messages_by_persisted_budget(
        db,
        target_date,
        push_messages,
    )
    selected_ids = {_anomaly_signal_identity(item) for item in push_candidates}
    built_ids = message_identities(anomaly_messages)
    observation_ids = message_identities(observation_messages)
    final_ids = message_identities(push_messages)
    gate_trace = {
        _anomaly_signal_identity(item): {
            "baseline_available": previous is not None,
            "strategy_selected": _anomaly_signal_identity(item) in selected_ids,
            "technical_message_built": _anomaly_signal_identity(item) in built_ids,
            "observation_message_built": _anomaly_signal_identity(item) in observation_ids,
            "performance_filter_passed": _anomaly_signal_identity(item) in performance_passed_ids,
            "performance_filter_applies_to_observation": False,
            "round_budget_passed": _anomaly_signal_identity(item) in round_budget_passed_ids,
            "persisted_budget_passed": _anomaly_signal_identity(item) in final_ids,
            "allow_limit_up": bool(settings.ANOMALY_PUSH_ALLOW_LIMIT_UP),
            "watchlist_a2_only": bool(settings.ANOMALY_PUSH_WATCHLIST_A2_ONLY),
        } for item in current_anomalies
    }
    logger.debug(
        "异动自动推送评估: "
        f"current={len(current_anomalies)} baseline={previous is not None} "
        f"candidates={len(push_candidates)} recovered={len(recovered_candidates)} "
        f"built={len(anomaly_messages)} "
        f"observation={len(observation_messages)} "
        f"observation_recovered={len(recovered_observation_messages)} "
        f"b1_changes={len(b1_state_changes)} b1_built={len(b1_messages)} "
        f"performance_passed={len(candidate_messages)} final={len(push_messages)} "
        f"allow_limit_up={bool(settings.ANOMALY_PUSH_ALLOW_LIMIT_UP)} "
        f"watchlist_a2_only={bool(settings.ANOMALY_PUSH_WATCHLIST_A2_ONLY)}"
    )
    if previous is not None and push_candidates and not anomaly_messages:
        logger.info(
            "异动自动推送候选未生成消息: "
            f"candidates={len(push_candidates)} "
            f"allow_limit_up={bool(settings.ANOMALY_PUSH_ALLOW_LIMIT_UP)}"
        )
    results: list[dict] = []
    if push_messages:
        results = await push_scheduler.push_batch(push_messages)
        await _record_sent_anomaly_signals(db, target_date, push_messages, results)
        sent_count = sum(1 for item in results if item.get("sent"))
        throttled_count = sum(1 for item in results if item.get("throttled"))
        logger.info(
            "异动自动推送完成: "
            f"anomaly={len(anomaly_messages)} b1={len(b1_messages)} "
            f"capped={len(push_messages)} sent={sent_count} throttled={throttled_count}"
        )
    capture = await _persist_anomaly_candidate_records(
        db,
        target_date,
        current_anomalies,
        evaluated=previous is not None,
        messages=push_messages,
        results=results,
        gate_trace=gate_trace,
    )
    return {**payload, "candidate_evidence_capture": capture}


# =========================================================================
# Tab 1: 异动监控
# =========================================================================

def _monitor_observation_only(item: dict) -> dict:
    """Strip current execution labels from an owned historical monitor projection.

    Do not mutate cached events/funding frames or alter live scanner/push policy.
    Numeric grades remain available as reference, not current trading authority.
    """
    reason = "非连续交易时段或过期快照仅供观察，不代表当前可执行买点"
    projected = {
        **item,
        "reference_setup_grade": item.get("setup_grade"),
        "reference_setup_track": item.get("setup_track"),
        "setup_grade": "快照观察", "setup_grade_display": "快照观察",
        "setup_grade_rank": 0, "setup_track": "",
        "buy_point_reached": False, "buy_point_pushable": False,
        "feishu_pushable": False, "b1_pushable": False,
        "buy_point_grade": "", "buy_point_grade_display": "",
        "buy_point_type": "", "buy_point_sources": [], "buy_point_reasons": [], "buy_point_label": reason,
        "buy_point_blockers": _unique_strings([reason, *(item.get("buy_point_blockers") or [])]),
        "b1_push_blockers": _unique_strings([reason, *(item.get("b1_push_blockers") or [])]),
        "monitor_observation_only": True,
    }
    if isinstance(item.get("events"), list):
        projected["events"] = [_monitor_observation_only(event) for event in item["events"]]
    return projected


async def _load_recorded_capital_activity(
    db: AsyncSession, *, trade_date: date | None, as_of_at: datetime,
) -> dict:
    """One indexed, grouped read of recorded identities; never replay or trade.

    Last-seen candidate metadata is not an immutable minute snapshot. Count only
    already-visible same-day records, independent of current scan/filter/grade.
    Do not load snapshot_json, quote data, prices, funding or execution booleans.
    """
    result = {
        "status": "unavailable", "trade_date": str(trade_date) if trade_date else "",
        "as_of_at": as_of_at.isoformat(), "basis": "recorded_candidates_distinct_stock",
        "stock_count": None, "record_count": None,
        "first_seen_at": None, "last_seen_at": None, "stocks": [],
        "note": "候选台账按股票去重，按当前主板及屏蔽规则展示；不代表买点、成交或全天无漏记",
    }
    if trade_date is None or trade_date > as_of_at.date():
        return result
    record = AnomalyCandidateRecord
    day_start = datetime.combine(trade_date, datetime.min.time())
    try:
        rows = (await db.execute(
            select(
                record.code,
                func.max(func.coalesce(StockTag.name, record.name)).label("name"),
                func.min(record.first_seen_at).label("first_seen_at"),
                func.max(record.last_seen_at).label("last_seen_at"),
                func.count(record.record_id).label("record_count"),
            )
            .outerjoin(StockTag, StockTag.code == record.code)
            .where(
                record.trade_date == trade_date, record.event_type == "capital",
                record.first_seen_at >= day_start,
                record.first_seen_at <= record.last_seen_at,
                record.last_seen_at < day_start + timedelta(days=1),
                record.last_seen_at <= as_of_at,
                func.coalesce(StockTag.is_st, False).is_(False),
                func.coalesce(StockTag.is_suspended, False).is_(False),
                func.coalesce(StockTag.is_delisting, False).is_(False),
                or_(StockTag.board_tag.is_(None), StockTag.board_tag == "tradeable"),
            )
            .group_by(record.code)
            .order_by(desc(func.max(record.last_seen_at)), record.code)
        )).mappings().all()
        stocks = _filter_main_board_items([
            {"code": str(row["code"]), "name": row["name"] or row["code"],
             "first_seen_at": row["first_seen_at"].isoformat(),
             "last_seen_at": row["last_seen_at"].isoformat(),
             "record_count": int(row["record_count"])}
            for row in rows
        ])
    except (SQLAlchemyError, ValueError, TypeError, OverflowError) as exc:
        await db.rollback()
        logger.warning(f"资金候选历史只读统计不可用: {type(exc).__name__}")
        result["note"] = "资金候选台账暂不可用；不能把读取失败当作本日零记录"
        return result
    return {
        **result, "status": "ok" if stocks else "empty",
        "stock_count": len(stocks), "record_count": sum(row["record_count"] for row in stocks),
        "first_seen_at": min((row["first_seen_at"] for row in stocks), default=None),
        "last_seen_at": max((row["last_seen_at"] for row in stocks), default=None),
        "stocks": stocks,
    }


@router.get("/anomalies")
async def anomaly_monitor(
    min_score: float = Query(50, description="最低评分"),
    event_type: str = Query("", description="异动类型: limit_up/limit_down/capital/low_absorb/breakthrough/rapid_rise"),
    setup_track: str = Query("", description="执行风格: 趋势/资金型 / 打板型"),
    sort_by: str = Query("priority", description="排序方式: priority/buy_point/grade/change_pct/net_inflow/latest"),
    buy_point_only: bool = Query(False, description="仅返回到达买点的股票/事件"),
    view: str = Query("event", description="返回视图: event / stock"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=10, le=200, description="每页数量"),
    db: AsyncSession = Depends(get_db),
):
    """异动监控 — 实时异动流+分类扫描"""
    try:
        snapshot = await prewarm_anomaly_snapshot(db, None, monitor_read=True)
    except Exception as exc:
        await db.rollback()
        logger.warning(f"异动监控快照生成失败，降级返回缓存: {exc}")
        snapshot = _get_any_cached_anomaly_snapshot()
        if snapshot is None:
            snapshot = {
                "trade_date": "",
                "snapshot_time": None,
                "anomalies": [],
                "summary": {},
                "b1_states": [],
                "b1_summary": {},
                "degraded": True,
                "degrade_reason": "异动扫描正忙且暂无可用快照",
            }
    snapshot_trade_date = None
    try:
        snapshot_trade_date = date.fromisoformat(str(snapshot.get("trade_date") or ""))
    except ValueError:
        snapshot_trade_date = None
    response_at = datetime.now()
    market_session = _monitor_market_session(response_at)
    detection_paused = market_session not in {"morning", "afternoon"} or snapshot_trade_date != response_at.date()
    snapshot_age = _snapshot_age_seconds(snapshot)
    closed_policy = snapshot_trade_date is not None and _monitor_snapshot_ttl(snapshot_trade_date, response_at) > ANOMALY_CACHE_TTL_SECONDS
    snapshot_stale = snapshot_age is None or snapshot_age < 0 or snapshot_age > ANOMALY_CACHE_TTL_SECONDS
    snapshot_at = evidence_clock(snapshot.get("snapshot_time"))
    snapshot_outside_session = snapshot_at is None or _monitor_market_session(snapshot_at) not in {"morning", "afternoon"}
    observation_only = detection_paused or closed_policy or snapshot_stale or snapshot_outside_session or bool(snapshot.get("degraded"))
    capital_activity = await _load_recorded_capital_activity(
        db, trade_date=snapshot_trade_date, as_of_at=response_at,
    )
    snapshot_meta = {
        "market_session": market_session,
        "detection_paused": detection_paused,
        "capital_activity": capital_activity,
        "snapshot_trade_date": str(snapshot_trade_date) if snapshot_trade_date else "",
        "snapshot_age_seconds": round(snapshot_age, 1) if snapshot_age is not None else None,
        "snapshot_stale": snapshot_stale,
        "snapshot_cache_policy": "lunch_read_only" if market_session == "lunch_break" else "post_close_read_only" if closed_policy else "live",
        "monitor_observation_only": observation_only,
    }
    anomalies_all = _filter_main_board_items(snapshot.get("anomalies") or [])
    if observation_only:
        anomalies_all = [_monitor_observation_only(item) for item in anomalies_all]
    anomalies_scored = [
        item for item in anomalies_all
        if (item.get("score") or 0) >= min_score
    ]
    summary = _summarize_anomalies(anomalies_scored)
    stock_rows_unfiltered = _build_stock_rows(
        anomalies_scored,
        event_type="",
        setup_track="",
        sort_by="priority",
    )
    # event视图不返回B1逐股字段，避免每次翻页都执行批量K线/B1增强；
    # stock视图的B1汇总与当次完整候选行一致，不沿用旧快照可推数。
    if view == "stock":
        try:
            stock_rows_unfiltered = await _enrich_stock_rows_with_b1(
                stock_rows_unfiltered,
                db,
                target_date=snapshot_trade_date,
            )
        except Exception as exc:
            await db.rollback()
            logger.warning(f"异动监控B1增强失败，降级返回基础行: {exc}")
    stock_rows_unfiltered = [
        _apply_stock_row_buy_point_status(row)
        for row in stock_rows_unfiltered
    ]
    if observation_only:
        stock_rows_unfiltered = [_monitor_observation_only(row) for row in stock_rows_unfiltered]
    stock_summary = _summarize_stock_rows(stock_rows_unfiltered)
    if view == "stock":
        b1_states = [state for row in stock_rows_unfiltered if (state := _serialize_b1_state_row(row)) is not None]
    else:
        b1_states = _filter_main_board_items(snapshot.get("b1_states") or [])
    if observation_only:
        b1_states = [{**state, "pushable": False} for state in b1_states]
    b1_summary = _summarize_b1_states(b1_states)

    # V2.2融合: 市场情绪量化上下文
    sentiment_context = {}
    try:
        from app.dashboard2.service import dashboard2_service
        a_share_ctx = await dashboard2_service._load_a_share_context(db)
        a_share_core = dashboard2_service._build_a_share_core(a_share_ctx)
        sentiment_context = {
            "sentiment_cycle": a_share_core.sentiment_cycle,
            "market_environment": a_share_core.market_environment,
            "buy_threshold": a_share_core.buy_threshold,
            "limit_up_count": a_share_core.limit_up_count,
            "limit_down_count": a_share_core.limit_down_count,
            "seal_rate": a_share_core.seal_rate,
            "board_height": a_share_core.board_height,
            "main_net_inflow": a_share_core.main_net_inflow,
        }
    except Exception as e:
        logger.debug(f"[anomaly_monitor] 市场情绪量化获取失败: {e}")

    offset = (page - 1) * page_size

    if view == "stock":
        rows_all = list(stock_rows_unfiltered)
        if event_type:
            rows_all = [row for row in rows_all if event_type in (row.get("event_types") or [])]
        if setup_track:
            rows_all = [row for row in rows_all if str(row.get("setup_track") or "") == setup_track]
        if buy_point_only:
            rows_all = [row for row in rows_all if row.get("buy_point_reached") or row.get("buy_point_pushable")]
        if sort_by == "net_inflow":
            # Sort the displayed amount (including known negative/zero), not the
            # execution-only nullable amount. This cannot alter buy-point filters.
            rows_all = await _attach_anomaly_display_context(
                rows_all, db, trade_date=snapshot_trade_date, as_of_at=datetime.now(),
            )
            rows_all = sorted(rows_all, key=lambda row: (
                _safe_float((row.get("fund_display") or {}).get("main_net_inflow"))
                if (row.get("fund_display") or {}).get("available") else float("-inf"),
                _compare_aggregated_rows(row, "priority"),
            ), reverse=True)
        else:
            rows_all = sorted(rows_all, key=lambda row: _compare_aggregated_rows(row, sort_by), reverse=True)
        rows = rows_all[offset: offset + page_size]
        if sort_by != "net_inflow":
            rows = await _attach_anomaly_display_context(
                rows, db, trade_date=snapshot_trade_date, as_of_at=datetime.now(),
            )
        return {
            "view": "stock",
            **snapshot_meta,
            "rows": rows,
            "total": len(rows_all),
            "page": page,
            "page_size": page_size,
            "summary": summary,
            "b1_summary": b1_summary,
            "stock_summary": stock_summary,
            "snapshot_time": snapshot.get("snapshot_time"),
            "sentiment": sentiment_context,
            "degraded": bool(snapshot.get("degraded")),
            "degrade_reason": snapshot.get("degrade_reason"),
        }

    anomalies_filtered = []
    for item in anomalies_scored:
        if event_type and _event_filter_key(item) != event_type:
            continue
        if setup_track and str(item.get("setup_track") or "") != setup_track:
            continue
        if buy_point_only and not (item.get("buy_point_reached") or item.get("buy_point_pushable")):
            continue
        anomalies_filtered.append(item)
    anomalies = anomalies_filtered[offset: offset + page_size]

    return {
        "view": "event",
        **snapshot_meta,
        "anomalies": anomalies,
        "total": len(anomalies_filtered),
        "page": page,
        "page_size": page_size,
        "summary": summary,
        "b1_summary": b1_summary,
        "stock_summary": stock_summary,
        "snapshot_time": snapshot.get("snapshot_time"),
        "sentiment": sentiment_context,
        "degraded": bool(snapshot.get("degraded")),
        "degrade_reason": snapshot.get("degrade_reason"),
    }


@router.post("/anomalies/replay")
async def replay_anomaly_push(
    limit: int = Query(5, ge=1, le=20, description="重推数量"),
    min_score: float = Query(70, description="最低评分"),
    event_type: str = Query("", description="异动类型筛选"),
    channel: str = Query("feishu", description="推送渠道: feishu/ws/all"),
    allow_limit_up: bool = Query(False, description="是否允许推送已涨停信号"),
    force_refresh: bool = Query(False, description="是否强制刷新最新快照"),
    bypass_throttle: bool = Query(True, description="是否绕过限频"),
    dry_run: bool = Query(False, description="仅预览，不实际推送"),
    db: AsyncSession = Depends(get_db),
):
    """手动重推异动卡片，便于验证飞书/WS链路。"""
    if not isinstance(allow_limit_up, bool):
        allow_limit_up = bool(getattr(allow_limit_up, "default", False))
    snapshot = await prewarm_anomaly_snapshot(db, None, force_refresh=force_refresh)
    anomalies_all = _filter_main_board_items(snapshot.get("anomalies") or [])
    anomalies_filtered = [
        item for item in anomalies_all
        if (item.get("score") or 0) >= min_score
        and (not event_type or _event_filter_key(item) == event_type)
    ]
    push_candidates = _build_push_message_candidates(
        anomalies_filtered,
        min_grade="A2 盘口确认后执行",
        allow_limit_up=allow_limit_up,
        peer_anomalies=anomalies_filtered,
    )[:limit]
    selected = push_candidates
    messages = [item["message"] for item in push_candidates]

    if dry_run:
        return {
            "dry_run": True,
            "trade_date": snapshot.get("trade_date"),
            "snapshot_time": snapshot.get("snapshot_time"),
            "channel": channel,
            "allow_limit_up": allow_limit_up,
            "selected": [
                {
                    "code": (item.get("anomaly") or {}).get("code"),
                    "name": (item.get("anomaly") or {}).get("name"),
                    "event_type": (item.get("anomaly") or {}).get("event_type"),
                    "score": (item.get("anomaly") or {}).get("score"),
                    "description": (item.get("anomaly") or {}).get("description"),
                    "alert_tier": item.get("alert_tier"),
                    "current_primary": _serialize_push_entry(item.get("primary_entry") or {}),
                    "selected_signals": [
                        _serialize_push_entry(entry)
                        for entry in (item.get("changed_entries") or [])
                    ],
                    "secondary_signals": [
                        _serialize_push_entry(entry)
                        for entry in (item.get("secondary_entries") or [])
                    ],
                }
                for item in selected
            ],
        }

    if bypass_throttle:
        results = await _push_messages_direct(messages, channel)
    else:
        results = await push_scheduler.push_batch(messages)

    return {
        "dry_run": False,
        "trade_date": snapshot.get("trade_date"),
        "snapshot_time": snapshot.get("snapshot_time"),
        "channel": channel,
        "allow_limit_up": allow_limit_up,
        "bypass_throttle": bypass_throttle,
        "requested": len(selected),
        "pushed": len(messages),
        "sent": sum(1 for item in results if item.get("sent")),
        "throttled": sum(1 for item in results if item.get("throttled")),
        "results": results,
    }


# =========================================================================
# Tab 2: 强势排行 — 短线BullScore v2.0(7维) + 十倍潜力
# =========================================================================

@router.get("/rank")
async def strength_rank(
    mode: str = Query("bull", description="bull=短线强势/tenbagger=十倍潜力"),
    profile: str = Query("", description="筛选口径: volume_price_uptrend=短线上升+量价齐升"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=10, le=100, description="每页数量"),
    limit: int = Query(100, description="兼容旧版，未分页时返回数量"),
    db: AsyncSession = Depends(get_db),
):
    """强势排行"""
    payload = await (_tenbagger_rank(db) if mode == "tenbagger" else _bull_rank(db))
    rows_all = _filter_main_board_items(payload.get("rank", []))
    if profile == "volume_price_uptrend" and mode == "bull":
        rows_all = [
            row for row in rows_all
            if bool(row.get("is_volume_price_uptrend"))
        ]
    total = len(rows_all)
    use_pagination = page_size is not None and page_size > 0
    if use_pagination:
        offset = (page - 1) * page_size
        rows = rows_all[offset: offset + page_size]
    else:
        rows = rows_all[:limit]

    result = dict(payload)
    result["rank"] = rows
    result["total"] = total
    result["page"] = page
    result["page_size"] = page_size
    result["profile"] = profile
    return result


def _build_volume_price_uptrend_meta(
    *,
    price: float,
    change_pct: float,
    volume_ratio: float,
    turnover: float,
    main_net_inflow: float,
    main_net_inflow_5d: float,
    ma5: float,
    ma10: float,
    ma20: float,
    rsi14: float | None,
    is_limit_up: bool,
) -> dict:
    """短线趋势股口径: 先排趋势，再看量价和资金，最后避开追高过热。"""
    tags: list[str] = []
    blockers: list[str] = []
    score = 0

    ma_bull = price > 0 and ma5 > 0 and ma10 > 0 and ma20 > 0 and price > ma5 > ma10 > ma20
    price_up = 1.0 <= change_pct <= 7.0
    volume_price = change_pct > 0 and 1.2 <= volume_ratio <= 3.0
    turnover_ok = 2.0 <= turnover <= 15.0
    fund_ok = main_net_inflow > 0 or main_net_inflow_5d > 0
    not_overheated = (
        rsi14 is not None
        and not is_limit_up
        and rsi14 <= 80
        and change_pct <= 7.0
        and volume_ratio <= 3.0
    )

    if ma_bull:
        score += 30
        tags.append("短线多头")
    else:
        blockers.append("均线未形成短线多头")

    if price_up:
        score += 20
        tags.append(f"涨幅{change_pct:.1f}%")
    else:
        blockers.append("涨幅不在健康启动区")

    if volume_price:
        score += 25
        tags.append(f"量价齐升/量比{volume_ratio:.1f}")
    else:
        blockers.append("量价未同步转强")

    if turnover_ok:
        score += 10
        tags.append(f"换手{turnover:.1f}%")
    else:
        blockers.append("换手不在短线健康区")

    if fund_ok:
        score += 15
        tags.append("资金确认")
    else:
        blockers.append("资金未确认")

    if not not_overheated:
        blockers.append("涨停/超买/巨量过热")

    is_ready = ma_bull and price_up and volume_price and turnover_ok and fund_ok and not_overheated
    if is_ready and score >= 85:
        label = "量价齐升买点"
    elif ma_bull and volume_price:
        label = "量价齐升观察"
    elif ma_bull:
        label = "短线趋势观察"
    else:
        label = ""

    return {
        "is_volume_price_uptrend": is_ready,
        "short_trend_score": round(float(score), 1),
        "short_trend_label": label,
        "short_trend_tags": tags[:4],
        "short_trend_blockers": blockers[:3],
    }


def _mean_positive(values: list[float]) -> float:
    positives = [float(v) for v in values if v and v > 0]
    return sum(positives) / len(positives) if positives else 0.0


def _volume_ratio_bucket(volume_ratio: float) -> str:
    if volume_ratio >= 3:
        return "放巨量"
    if volume_ratio >= 1.5:
        return "放量"
    if volume_ratio >= 0.8:
        return "温和"
    return "缩量"


def _turnover_bucket(turnover: float) -> str:
    if turnover >= 20:
        return "极高换手"
    if turnover >= 10:
        return "高换手"
    if turnover >= 3:
        return "活跃"
    return "低换手"


def _price_volume_relation(change_pct: float, volume_ratio: float) -> str:
    if change_pct > 0 and volume_ratio >= 1.2:
        return "价升量增"
    if change_pct > 0 and volume_ratio < 0.8:
        return "价升量缩"
    if change_pct < 0 and volume_ratio >= 1.2:
        return "放量回落"
    return "量价平衡"


def _main_wave_pullback_shape_stats(
    *,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    opens: list[float] | None = None,
) -> dict:
    """识别主升过程中的首阴或缩量十字星，只使用当日及更早K线。"""
    size = min(len(closes), len(highs), len(lows), len(volumes))
    if size < 20:
        return {}
    clean_closes = [_safe_float(value) for value in closes[-size:]]
    clean_highs = [_safe_float(value) for value in highs[-size:]]
    clean_lows = [_safe_float(value) for value in lows[-size:]]
    clean_volumes = [_safe_float(value) for value in volumes[-size:]]
    clean_opens = [_safe_float(value) for value in (opens or [])[-size:]]
    if any(value <= 0 for value in clean_closes[-20:] + clean_highs[-20:] + clean_lows[-20:]):
        return {}

    last_close = clean_closes[-1]
    prev_close = clean_closes[-2]
    last_high = clean_highs[-1]
    last_low = clean_lows[-1]
    last_volume = clean_volumes[-1]
    ma5 = _mean_positive(clean_closes[-5:])
    ma10 = _mean_positive(clean_closes[-10:])
    ma20 = _mean_positive(clean_closes[-20:])
    day_change_pct = (last_close / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
    prior_changes = [
        (clean_closes[index] / clean_closes[index - 1] - 1.0) * 100.0
        for index in range(size - 6, size - 1)
        if clean_closes[index - 1] > 0
    ]
    prior_changes_10 = [
        (clean_closes[index] / clean_closes[index - 1] - 1.0) * 100.0
        for index in range(size - 11, size - 1)
        if clean_closes[index - 1] > 0
    ]
    prior_up_days = sum(change > 0.0 for change in prior_changes)
    prior_up_days_10 = sum(change > 0.0 for change in prior_changes_10)
    prior_impulse_days_10 = sum(change >= 5.0 for change in prior_changes_10)
    prior_gain_pct = (
        (prev_close / clean_closes[-6] - 1.0) * 100.0
        if clean_closes[-6] > 0
        else 0.0
    )
    prior_gain_pct_10 = (
        (prev_close / clean_closes[-11] - 1.0) * 100.0
        if clean_closes[-11] > 0
        else 0.0
    )
    prior_gain_pct_20 = (
        (prev_close / clean_closes[-20] - 1.0) * 100.0
        if clean_closes[-20] > 0
        else 0.0
    )
    avg_volume_5_before = _mean_positive(clean_volumes[-6:-1])
    volume_contraction = (
        last_volume / avg_volume_5_before
        if avg_volume_5_before > 0
        else 1.0
    )
    candle_range = max(last_high - last_low, 0.0)
    last_open = clean_opens[-1] if len(clean_opens) == size and clean_opens[-1] > 0 else prev_close
    body_ratio = abs(last_close - last_open) / candle_range if candle_range > 0 else 1.0
    close_position = (
        (last_close - last_low) / candle_range
        if candle_range > 0
        else 0.5
    )
    high_20 = max(clean_highs[-20:])
    near_high_ratio = last_close / high_20 if high_20 > 0 else 0.0
    first_bearish = bool(
        prior_up_days >= 4
        and prior_gain_pct >= 8.0
        and -4.8 <= day_change_pct <= -0.15
        and last_close >= ma10 * 0.985
        and volume_contraction <= 1.05
    )
    low_volume_doji = bool(
        prior_up_days >= 3
        and prior_gain_pct >= 6.0
        and abs(day_change_pct) <= 1.25
        and body_ratio <= 0.38
        and volume_contraction <= 0.78
        and last_close >= ma10 * 0.985
    )
    # 强趋势首阴的另一类是“放量深洗”。它不是当日可直接抄底的买点，
    # 但如果完全按普通破位删除，次日低开下探后收回昨收时系统已失去记忆，
    # 也就无法捕捉风范股份/金健米业一类弱转强。这里只建立次日回收观察，
    # 必须等滚动60秒增量成交、VWAP、资金与板块共同确认，不能直接开仓。
    deep_wash_first_bearish = bool(
        prior_up_days_10 >= 4
        and prior_impulse_days_10 >= 1
        and (prior_gain_pct_10 >= 12.0 or prior_gain_pct_20 >= 25.0)
        and -7.8 <= day_change_pct <= -2.5
        and last_close >= ma10 * 0.97
        and last_close >= ma20 * 1.02
        and near_high_ratio >= 0.86
        # 深洗必须有真实分歧成交；缩量中阴继续归普通首阴观察，不能因为
        # 跌幅稍深就升级成“次日弱转强”记忆。
        and 1.05 <= volume_contraction <= 3.5
    )
    if not (first_bearish or low_volume_doji or deep_wash_first_bearish):
        return {}

    # “首阴”只描述K线颜色；真正的缩量价稳回踩必须同时满足跌幅可控、
    # 成交显著收缩且仍贴近MA5。宽泛首阴继续观察，但不能等同严格买点。
    price_stable = bool(-2.0 <= day_change_pct <= 0.8 and last_close >= ma5 * 0.985)
    volume_dry_up = bool(volume_contraction <= 0.80)
    strict_pullback = bool(price_stable and volume_dry_up)

    # 十字星是更具体的K线语义；同为小阴线时优先归入缩量十字星。
    shape_type = (
        "low_volume_doji"
        if low_volume_doji
        else "deep_wash_first_bearish"
        if deep_wash_first_bearish
        else "first_bearish"
    )
    shape_label = (
        "主升浪缩量十字星"
        if low_volume_doji
        else "主升首阴深洗观察"
        if deep_wash_first_bearish
        else "主升浪首阴"
    )
    quality_tier = (
        "strict"
        if strict_pullback
        else "reclaim"
        if deep_wash_first_bearish
        else "watch"
    )
    # 形态成立不等于具备持续性。单独给出只基于历史K线的趋势持续分，供
    # 晋级排序和盘中回收闸门使用；消息、板块、资金仍在各自链路独立确认。
    # 这样既不会把一次偶发大阳误当主升，也不会让“深洗记忆”只剩布尔值。
    persistence_score = 38.0
    persistence_score += min(prior_up_days_10, 6) * 2.5
    persistence_score += min(prior_impulse_days_10, 3) * 4.0
    if 12.0 <= prior_gain_pct_20 <= 60.0:
        persistence_score += 8.0
    elif 6.0 <= prior_gain_pct_20 <= 75.0:
        persistence_score += 4.0
    elif prior_gain_pct_20 < 0.0:
        persistence_score -= 14.0
    elif prior_gain_pct_20 < 6.0:
        persistence_score -= 8.0
    elif prior_gain_pct_20 > 90.0:
        persistence_score -= 10.0
    if 0.88 <= near_high_ratio <= 0.98:
        persistence_score += 6.0
    elif near_high_ratio >= 0.84:
        persistence_score += 3.0
    if strict_pullback:
        persistence_score += 8.0
    elif deep_wash_first_bearish and volume_contraction <= 1.80:
        persistence_score += 6.0
    elif deep_wash_first_bearish and volume_contraction <= 2.30:
        persistence_score += 2.0
    else:
        persistence_score -= 4.0
    if 0.12 <= close_position <= 0.68:
        persistence_score += 4.0
    persistence_score = round(max(0.0, min(persistence_score, 100.0)), 1)
    persistence_tier = (
        "high"
        if persistence_score >= 76.0
        else "medium"
        if persistence_score >= 66.0
        else "low"
    )
    return {
        "pullback_shape_ready": True,
        "pullback_shape_type": shape_type,
        "pullback_shape_label": shape_label,
        "pullback_anchor_low": round(last_low, 3),
        "pullback_anchor_close": round(last_close, 3),
        "pullback_day_change_pct": round(day_change_pct, 2),
        "pullback_volume_contraction": round(volume_contraction, 3),
        "pullback_body_ratio": round(body_ratio, 3),
        "pullback_close_position": round(close_position, 3),
        "pullback_prior_up_days": prior_up_days,
        "pullback_prior_up_days_10": prior_up_days_10,
        "pullback_prior_impulse_days_10": prior_impulse_days_10,
        "pullback_prior_gain_pct": round(prior_gain_pct, 2),
        "pullback_prior_gain_pct_10": round(prior_gain_pct_10, 2),
        "pullback_prior_gain_pct_20": round(prior_gain_pct_20, 2),
        "pullback_near_high_ratio": round(near_high_ratio, 3),
        "pullback_price_stable": price_stable,
        "pullback_volume_dry_up": volume_dry_up,
        "pullback_reclaim_price": round(last_close, 3),
        "pullback_requires_next_session_reclaim": deep_wash_first_bearish,
        "pullback_quality_tier": quality_tier,
        "pullback_quality_label": (
            "主升缩量价稳回踩"
            if strict_pullback
            else "主升深洗·次日收回确认"
            if deep_wash_first_bearish
            else "普通首阴观察"
        ),
        "pullback_technical_persistence_score": persistence_score,
        "pullback_technical_persistence_tier": persistence_tier,
        "pullback_technical_persistence_label": (
            "技术持续性高"
            if persistence_tier == "high"
            else "技术持续性中等"
            if persistence_tier == "medium"
            else "技术持续性不足"
        ),
        "ma5": round(ma5, 2),
        "ma10": round(ma10, 2),
        "ma20": round(ma20, 2),
    }


def _score_main_wave_kline_pattern(
    *,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    opens: list[float] | None = None,
    change_pct: float = 0.0,
    turnover: float = 0.0,
    volume_ratio: float = 1.0,
) -> dict:
    """主升浪形态候选: 用K线结构识别多板/趋势加速，不依赖当日涨停池。"""
    clean_closes = [float(v) for v in closes if v and v > 0]
    clean_highs = [float(v) for v in highs if v and v > 0]
    clean_lows = [float(v) for v in lows if v and v > 0]
    clean_volumes = [float(v) for v in volumes if v and v > 0]
    if len(clean_closes) < 20 or len(clean_highs) < 20 or len(clean_lows) < 20:
        return {
            "is_main_wave_pattern": False,
            "main_wave_score": 0.0,
            "main_wave_label": "",
            "main_wave_tags": [],
            "main_wave_blockers": ["K线样本不足"],
            "main_wave_stats": {},
        }

    last_close = clean_closes[-1]
    ma5 = _mean_positive(clean_closes[-5:])
    ma10 = _mean_positive(clean_closes[-10:])
    ma20 = _mean_positive(clean_closes[-20:])
    high_12 = max(clean_highs[-12:])
    high_20 = max(clean_highs[-20:])
    low_recent_5 = min(clean_lows[-5:])
    low_prev_5 = min(clean_lows[-10:-5])

    pct_12 = (last_close / clean_closes[-12] - 1) * 100 if clean_closes[-12] > 0 else 0.0
    pct_16 = (last_close / clean_closes[-16] - 1) * 100 if clean_closes[-16] > 0 else 0.0
    pct_20 = (last_close / clean_closes[-20] - 1) * 100 if clean_closes[-20] > 0 else 0.0
    daily_changes = [
        (clean_closes[i] / clean_closes[i - 1] - 1) * 100
        for i in range(max(1, len(clean_closes) - 15), len(clean_closes))
        if clean_closes[i - 1] > 0
    ]
    board_like_count_12 = sum(1 for pct in daily_changes[-12:] if pct >= 8.8)
    up_days_12 = sum(1 for pct in daily_changes[-12:] if pct > 0)
    near_high_ratio = last_close / high_20 if high_20 > 0 else 0.0
    pullback_pct = (high_12 - last_close) / high_12 * 100 if high_12 > 0 else 100.0
    vol_expansion = 0.0
    if len(clean_volumes) >= 20:
        base_vol = _mean_positive(clean_volumes[-20:-5])
        recent_vol = _mean_positive(clean_volumes[-5:])
        vol_expansion = recent_vol / base_vol if base_vol > 0 else 0.0

    score = 0.0
    tags: list[str] = []
    blockers: list[str] = []

    if pct_12 >= 18:
        trend_score = min(28.0, max(12.0, pct_12 * 0.45))
        score += trend_score
        tags.append(f"12日涨幅{pct_12:.1f}%")
    else:
        blockers.append("12日涨幅未进入主升浪区")

    if board_like_count_12 > 0:
        score += min(24.0, board_like_count_12 * 8.0)
        tags.append(f"近12日{board_like_count_12}次类涨停")
    elif pct_12 >= 35:
        score += 10.0
        tags.append("趋势加速")
    else:
        blockers.append("缺少涨停或加速段")

    ma_stack = last_close > ma5 > ma10 > ma20 > 0
    if ma_stack:
        score += 20.0
        tags.append("均线多头")
    elif last_close > ma10 > 0:
        score += 10.0
        tags.append("站上10日线")
    else:
        blockers.append("未站稳短中期均线")

    if near_high_ratio >= 0.92:
        score += 12.0
        tags.append("贴近20日高点")
    elif near_high_ratio >= 0.88:
        score += 8.0
        tags.append("接近20日高点")
    else:
        blockers.append("距离阶段高点过远")

    if pullback_pct <= 10:
        score += 8.0
        tags.append(f"回撤{pullback_pct:.1f}%")
    elif pullback_pct <= 18:
        score += 4.0
        tags.append(f"回撤{pullback_pct:.1f}%")
    else:
        blockers.append("阶段回撤过深")

    if vol_expansion >= 1.2 or turnover >= 3:
        score += 8.0
        tags.append("量能确认")
    elif volume_ratio >= 1.1 or turnover >= 1.5:
        score += 4.0
        tags.append("量能温和")
    else:
        blockers.append("量能确认不足")

    higher_low = low_prev_5 > 0 and low_recent_5 >= low_prev_5 * 0.98
    if higher_low:
        score += 5.0
        tags.append("低点抬高")

    if up_days_12 >= 7:
        score += 3.0
        tags.append(f"近12日{up_days_12}阳线")

    if change_pct < -5:
        blockers.append("当日跌幅破坏形态")

    pullback_shape = _main_wave_pullback_shape_stats(
        closes=clean_closes,
        highs=clean_highs,
        lows=clean_lows,
        volumes=clean_volumes,
        opens=opens,
    )
    if pullback_shape:
        score += 6.0
        tags.append(str(pullback_shape.get("pullback_shape_label") or "主升回踩"))

    is_ready = (
        score >= 70
        and pct_12 >= 18
        and near_high_ratio >= 0.88
        and pullback_pct <= 18
        and last_close > ma10 > 0
        and change_pct > -5
        and (board_like_count_12 >= 1 or pct_12 >= 35)
    )
    label = "多板主升浪跟踪" if board_like_count_12 >= 2 else "趋势主升浪跟踪"
    if not is_ready and not blockers:
        blockers.append("主升浪确认度不足")

    return {
        "is_main_wave_pattern": is_ready,
        "main_wave_score": round(float(score), 1),
        "main_wave_label": label if is_ready else "",
        "main_wave_tags": tags[:5],
        "main_wave_blockers": blockers[:3],
        "main_wave_stats": {
            "setup_state": "armed_main_wave_pullback" if is_ready else "candidate",
            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "ma20": round(ma20, 2),
            "support": round(max(ma5, ma10), 2),
            "resistance": round(high_12, 2),
            "pct_12": round(pct_12, 2),
            "pct_16": round(pct_16, 2),
            "pct_20": round(pct_20, 2),
            "board_like_count_12": board_like_count_12,
            "up_days_12": up_days_12,
            "near_high_ratio": round(near_high_ratio, 3),
            "pullback_pct": round(pullback_pct, 2),
            "vol_expansion": round(vol_expansion, 2),
            **pullback_shape,
        },
    }


def _score_second_wave_kline_pattern(
    *,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    change_pct: float = 0.0,
    turnover: float = 0.0,
    volume_ratio: float = 1.0,
) -> dict:
    """高标断板后二波: 先识别前高标，再看断板后的修复与趋势再加速。"""
    clean_closes = [float(v) for v in closes if v and v > 0]
    clean_highs = [float(v) for v in highs if v and v > 0]
    clean_lows = [float(v) for v in lows if v and v > 0]
    clean_volumes = [float(v) for v in volumes if v and v > 0]
    if len(clean_closes) < 24 or len(clean_highs) < 24 or len(clean_lows) < 24:
        return {
            "is_second_wave_pattern": False,
            "second_wave_score": 0.0,
            "second_wave_label": "",
            "second_wave_tags": [],
            "second_wave_blockers": ["K线样本不足"],
            "second_wave_stats": {},
        }

    daily_changes = [
        (clean_closes[i] / clean_closes[i - 1] - 1) * 100
        if clean_closes[i - 1] > 0 else 0.0
        for i in range(1, len(clean_closes))
    ]
    # 二波必须锚定最近一段高标记忆。旧实现遇到多段相同连板高度时会一直
    # 保留最早一段，导致断板日、回撤和修复幅度全部引用过期行情。
    recent_window_days = min(80, len(clean_closes) - 1)
    recent_start_index = max(1, len(clean_closes) - recent_window_days)
    board_like_indexes = [
        idx + 1
        for idx, pct in enumerate(daily_changes)
        if idx + 1 >= recent_start_index and pct >= 8.8
    ]
    cluster_end = -1
    max_streak = 0
    current_streak = 0
    for idx, pct in enumerate(daily_changes, start=1):
        if idx < recent_start_index:
            continue
        if pct >= 8.8:
            current_streak += 1
            # 高度相同时取最新一段，避免被数月前的旧连板锚点污染。
            if current_streak >= max_streak:
                max_streak = current_streak
                cluster_end = idx
        else:
            current_streak = 0
    if max_streak < 3 and len(board_like_indexes) < 4:
        return {
            "is_second_wave_pattern": False,
            "second_wave_score": 0.0,
            "second_wave_label": "",
            "second_wave_tags": [],
            "second_wave_blockers": ["前段高标强度不足"],
            "second_wave_stats": {
                "board_like_count_30": len(board_like_indexes),
                "max_board_streak": max_streak,
                "board_memory_window_days": recent_window_days,
            },
        }

    if cluster_end < 0:
        cluster_end = board_like_indexes[-1]
    if len(clean_closes) - cluster_end < 6:
        return {
            "is_second_wave_pattern": False,
            "second_wave_score": 0.0,
            "second_wave_label": "",
            "second_wave_tags": [],
            "second_wave_blockers": ["断板后修复周期不足"],
            "second_wave_stats": {
                "board_like_count_30": len(board_like_indexes),
                "max_board_streak": max_streak,
                "board_memory_window_days": recent_window_days,
            },
        }

    post_start = min(cluster_end + 1, len(clean_closes) - 1)
    cluster_start = max(recent_start_index, cluster_end - max(max_streak, 1) + 1)
    # 前高必须锚定最近一段高标，而不是全历史最高价；否则复权前旧高会把
    # 美利云式二波的回撤/修复幅度严重放大，直到三板后才被识别。
    prior_peak = max(clean_highs[cluster_start:post_start + 1])
    post_lows = clean_lows[post_start:]
    post_closes = clean_closes[post_start:]
    post_daily_changes = daily_changes[post_start - 1:]
    post_low = min(post_lows) if post_lows else 0.0
    last_close = clean_closes[-1]
    last_high_10 = max(clean_highs[-10:])
    ma5 = _mean_positive(clean_closes[-5:])
    ma10 = _mean_positive(clean_closes[-10:])
    ma20 = _mean_positive(clean_closes[-20:])
    low_recent_5 = min(clean_lows[-5:])
    low_prev_5 = min(clean_lows[-10:-5])

    break_down_pct = (post_low / prior_peak - 1) * 100 if prior_peak > 0 and post_low > 0 else 0.0
    has_break_day = any(pct <= -8.0 for pct in post_daily_changes[:6])
    has_broken_board = has_break_day or break_down_pct <= -12
    rebound_from_low = (last_close / post_low - 1) * 100 if post_low > 0 else 0.0
    reclaim_peak_ratio = last_close / prior_peak if prior_peak > 0 else 0.0
    near_recent_high_ratio = last_close / last_high_10 if last_high_10 > 0 else 0.0
    post_up_days = sum(1 for i in range(1, len(post_closes)) if post_closes[i] > post_closes[i - 1])
    vol_expansion = 0.0
    if len(clean_volumes) >= 20:
        base_vol = _mean_positive(clean_volumes[-20:-5])
        recent_vol = _mean_positive(clean_volumes[-5:])
        vol_expansion = recent_vol / base_vol if base_vol > 0 else 0.0

    score = 0.0
    tags: list[str] = []
    blockers: list[str] = []

    score += min(24.0, max_streak * 6.0)
    tags.append(f"前段{max_streak}连类涨停")
    if len(board_like_indexes) >= 4:
        score += min(10.0, (len(board_like_indexes) - 3) * 3.0)
        tags.append(f"累计{len(board_like_indexes)}次类涨停")

    if has_broken_board:
        score += 16.0
        tags.append(f"断板回撤{abs(break_down_pct):.1f}%")
    else:
        blockers.append("断板急杀/洗盘不明确")

    if rebound_from_low >= 18:
        score += 18.0
        tags.append(f"低点修复{rebound_from_low:.1f}%")
    elif rebound_from_low >= 10:
        score += 10.0
        tags.append(f"低点修复{rebound_from_low:.1f}%")
    else:
        blockers.append("断板低点修复不足")

    if last_close > ma5 > ma10 > 0 and last_close > ma20:
        score += 18.0
        tags.append("修复后均线重多头")
    elif last_close > ma10 > 0:
        score += 10.0
        tags.append("重新站上10日线")
    else:
        blockers.append("修复段未站稳均线")

    if reclaim_peak_ratio >= 0.92:
        score += 12.0
        tags.append("逼近前高")
    elif reclaim_peak_ratio >= 0.80:
        score += 8.0
        tags.append("回到前高区间")
    else:
        blockers.append("距离前高仍远")

    if near_recent_high_ratio >= 0.94:
        score += 6.0
        tags.append("贴近修复高点")

    higher_low = low_prev_5 > 0 and low_recent_5 >= low_prev_5 * 0.98
    if higher_low:
        score += 6.0
        tags.append("二波低点抬高")

    if vol_expansion >= 1.15 or turnover >= 4:
        score += 6.0
        tags.append("二波量能确认")
    elif volume_ratio >= 1.1:
        score += 3.0
        tags.append("二波量能温和")
    else:
        blockers.append("二波量能不足")

    if post_up_days >= 4:
        score += 4.0
        tags.append(f"修复段{post_up_days}日收涨")

    if change_pct < -5:
        blockers.append("当日跌幅破坏二波形态")

    is_ready = (
        score >= 72
        and has_broken_board
        and rebound_from_low >= 10
        and reclaim_peak_ratio >= 0.78
        and last_close > ma10 > 0
        and near_recent_high_ratio >= 0.90
        and change_pct > -5
    )
    if not is_ready and not blockers:
        blockers.append("二波确认度不足")

    return {
        "is_second_wave_pattern": is_ready,
        "second_wave_score": round(float(score), 1),
        "second_wave_label": "断板二波主升跟踪" if is_ready else "",
        "second_wave_tags": tags[:5],
        "second_wave_blockers": blockers[:3],
        "second_wave_stats": {
            "board_like_count_30": len(board_like_indexes),
            "max_board_streak": max_streak,
            "days_after_break": len(clean_closes) - cluster_end - 1,
            "break_down_pct": round(break_down_pct, 2),
            "rebound_from_low_pct": round(rebound_from_low, 2),
            "reclaim_peak_ratio": round(reclaim_peak_ratio, 3),
            "near_recent_high_ratio": round(near_recent_high_ratio, 3),
            "post_up_days": post_up_days,
            "vol_expansion": round(vol_expansion, 2),
            "support": round(ma10, 2),
            "resistance": round(max(prior_peak, last_high_10), 2),
            "setup_state": "armed_pullback",
            "board_memory_window_days": recent_window_days,
        },
    }


def _score_second_wave_reset_watch_pattern(
    *,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    change_pct: float = 0.0,
) -> dict:
    """首波高标/趋势高点下杀后的二波准备态，只入检测池不直接给买点。"""
    clean_closes = [float(v) for v in closes if v and v > 0]
    clean_highs = [float(v) for v in highs if v and v > 0]
    clean_lows = [float(v) for v in lows if v and v > 0]
    clean_volumes = [float(v) for v in volumes if v and v > 0]
    size = min(
        len(clean_closes),
        len(clean_highs),
        len(clean_lows),
        len(clean_volumes),
    )
    if size < 30:
        return {
            "is_second_wave_reset_watch": False,
            "second_wave_reset_score": 0.0,
            "second_wave_reset_label": "",
            "second_wave_reset_tags": [],
            "second_wave_reset_blockers": ["K线样本不足"],
            "second_wave_reset_stats": {},
        }

    clean_closes = clean_closes[-size:]
    clean_highs = clean_highs[-size:]
    clean_lows = clean_lows[-size:]
    clean_volumes = clean_volumes[-size:]
    daily_changes = [
        (clean_closes[index] / clean_closes[index - 1] - 1.0) * 100.0
        if clean_closes[index - 1] > 0
        else 0.0
        for index in range(1, size)
    ]

    # 只寻找2~15个交易日前的最近首波高点，避免把当前正在形成的新高误作旧峰。
    peak_search_start = max(10, size - 16)
    peak_search_end = size - 2
    if peak_search_start >= peak_search_end:
        return {
            "is_second_wave_reset_watch": False,
            "second_wave_reset_score": 0.0,
            "second_wave_reset_label": "",
            "second_wave_reset_tags": [],
            "second_wave_reset_blockers": ["首波高点距离不足"],
            "second_wave_reset_stats": {},
        }
    peak_index = max(
        range(peak_search_start, peak_search_end),
        key=lambda index: clean_highs[index],
    )
    peak_price = clean_highs[peak_index]
    days_since_peak = size - peak_index - 1
    post_lows = clean_lows[peak_index + 1:]
    post_highs = clean_highs[peak_index + 1:]
    if peak_price <= 0 or not post_lows:
        return {
            "is_second_wave_reset_watch": False,
            "second_wave_reset_score": 0.0,
            "second_wave_reset_label": "",
            "second_wave_reset_tags": [],
            "second_wave_reset_blockers": ["首波高点后样本不足"],
            "second_wave_reset_stats": {},
        }

    post_low = min(post_lows)
    drawdown_pct = (post_low / peak_price - 1.0) * 100.0
    rebound_from_low_pct = (
        (clean_closes[-1] / post_low - 1.0) * 100.0 if post_low > 0 else 0.0
    )
    pre_wave_start = max(0, peak_index - 25)
    pre_wave_low = min(clean_lows[pre_wave_start:peak_index + 1])
    first_wave_gain_pct = (
        (peak_price / pre_wave_low - 1.0) * 100.0 if pre_wave_low > 0 else 0.0
    )

    board_changes = daily_changes[max(0, pre_wave_start - 1):peak_index]
    max_board_streak = 0
    current_streak = 0
    for pct in board_changes:
        if pct >= 8.8:
            current_streak += 1
            max_board_streak = max(max_board_streak, current_streak)
        else:
            current_streak = 0
    post_changes = daily_changes[peak_index:]
    limit_down_like_count = sum(1 for pct in post_changes if pct <= -8.8)
    down_day_count = sum(1 for pct in post_changes if pct <= -3.0)

    pre_volume_start = max(0, peak_index - 9)
    pre_wave_volume = _mean_positive(
        clean_volumes[pre_volume_start:peak_index + 1]
    )
    recent_volume = _mean_positive(clean_volumes[-min(3, days_since_peak):])
    dry_up_ratio = recent_volume / pre_wave_volume if pre_wave_volume > 0 else 1.0
    close_range = clean_highs[-1] - clean_lows[-1]
    close_location = (
        (clean_closes[-1] - clean_lows[-1]) / close_range
        if close_range > 0
        else 0.5
    )

    high_board_reset = bool(
        max_board_streak >= 3
        and 3 <= days_since_peak <= 15
        and -35.0 <= drawdown_pct <= -12.0
        and rebound_from_low_pct <= 12.0
        and down_day_count >= 2
    )
    trend_cascade_reset = bool(
        first_wave_gain_pct >= 35.0
        and 3 <= days_since_peak <= 15
        and -55.0 <= drawdown_pct <= -25.0
        and rebound_from_low_pct <= 12.0
        and limit_down_like_count >= 2
        and down_day_count >= 3
        and dry_up_ratio <= 0.9
    )
    is_ready = high_board_reset or trend_cascade_reset
    reset_type = (
        "high_board_reset" if high_board_reset else "trend_cascade_reset"
        if trend_cascade_reset else ""
    )

    score = 0.0
    tags: list[str] = []
    blockers: list[str] = []
    if max_board_streak >= 3:
        score += 24.0
        tags.append(f"首波{max_board_streak}连板")
    elif first_wave_gain_pct >= 35.0:
        score += 22.0
        tags.append(f"首波主升{first_wave_gain_pct:.1f}%")
    else:
        blockers.append("首波高标/主升记忆不足")
    if -55.0 <= drawdown_pct <= -12.0:
        score += 22.0
        tags.append(f"高点下杀{abs(drawdown_pct):.1f}%")
    else:
        blockers.append("回撤深度不在二波重置区")
    if 3 <= days_since_peak <= 15:
        score += 12.0
        tags.append(f"调整{days_since_peak}日")
    else:
        blockers.append("调整时间不在二波窗口")
    if rebound_from_low_pct <= 12.0:
        score += 10.0
        tags.append("仍在下杀低位区")
    if limit_down_like_count >= 2:
        score += 8.0
        tags.append(f"连续急杀{limit_down_like_count}日")
    if dry_up_ratio <= 0.9:
        score += 8.0
        tags.append(f"末端量能收缩{dry_up_ratio:.2f}")
    if change_pct > 3.0 or close_location >= 0.72:
        tags.append("已出现日内止跌迹象")

    resistance = max(post_highs) if post_highs else peak_price
    if resistance <= post_low:
        resistance = peak_price
    return {
        "is_second_wave_reset_watch": is_ready,
        "second_wave_reset_score": round(min(score, 90.0), 1),
        "second_wave_reset_label": "高标下杀二波准备" if is_ready else "",
        "second_wave_reset_tags": tags[:6],
        "second_wave_reset_blockers": blockers[:3],
        "second_wave_reset_stats": {
            "setup_state": "armed_second_wave_reset",
            "reset_type": reset_type,
            "peak_price": round(peak_price, 2),
            "support": round(post_low, 2),
            "resistance": round(resistance, 2),
            "days_since_peak": days_since_peak,
            "first_wave_gain_pct": round(first_wave_gain_pct, 2),
            "max_board_streak": max_board_streak,
            "drawdown_pct": round(drawdown_pct, 2),
            "rebound_from_low_pct": round(rebound_from_low_pct, 2),
            "limit_down_like_count": limit_down_like_count,
            "down_day_count": down_day_count,
            "dry_up_ratio": round(dry_up_ratio, 3),
            "close_location": round(close_location, 3),
        },
    }


def _score_trend_main_wave_kline_pattern(
    *,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    opens: list[float] | None = None,
    change_pct: float = 0.0,
    turnover: float = 0.0,
    volume_ratio: float = 1.0,
) -> dict:
    """趋势主升加速: 不强制涨停，强调均线坡度、贴线运行、回撤受控和量能承接。"""
    clean_closes = [float(v) for v in closes if v and v > 0]
    clean_highs = [float(v) for v in highs if v and v > 0]
    clean_lows = [float(v) for v in lows if v and v > 0]
    clean_volumes = [float(v) for v in volumes if v and v > 0]
    if len(clean_closes) < 30 or len(clean_highs) < 30 or len(clean_lows) < 30:
        return {
            "is_trend_main_wave_pattern": False,
            "trend_main_wave_score": 0.0,
            "trend_main_wave_label": "",
            "trend_main_wave_tags": [],
            "trend_main_wave_blockers": ["K线样本不足"],
            "trend_main_wave_stats": {},
        }

    last_close = clean_closes[-1]
    ma5 = _mean_positive(clean_closes[-5:])
    ma10 = _mean_positive(clean_closes[-10:])
    ma20 = _mean_positive(clean_closes[-20:])
    prev_ma10 = _mean_positive(clean_closes[-20:-10])
    prev_ma20 = _mean_positive(clean_closes[-30:-10])
    pct_10 = (last_close / clean_closes[-10] - 1) * 100 if clean_closes[-10] > 0 else 0.0
    pct_20 = (last_close / clean_closes[-20] - 1) * 100 if clean_closes[-20] > 0 else 0.0
    pct_30 = (last_close / clean_closes[-30] - 1) * 100 if clean_closes[-30] > 0 else 0.0
    ma10_slope = (ma10 / prev_ma10 - 1) * 100 if prev_ma10 > 0 else 0.0
    ma20_slope = (ma20 / prev_ma20 - 1) * 100 if prev_ma20 > 0 else 0.0
    high_20 = max(clean_highs[-20:])
    near_high_ratio = last_close / high_20 if high_20 > 0 else 0.0
    pullback_pct = (high_20 - last_close) / high_20 * 100 if high_20 > 0 else 100.0
    above_ma10_days = sum(1 for close in clean_closes[-10:] if ma10 > 0 and close >= ma10 * 0.98)
    up_days_20 = sum(
        1 for i in range(len(clean_closes) - 19, len(clean_closes))
        if clean_closes[i] > clean_closes[i - 1]
    )
    board_like_count_20 = sum(
        1 for i in range(len(clean_closes) - 19, len(clean_closes))
        if clean_closes[i - 1] > 0 and (clean_closes[i] / clean_closes[i - 1] - 1) * 100 >= 8.8
    )
    max_drawdown_20 = 0.0
    running_high = clean_highs[-20]
    for high, low in zip(clean_highs[-20:], clean_lows[-20:]):
        running_high = max(running_high, high)
        if running_high > 0 and low > 0:
            max_drawdown_20 = max(max_drawdown_20, (running_high - low) / running_high * 100)
    low_recent_5 = min(clean_lows[-5:])
    low_prev_5 = min(clean_lows[-10:-5])
    vol_expansion = 0.0
    if len(clean_volumes) >= 30:
        base_vol = _mean_positive(clean_volumes[-30:-10])
        recent_vol = _mean_positive(clean_volumes[-5:])
        vol_expansion = recent_vol / base_vol if base_vol > 0 else 0.0

    score = 0.0
    tags: list[str] = []
    blockers: list[str] = []

    if pct_20 >= 25:
        score += min(24.0, max(12.0, pct_20 * 0.35))
        tags.append(f"20日趋势{pct_20:.1f}%")
    elif pct_30 >= 30:
        score += 14.0
        tags.append(f"30日趋势{pct_30:.1f}%")
    else:
        blockers.append("中期趋势斜率不足")

    if ma5 > ma10 > ma20 > 0 and last_close > ma5:
        score += 20.0
        tags.append("均线贴合上行")
    elif last_close > ma10 > ma20 > 0:
        score += 12.0
        tags.append("站稳上升通道")
    else:
        blockers.append("均线通道未成形")

    if ma10_slope >= 8 and ma20_slope >= 5:
        score += 16.0
        tags.append("均线斜率加速")
    elif ma10_slope >= 4 and ma20_slope >= 2:
        score += 10.0
        tags.append("均线斜率向上")
    else:
        blockers.append("均线斜率不足")

    if above_ma10_days >= 8:
        score += 10.0
        tags.append(f"{above_ma10_days}日贴线运行")
    elif above_ma10_days >= 5:
        score += 6.0
        tags.append(f"{above_ma10_days}日站上MA10")
    elif above_ma10_days >= 4:
        score += 4.0
        tags.append(f"{above_ma10_days}日贴近MA10")
    else:
        blockers.append("贴线运行稳定性不足")

    if near_high_ratio >= 0.94:
        score += 10.0
        tags.append("贴近趋势新高")
    elif near_high_ratio >= 0.88:
        score += 6.0
        tags.append("接近趋势高点")
    else:
        blockers.append("离趋势高点偏远")

    if pullback_pct <= 8:
        score += 8.0
        tags.append(f"回撤{pullback_pct:.1f}%")
    elif pullback_pct <= 15:
        score += 4.0
        tags.append(f"回撤{pullback_pct:.1f}%")
    else:
        blockers.append("短线回撤过深")

    if max_drawdown_20 <= 22:
        score += 6.0
        tags.append("趋势回撤受控")
    elif max_drawdown_20 > 32:
        blockers.append("趋势波动过大")

    if vol_expansion >= 1.15 or turnover >= 4:
        score += 7.0
        tags.append("量能承接")
    elif volume_ratio >= 1.05:
        score += 4.0
        tags.append("量能温和")
    else:
        blockers.append("量能承接不足")

    higher_low = low_prev_5 > 0 and low_recent_5 >= low_prev_5 * 0.98
    if higher_low:
        score += 5.0
        tags.append("低点抬高")

    if up_days_20 >= 11:
        score += 4.0
        tags.append(f"20日{up_days_20}日收涨")

    if board_like_count_20 <= 2:
        score += 4.0
        tags.append("非连板趋势")

    if change_pct < -5:
        blockers.append("当日跌幅破坏趋势")

    pullback_shape = _main_wave_pullback_shape_stats(
        closes=clean_closes,
        highs=clean_highs,
        lows=clean_lows,
        volumes=clean_volumes,
        opens=opens,
    )
    if pullback_shape:
        score += 6.0
        tags.append(str(pullback_shape.get("pullback_shape_label") or "主升回踩"))

    is_ready = (
        score >= 74
        and (pct_20 >= 25 or pct_30 >= 35)
        and last_close > ma10 > ma20 > 0
        and ma10_slope >= 4
        and ma20_slope >= 2
        and above_ma10_days >= 4
        and near_high_ratio >= 0.88
        and pullback_pct <= 15
        and max_drawdown_20 <= 32
        and change_pct > -5
    )
    if not is_ready and not blockers:
        blockers.append("趋势主升确认度不足")

    return {
        "is_trend_main_wave_pattern": is_ready,
        "trend_main_wave_score": round(float(score), 1),
        "trend_main_wave_label": "趋势主升加速跟踪" if is_ready else "",
        "trend_main_wave_tags": tags[:5],
        "trend_main_wave_blockers": blockers[:3],
        "trend_main_wave_stats": {
            "setup_state": "armed_main_wave_pullback" if is_ready else "candidate",
            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "ma20": round(ma20, 2),
            "support": round(max(ma5, ma10), 2),
            "resistance": round(high_20, 2),
            "pct_10": round(pct_10, 2),
            "pct_20": round(pct_20, 2),
            "pct_30": round(pct_30, 2),
            "ma10_slope": round(ma10_slope, 2),
            "ma20_slope": round(ma20_slope, 2),
            "above_ma10_days": above_ma10_days,
            "up_days_20": up_days_20,
            "board_like_count_20": board_like_count_20,
            "near_high_ratio": round(near_high_ratio, 3),
            "pullback_pct": round(pullback_pct, 2),
            "max_drawdown_20": round(max_drawdown_20, 2),
            "vol_expansion": round(vol_expansion, 2),
            **pullback_shape,
        },
    }


def _score_trend_driver_kline_pattern(
    *,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    change_pct: float = 0.0,
    turnover: float = 0.0,
    volume_ratio: float = 1.0,
) -> dict:
    """趋势驱动候选：在主升加速前识别蓄势、首波回踩和阶梯启动。

    该函数只负责把股票放入观察池，不直接产生买入结论。所有特征只使用
    当前及更早的日K；真正的买点由盘中 VWAP、盘口承接和量能再次确认。
    """
    size = min(len(closes), len(highs), len(lows), len(volumes))
    if size < 60:
        return {
            "is_trend_driver_pattern": False,
            "trend_driver_score": 0.0,
            "trend_driver_label": "",
            "trend_driver_tags": [],
            "trend_driver_blockers": ["趋势驱动至少要求60根有效K线"],
            "trend_driver_stats": {},
        }

    clean_closes = [_safe_float(value) for value in closes[-size:]]
    clean_highs = [_safe_float(value) for value in highs[-size:]]
    clean_lows = [_safe_float(value) for value in lows[-size:]]
    clean_volumes = [_safe_float(value) for value in volumes[-size:]]
    if any(value <= 0 for value in clean_closes[-60:] + clean_highs[-60:] + clean_lows[-60:]):
        return {
            "is_trend_driver_pattern": False,
            "trend_driver_score": 0.0,
            "trend_driver_label": "",
            "trend_driver_tags": [],
            "trend_driver_blockers": ["近60日K线存在无效价格"],
            "trend_driver_stats": {},
        }

    last_close = clean_closes[-1]
    ma5 = _mean_positive(clean_closes[-5:])
    ma10 = _mean_positive(clean_closes[-10:])
    ma20 = _mean_positive(clean_closes[-20:])
    ma60 = _mean_positive(clean_closes[-60:])
    previous_ma20 = _mean_positive(clean_closes[-25:-5])
    ma20_slope_5d = (ma20 / previous_ma20 - 1.0) * 100.0 if previous_ma20 > 0 else 0.0
    pct_20 = (last_close / clean_closes[-20] - 1.0) * 100.0

    resistance = max(clean_highs[-20:-1])
    support = max(value for value in (ma10, ma20) if value > 0)
    distance_to_resistance_pct = (last_close / resistance - 1.0) * 100.0 if resistance > 0 else -100.0
    support_gap_pct = (last_close / support - 1.0) * 100.0 if support > 0 else 100.0
    range_10_pct = (max(clean_highs[-10:]) / min(clean_lows[-10:]) - 1.0) * 100.0
    range_60_low = min(clean_lows[-60:])
    range_60_high = max(clean_highs[-60:])
    position_60 = (
        (last_close - range_60_low) / (range_60_high - range_60_low)
        if range_60_high > range_60_low
        else 0.5
    )

    true_ranges: list[float] = []
    for index in range(size - 10, size):
        previous_close = clean_closes[index - 1]
        true_ranges.append(max(
            clean_highs[index] - clean_lows[index],
            abs(clean_highs[index] - previous_close),
            abs(clean_lows[index] - previous_close),
        ))
    atr_10_pct = (_mean_positive(true_ranges) / last_close * 100.0) if last_close > 0 else 100.0
    avg_volume_20 = _mean_positive(clean_volumes[-20:])
    avg_volume_5 = _mean_positive(clean_volumes[-5:])
    dry_up_ratio = avg_volume_5 / avg_volume_20 if avg_volume_20 > 0 else 1.0

    daily_changes = [0.0]
    for index in range(1, size):
        daily_changes.append((clean_closes[index] / clean_closes[index - 1] - 1.0) * 100.0)
    board_like_count_20 = sum(1 for value in daily_changes[-20:] if value >= 8.8)
    advance_days_15 = sum(1 for value in daily_changes[-15:] if 3.0 <= value < 8.8)

    running_high = clean_highs[-20]
    max_drawdown_20 = 0.0
    for high, low in zip(clean_highs[-20:], clean_lows[-20:]):
        running_high = max(running_high, high)
        max_drawdown_20 = max(max_drawdown_20, (running_high - low) / running_high * 100.0)

    ignition_index: int | None = None
    ignition_volume_ratio = 0.0
    for index in range(size - 15, size - 1):
        baseline = _mean_positive(clean_volumes[max(0, index - 20):index])
        current_volume_ratio = clean_volumes[index] / baseline if baseline > 0 else 0.0
        candle_range = clean_highs[index] - clean_lows[index]
        close_location = (
            (clean_closes[index] - clean_lows[index]) / candle_range
            if candle_range > 0
            else 0.5
        )
        if 3.0 <= daily_changes[index] < 8.8 and current_volume_ratio >= 1.35 and close_location >= 0.65:
            ignition_index = index
            ignition_volume_ratio = current_volume_ratio

    days_since_ignition = size - 1 - ignition_index if ignition_index is not None else 0
    pullback_from_ignition_pct = 0.0
    ignition_support = 0.0
    if ignition_index is not None:
        ignition_peak = max(clean_highs[ignition_index:])
        pullback_from_ignition_pct = (ignition_peak - last_close) / ignition_peak * 100.0
        ignition_support = min(clean_lows[ignition_index], clean_closes[ignition_index] * 0.97)
        support = max(support, ignition_support)
        support_gap_pct = (last_close / support - 1.0) * 100.0 if support > 0 else 100.0

    base_trend = (
        last_close >= ma20 * 0.97
        and ma10 >= ma20 * 0.97
        and ma20_slope_5d >= -2.0
        and pct_20 <= 35.0
    )
    compression_ready = (
        base_trend
        and range_10_pct <= 13.0
        and atr_10_pct <= 4.8
        and dry_up_ratio <= 1.10
        and -12.0 <= distance_to_resistance_pct <= 1.5
        and -3.5 <= support_gap_pct <= 8.0
    )
    pullback_ready = (
        base_trend
        and ignition_index is not None
        and 2 <= days_since_ignition <= 10
        and 1.5 <= pullback_from_ignition_pct <= 12.0
        and last_close >= support * 0.985
        and dry_up_ratio <= 1.15
        and change_pct <= 5.0
    )
    staircase_ready = (
        base_trend
        and advance_days_15 >= 2
        and range_10_pct <= 18.0
        and -4.0 <= change_pct <= 5.5
        and -8.0 <= distance_to_resistance_pct <= 2.0
    )

    tags: list[str] = []
    blockers: list[str] = []
    score = 35.0
    if base_trend:
        score += 15.0
        tags.append("MA20趋势/修复底座")
    else:
        blockers.append("MA20趋势底座未成立")
    if compression_ready:
        score += 25.0
        tags.extend(["平台缩量蓄势", f"10日振幅{range_10_pct:.1f}%"])
    if pullback_ready:
        score += 28.0
        tags.extend(["首波后缩量回踩", f"距启动{days_since_ignition}日"])
    if staircase_ready:
        score += 20.0
        tags.append("阶梯启动结构")
    if ignition_index is not None:
        score += min(8.0, max(0.0, ignition_volume_ratio - 1.0) * 5.0)
    if -5.0 <= distance_to_resistance_pct <= 1.0:
        score += 6.0
        tags.append("临近突破位")
    if 0.0 <= support_gap_pct <= 4.0:
        score += 5.0
        tags.append("贴近趋势支撑")
    if ma20 > ma60 > 0:
        score += 4.0
        tags.append("中期趋势配合")
    if position_60 <= 0.72:
        score += 4.0
        tags.append("60日中低位")

    if board_like_count_20 >= 2:
        blockers.append("近20日多次涨停，转高标事件跟踪")
    if change_pct >= 8.8:
        blockers.append("当日已涨停，不作为趋势驱动买点")
    if max_drawdown_20 > 22.0:
        blockers.append("近20日回撤过大")
    if turnover > 18.0:
        blockers.append("换手过热")

    is_ready = (
        score >= 70.0
        and (compression_ready or pullback_ready or staircase_ready)
        and board_like_count_20 < 2
        and change_pct < 8.8
        and max_drawdown_20 <= 22.0
        and turnover <= 18.0
    )
    if not is_ready and not blockers:
        blockers.append("趋势驱动确认度不足")

    if pullback_ready:
        label = "趋势驱动-首波回踩"
        setup_state = "armed_pullback"
    elif compression_ready:
        label = "趋势驱动-平台蓄势"
        setup_state = "armed_breakout"
    elif staircase_ready:
        label = "趋势驱动-阶梯启动"
        setup_state = "armed_breakout"
    else:
        label = ""
        setup_state = "candidate"

    return {
        "is_trend_driver_pattern": is_ready,
        "trend_driver_score": round(min(score, 96.0), 1),
        "trend_driver_label": label if is_ready else "",
        "trend_driver_tags": list(dict.fromkeys(tags))[:6],
        "trend_driver_blockers": blockers[:4],
        "trend_driver_stats": {
            "setup_state": setup_state,
            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "ma20_slope_5d": round(ma20_slope_5d, 2),
            "pct_20": round(pct_20, 2),
            "support": round(support, 2),
            "resistance": round(resistance, 2),
            "support_gap_pct": round(support_gap_pct, 2),
            "distance_to_resistance_pct": round(distance_to_resistance_pct, 2),
            "range_10_pct": round(range_10_pct, 2),
            "position_60": round(position_60, 3),
            "atr_10_pct": round(atr_10_pct, 2),
            "dry_up_ratio": round(dry_up_ratio, 2),
            "turnover": round(turnover, 2),
            "days_since_ignition": days_since_ignition,
            "pullback_from_ignition_pct": round(pullback_from_ignition_pct, 2),
            "advance_days_15": advance_days_15,
            "board_like_count_20": board_like_count_20,
            "max_drawdown_20": round(max_drawdown_20, 2),
        },
    }


def _build_low_expectation_trend_pattern(
    trend_driver: dict,
    *,
    change_pct: float,
    turnover: float,
    volume_ratio: float,
    news_catalyst: dict | None = None,
) -> dict:
    """从趋势驱动结果中提取低位、未加速的独立观察赛道。

    这里不降低通用趋势形态阈值，也不把研究名单硬编码进候选池。它只把已经
    通过趋势驱动校验、但会被更高原始分的长底座/二波形态挤掉的标的单独分层；
    真正买点仍要求盘中滚动60秒增量成交、VWAP和资金/板块二次确认。
    """
    stats = dict(trend_driver.get("trend_driver_stats") or {})
    if not trend_driver.get("is_trend_driver_pattern"):
        return {"is_ready": False, "source": "", "label": "", "score": 0.0, "stats": {}}

    position_60 = _safe_float(stats.get("position_60"), 1.0)
    pct_20 = _safe_float(stats.get("pct_20"))
    ma20_slope_5d = _safe_float(stats.get("ma20_slope_5d"))
    board_like_count_20 = _safe_int(stats.get("board_like_count_20"))
    max_drawdown_20 = _safe_float(stats.get("max_drawdown_20"), 100.0)
    distance_to_resistance_pct = _safe_float(
        stats.get("distance_to_resistance_pct"),
        -100.0,
    )
    price = _safe_float(stats.get("support")) * (
        1.0 + _safe_float(stats.get("support_gap_pct")) / 100.0
    )
    ma10 = _safe_float(stats.get("ma10"))
    ma20 = _safe_float(stats.get("ma20"))
    # 若收盘暂时落在MA10下方，MA10是“回收线”而不是支撑，使用仍在价格
    # 下方的MA20，避免页面给出高于现价的伪低吸位。
    support = ma20 if price > 0 and ma10 > price and ma20 > 0 else max(ma10, ma20)
    support_gap_pct = (price / support - 1.0) * 100.0 if support > 0 and price > 0 else 100.0

    is_ready = bool(
        0.0 <= position_60 <= 0.50
        and -3.0 <= pct_20 <= 18.0
        and ma20_slope_5d >= 0.0
        and board_like_count_20 == 0
        and max_drawdown_20 <= 14.0
        and -3.0 <= change_pct <= 4.5
        and 1.0 <= turnover <= 10.0
        and 0.55 <= volume_ratio <= 2.50
        and -1.5 <= support_gap_pct <= 3.5
        and -10.0 <= distance_to_resistance_pct <= 0.5
    )
    if not is_ready:
        return {"is_ready": False, "source": "", "label": "", "score": 0.0, "stats": {}}

    catalyst = dict(news_catalyst or {})
    catalyst_grade = str(catalyst.get("news_event_grade") or "")
    raw_score = _safe_float(trend_driver.get("trend_driver_score"))
    # 不沿用容易饱和到96分的形态原始分；低位、趋势坡度、支撑距离、
    # 量能和直接公告分别贡献，保证排序能真正区分“低位但没有驱动”和
    # “低位且正在形成预期差”的候选。
    quality_score = 48.0
    quality_score += min(max(0.50 - position_60, 0.0) * 16.0, 8.0)
    quality_score += min(max(ma20_slope_5d, 0.0) * 2.0, 8.0)
    quality_score += 6.0 if 0.0 <= support_gap_pct <= 2.5 else 3.0
    quality_score += 6.0 if -5.0 <= distance_to_resistance_pct <= 0.5 else 2.0
    quality_score += 7.0 if 2.0 <= pct_20 <= 12.0 else 3.0
    quality_score += 8.0 if 1.20 <= volume_ratio <= 2.20 else 3.0
    quality_score += 5.0 if 0.3 <= change_pct <= 4.5 else 1.0
    quality_score += 12.0 if catalyst_grade == "hard" else 6.0 if catalyst_grade == "medium" else 0.0
    quality_score += min(max(raw_score - 84.0, 0.0) * 0.25, 3.0)
    quality_score = min(96.0, quality_score)
    setup_state = "armed_breakout" if distance_to_resistance_pct >= -3.0 else "armed_pullback"
    return {
        "is_ready": True,
        "source": "low_expectation_trend_watch",
        "label": "低位启动-趋势临界确认",
        "score": round(quality_score, 1),
        "tags": [
            f"60日位置{position_60 * 100:.0f}%",
            f"20日涨幅{pct_20:+.1f}%",
            "未出现近期连板",
        ],
        "stats": {
            **stats,
            **catalyst,
            "setup_state": setup_state,
            "support": round(support, 2),
            "support_gap_pct": round(support_gap_pct, 2),
            "low_expectation_quality_score": round(quality_score, 1),
            "requires_rolling_60s_volume": True,
            "position_before_trigger": 0,
        },
    }


def _score_platform_breakout_kline_pattern(
    *,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    change_pct: float = 0.0,
    turnover: float = 0.0,
    volume_ratio: float = 1.0,
) -> dict:
    """平台突破主升: 横盘压缩后放量越过平台高点，适合题材蓄势后的启动/再启动。"""
    clean_closes = [float(v) for v in closes if v and v > 0]
    clean_highs = [float(v) for v in highs if v and v > 0]
    clean_lows = [float(v) for v in lows if v and v > 0]
    clean_volumes = [float(v) for v in volumes if v and v > 0]
    if len(clean_closes) < 25 or len(clean_highs) < 25 or len(clean_lows) < 25:
        return {
            "is_platform_breakout_pattern": False,
            "platform_breakout_score": 0.0,
            "platform_breakout_label": "",
            "platform_breakout_tags": [],
            "platform_breakout_blockers": ["K线样本不足"],
            "platform_breakout_stats": {},
        }

    last_close = clean_closes[-1]
    ma5 = _mean_positive(clean_closes[-5:])
    ma10 = _mean_positive(clean_closes[-10:])
    ma20 = _mean_positive(clean_closes[-20:])
    platform_high = max(clean_highs[-16:-1])
    platform_low = min(clean_lows[-16:-1])
    platform_range_pct = (platform_high - platform_low) / platform_low * 100 if platform_low > 0 else 100.0
    breakout_pct = (last_close / platform_high - 1) * 100 if platform_high > 0 else 0.0
    pct_20 = (last_close / clean_closes[-20] - 1) * 100 if clean_closes[-20] > 0 else 0.0
    pct_30 = (last_close / clean_closes[-30] - 1) * 100 if len(clean_closes) >= 30 and clean_closes[-30] > 0 else pct_20
    platform_close_avg = _mean_positive(clean_closes[-16:-1])
    platform_close_deviation = (
        max(abs(close / platform_close_avg - 1) * 100 for close in clean_closes[-16:-1])
        if platform_close_avg > 0 else 100.0
    )
    near_high_ratio = last_close / max(clean_highs[-20:]) if max(clean_highs[-20:]) > 0 else 0.0
    recent_low = min(clean_lows[-5:])
    prev_low = min(clean_lows[-12:-5])
    higher_low = prev_low > 0 and recent_low >= prev_low * 0.98
    vol_expansion = 0.0
    if len(clean_volumes) >= 25:
        base_vol = _mean_positive(clean_volumes[-16:-1])
        recent_vol = _mean_positive(clean_volumes[-3:])
        vol_expansion = recent_vol / base_vol if base_vol > 0 else 0.0
    board_like_count_12 = sum(
        1 for i in range(len(clean_closes) - 11, len(clean_closes))
        if clean_closes[i - 1] > 0 and (clean_closes[i] / clean_closes[i - 1] - 1) * 100 >= 8.8
    )

    score = 0.0
    tags: list[str] = []
    blockers: list[str] = []

    if platform_range_pct <= 18:
        score += 18.0
        tags.append(f"平台压缩{platform_range_pct:.1f}%")
    elif platform_range_pct <= 25:
        score += 10.0
        tags.append(f"平台震荡{platform_range_pct:.1f}%")
    else:
        blockers.append("平台震荡过宽")

    if platform_close_deviation <= 10:
        score += 8.0
        tags.append("平台收敛")
    elif platform_close_deviation <= 16:
        score += 4.0
        tags.append("平台温和收敛")
    else:
        blockers.append("平台收敛不足")

    if breakout_pct >= 5:
        score += 22.0
        tags.append(f"突破平台{breakout_pct:.1f}%")
    elif breakout_pct >= 2:
        score += 14.0
        tags.append(f"越过平台{breakout_pct:.1f}%")
    else:
        blockers.append("尚未有效突破平台")

    if change_pct >= 6:
        score += 12.0
        tags.append(f"当日强攻{change_pct:.1f}%")
    elif change_pct >= 3:
        score += 8.0
        tags.append(f"当日上攻{change_pct:.1f}%")
    else:
        blockers.append("突破日攻击力度不足")

    if last_close > ma5 > ma10 > 0 and last_close > ma20:
        score += 14.0
        tags.append("突破站上均线")
    elif last_close > ma10 > ma20 > 0:
        score += 9.0
        tags.append("突破站上通道")
    else:
        blockers.append("突破未站稳均线")

    if pct_20 >= 18 or pct_30 >= 25:
        score += 10.0
        tags.append("中期趋势配合")
    elif pct_20 >= 10:
        score += 6.0
        tags.append("趋势初步配合")
    else:
        blockers.append("中期趋势配合不足")

    if vol_expansion >= 1.25 or volume_ratio >= 1.2 or turnover >= 4:
        score += 10.0
        tags.append("放量突破")
    elif vol_expansion >= 1.05:
        score += 5.0
        tags.append("量能温和突破")
    else:
        blockers.append("突破量能不足")

    if near_high_ratio >= 0.96:
        score += 6.0
        tags.append("突破接近新高")

    if higher_low:
        score += 4.0
        tags.append("平台低点抬高")

    if board_like_count_12 <= 2:
        score += 3.0
        tags.append("少板突破")

    if change_pct < -3:
        blockers.append("当日走势破坏突破")

    is_ready = (
        score >= 72
        and platform_range_pct <= 25
        and breakout_pct >= 2
        and change_pct >= 3
        and last_close > ma10 > 0
        and (vol_expansion >= 1.05 or volume_ratio >= 1.1 or turnover >= 4)
        and near_high_ratio >= 0.94
    )
    if not is_ready and not blockers:
        blockers.append("平台突破确认度不足")

    return {
        "is_platform_breakout_pattern": is_ready,
        "platform_breakout_score": round(float(score), 1),
        "platform_breakout_label": "平台突破主升跟踪" if is_ready else "",
        "platform_breakout_tags": tags[:5],
        "platform_breakout_blockers": blockers[:3],
        "platform_breakout_stats": {
            "platform_range_pct": round(platform_range_pct, 2),
            "platform_close_deviation": round(platform_close_deviation, 2),
            "breakout_pct": round(breakout_pct, 2),
            "pct_20": round(pct_20, 2),
            "pct_30": round(pct_30, 2),
            "near_high_ratio": round(near_high_ratio, 3),
            "vol_expansion": round(vol_expansion, 2),
            "board_like_count_12": board_like_count_12,
            # 突破后的动态监控以平台上沿为支撑、近20日高点为再突破位；
            # 只有盘中回踩收回或放量越过压力位才会触发推送。
            "support": round(max(ma10, platform_high), 2),
            "resistance": round(max(clean_highs[-20:]), 2),
            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "ma20": round(ma20, 2),
            "setup_state": "armed_pullback",
        },
    }


def _score_probe_wash_kline_pattern(
    *,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    change_pct: float = 0.0,
    turnover: float = 0.0,
) -> dict:
    """试盘放量后缩量洗盘。

    典型序列是“4.5%~8.8%放量试盘（可连续两日）→3~14日温和回撤→
    成交量缩至试盘量的65%以内”。它只定义盘后准备态，盘中仍需支撑回收或
    压力位放量突破，避免把低位和缩量本身误当成买点。
    """
    sample_size = min(len(closes), len(highs), len(lows), len(volumes))
    if sample_size < 45:
        return {
            "is_probe_wash_pattern": False,
            "probe_wash_score": 0.0,
            "probe_wash_label": "",
            "probe_wash_tags": [],
            "probe_wash_blockers": ["试盘洗盘至少要求45根有效K线"],
            "probe_wash_stats": {},
        }

    series = [
        (float(close), float(high), float(low), float(volume))
        for close, high, low, volume in zip(
            closes[-sample_size:],
            highs[-sample_size:],
            lows[-sample_size:],
            volumes[-sample_size:],
        )
        if close and high and low and volume
        and float(close) > 0 and float(high) > 0 and float(low) > 0 and float(volume) > 0
    ]
    if len(series) < 45:
        return {
            "is_probe_wash_pattern": False,
            "probe_wash_score": 0.0,
            "probe_wash_label": "",
            "probe_wash_tags": [],
            "probe_wash_blockers": ["试盘洗盘有效K线不足"],
            "probe_wash_stats": {},
        }

    clean_closes = [item[0] for item in series]
    clean_highs = [item[1] for item in series]
    clean_lows = [item[2] for item in series]
    clean_volumes = [item[3] for item in series]
    size = len(series)
    daily_changes = [0.0] + [
        (clean_closes[index] / clean_closes[index - 1] - 1.0) * 100.0
        for index in range(1, size)
    ]
    board_like_count_20 = sum(1 for pct in daily_changes[-20:] if pct >= 8.8)
    if board_like_count_20 > 0 or change_pct >= 8.5:
        return {
            "is_probe_wash_pattern": False,
            "probe_wash_score": 0.0,
            "probe_wash_label": "",
            "probe_wash_tags": [],
            "probe_wash_blockers": ["近期已有类涨停，不再定义为启动前洗盘"],
            "probe_wash_stats": {"board_like_count_20": board_like_count_20},
        }

    high_60 = max(clean_highs[-60:])
    low_60 = min(clean_lows[-60:])
    high_120 = max(clean_highs[-120:]) if size >= 120 else max(clean_highs)
    low_120 = min(clean_lows[-120:]) if size >= 120 else min(clean_lows)
    last_close = clean_closes[-1]
    position_60 = (last_close - low_60) / (high_60 - low_60) if high_60 > low_60 else 0.5
    position_120 = (last_close - low_120) / (high_120 - low_120) if high_120 > low_120 else 0.5
    ma5 = _mean_positive(clean_closes[-5:])
    ma10 = _mean_positive(clean_closes[-10:])
    ma20 = _mean_positive(clean_closes[-20:])
    range_10_pct = (
        (max(clean_highs[-10:]) - min(clean_lows[-10:])) / min(clean_lows[-10:]) * 100.0
        if min(clean_lows[-10:]) > 0 else 100.0
    )
    high_20 = max(clean_highs[-20:])
    max_drawdown_20 = (high_20 - min(clean_lows[-20:])) / high_20 * 100.0 if high_20 > 0 else 100.0

    best: dict | None = None
    search_start = max(10, size - 18)
    for probe_start in range(search_start, size - 3):
        probe_gain_pct = daily_changes[probe_start]
        base_volume = _mean_positive(clean_volumes[probe_start - 10:probe_start])
        initial_volume_ratio = clean_volumes[probe_start] / base_volume if base_volume > 0 else 0.0
        if not (4.5 <= probe_gain_pct < 8.8 and initial_volume_ratio >= 1.25):
            continue

        probe_end = probe_start
        two_day_gain_pct = probe_gain_pct
        if probe_start + 1 < size - 2:
            next_gain_pct = daily_changes[probe_start + 1]
            cumulative_gain_pct = (
                (clean_closes[probe_start + 1] / clean_closes[probe_start - 1] - 1.0) * 100.0
            )
            if 1.8 <= next_gain_pct < 8.8 and cumulative_gain_pct >= 7.5:
                probe_end = probe_start + 1
                two_day_gain_pct = cumulative_gain_pct

        days_since_probe = size - 1 - probe_end
        if not 3 <= days_since_probe <= 14:
            continue

        probe_volume = max(clean_volumes[probe_start:probe_end + 1])
        probe_volume_ratio = probe_volume / base_volume if base_volume > 0 else 0.0
        post_lows = clean_lows[probe_end + 1:]
        if not post_lows or probe_volume_ratio < 1.55:
            continue

        probe_peak = max(clean_highs[probe_start:probe_end + 1])
        wash_low = min(post_lows)
        wash_drawdown_pct = (probe_peak - wash_low) / probe_peak * 100.0 if probe_peak > 0 else 100.0
        terminal_volume_ratio = clean_volumes[-1] / probe_volume if probe_volume > 0 else 1.0
        wash_volume_ratio = _mean_positive(clean_volumes[-3:]) / probe_volume if probe_volume > 0 else 1.0
        base_low = min(clean_lows[max(0, probe_start - 5):probe_start])
        base_held = wash_low >= base_low * 0.97 if base_low > 0 else False
        no_price_failure = last_close >= min(clean_closes[max(0, probe_start - 3):probe_start]) * 0.97
        no_premature_breakout = last_close <= probe_peak * 1.01

        resistance_start = max(probe_end + 1, size - 4)
        resistance = max(clean_highs[resistance_start:])
        support = max(
            wash_low,
            min(clean_lows[-3:]),
            min(ma10 * 0.985, resistance * 0.97),
        )
        distance_to_resistance_pct = (
            (last_close / resistance - 1.0) * 100.0 if resistance > 0 else -100.0
        )
        support_gap_pct = (last_close / support - 1.0) * 100.0 if support > 0 else 100.0

        hard_ready = (
            1.5 <= wash_drawdown_pct <= 14.0
            and terminal_volume_ratio <= 0.65
            and wash_volume_ratio <= 0.75
            and position_120 <= 0.55
            and -9.0 <= distance_to_resistance_pct <= 1.5
            and -4.0 <= change_pct <= 4.8
            and turnover <= 18.0
            and base_held
            and no_price_failure
            and no_premature_breakout
        )
        if not hard_ready:
            continue

        score = 0.0
        tags: list[str] = []
        score += 12.0 if probe_gain_pct >= 5.5 else 10.0
        tags.append(f"试盘日涨{probe_gain_pct:.1f}%")
        if probe_volume_ratio >= 2.0:
            score += 14.0
        else:
            score += 10.0
        tags.append(f"试盘量{probe_volume_ratio:.1f}倍")
        if probe_end > probe_start:
            score += 6.0
            tags.append(f"两日试盘{two_day_gain_pct:.1f}%")
        if terminal_volume_ratio <= 0.35:
            score += 15.0
        elif terminal_volume_ratio <= 0.50:
            score += 11.0
        else:
            score += 7.0
        tags.append(f"末日量缩至{terminal_volume_ratio:.0%}")
        score += 12.0 if 3.0 <= wash_drawdown_pct <= 10.0 else 8.0
        tags.append(f"洗盘回撤{wash_drawdown_pct:.1f}%")
        score += 10.0
        if position_120 <= 0.30:
            score += 10.0
            tags.append(f"120日低位{position_120:.0%}")
        else:
            score += 7.0
            tags.append(f"120日位置{position_120:.0%}")
        if -8.0 <= distance_to_resistance_pct <= 0.5:
            score += 9.0
            tags.append("接近洗盘压力位")
        score += 8.0 if 4 <= days_since_probe <= 10 else 4.0

        stats = {
            "probe_start_offset": size - 1 - probe_start,
            "probe_end_offset": days_since_probe,
            "days_since_probe": days_since_probe,
            "probe_gain_pct": round(probe_gain_pct, 2),
            "two_day_probe_gain_pct": round(two_day_gain_pct, 2),
            "probe_volume_ratio": round(probe_volume_ratio, 2),
            "terminal_volume_ratio": round(terminal_volume_ratio, 2),
            "wash_volume_ratio": round(wash_volume_ratio, 2),
            "wash_drawdown_pct": round(wash_drawdown_pct, 2),
            "position_60": round(position_60, 3),
            "position_120": round(position_120, 3),
            "pos_60": round(position_60, 3),
            "pos_120": round(position_120, 3),
            "range_10_pct": round(range_10_pct, 2),
            "max_drawdown_20": round(max_drawdown_20, 2),
            "dry_up_ratio": round(wash_volume_ratio, 2),
            "board_like_count_20": board_like_count_20,
            "support": round(support, 2),
            "resistance": round(resistance, 2),
            "support_gap_pct": round(support_gap_pct, 2),
            "distance_to_resistance_pct": round(distance_to_resistance_pct, 2),
            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "ma20": round(ma20, 2),
            "turnover": round(turnover, 2),
            "setup_state": "armed_pullback" if -1.5 <= support_gap_pct <= 3.0 else "armed_breakout",
        }
        candidate = {
            "is_probe_wash_pattern": True,
            "probe_wash_score": round(min(score, 96.0), 1),
            "probe_wash_label": "连板前兆-试盘缩量洗盘",
            "probe_wash_tags": tags[:6],
            "probe_wash_blockers": [],
            "probe_wash_stats": stats,
        }
        if best is None or candidate["probe_wash_score"] > best["probe_wash_score"]:
            best = candidate

    if best:
        return best
    return {
        "is_probe_wash_pattern": False,
        "probe_wash_score": 0.0,
        "probe_wash_label": "",
        "probe_wash_tags": [],
        "probe_wash_blockers": ["未形成放量试盘后缩量洗盘序列"],
        "probe_wash_stats": {
            "position_60": round(position_60, 3),
            "position_120": round(position_120, 3),
            "board_like_count_20": board_like_count_20,
        },
    }


def _score_momentum_shakeout_kline_pattern(
    *,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    change_pct: float = 0.0,
    turnover: float = 0.0,
) -> dict:
    """中期动量后的缩量洗盘准备态。

    该形态来自逐日首板漏选复盘：不少次日首板在前一日并不放量上涨，
    而是此前10~120日已经积累动量、近期出现过强阳/涨停记忆，随后用
    小阴或窄幅K线洗盘。这里只扩大预测和盘中检测池，不直接产生买点。
    """
    series = [
        (float(close), float(high), float(low), float(volume))
        for close, high, low, volume in zip(closes, highs, lows, volumes)
        if close and high and low and volume
        and float(close) > 0 and float(high) > 0 and float(low) > 0 and float(volume) > 0
    ]
    if len(series) < 121:
        return {
            "is_momentum_shakeout_pattern": False,
            "momentum_shakeout_score": 0.0,
            "momentum_shakeout_label": "",
            "momentum_shakeout_tags": [],
            "momentum_shakeout_blockers": ["中期动量洗盘至少要求121根有效K线"],
            "momentum_shakeout_stats": {},
        }

    clean_closes = [item[0] for item in series]
    clean_highs = [item[1] for item in series]
    clean_lows = [item[2] for item in series]
    clean_volumes = [item[3] for item in series]
    last_close = clean_closes[-1]

    def _return_pct(period: int) -> float:
        base = clean_closes[-period - 1]
        return (last_close / base - 1.0) * 100.0 if base > 0 else 0.0

    ret_10 = _return_pct(10)
    ret_20 = _return_pct(20)
    ret_60 = _return_pct(60)
    ret_120 = _return_pct(120)
    ma10 = _mean_positive(clean_closes[-10:])
    ma20 = _mean_positive(clean_closes[-20:])
    prior_high_20 = max(clean_highs[-21:-1])
    prior_low_20 = min(clean_lows[-21:-1])
    position_20 = (
        (last_close - prior_low_20) / (prior_high_20 - prior_low_20)
        if prior_high_20 > prior_low_20
        else 0.5
    )
    previous_volume_20 = _mean_positive(clean_volumes[-21:-1])
    volume_ratio_20 = clean_volumes[-1] / previous_volume_20 if previous_volume_20 > 0 else 0.0
    daily_changes = [
        (clean_closes[index] / clean_closes[index - 1] - 1.0) * 100.0
        for index in range(1, len(clean_closes))
        if clean_closes[index - 1] > 0
    ]
    recent_big_up_pct = max(daily_changes[-21:-1] or [0.0])
    latest_range_pct = (
        (clean_highs[-1] - clean_lows[-1]) / last_close * 100.0
        if last_close > 0 else 0.0
    )
    upper_gap_pct = (clean_highs[-1] - last_close) / last_close * 100.0
    lower_gap_pct = (last_close - clean_lows[-1]) / last_close * 100.0

    hard_ready = bool(
        2.0 <= ret_10 <= 38.0
        and -8.0 <= ret_20 <= 55.0
        and -25.0 <= ret_60 <= 90.0
        and -40.0 <= ret_120 <= 130.0
        and -5.5 <= change_pct <= 3.2
        and 0.35 <= volume_ratio_20 <= 1.8
        and 0.4 <= turnover <= 18.0
        and ma20 > 0
        and last_close >= ma20 * 0.96
        and 0.35 <= position_20 <= 1.05
    )
    raw_score = (
        min(max(ret_10, 0.0), 30.0) * 0.18
        + min(max(ret_20, -5.0), 40.0) * 0.06
        + min(max(ret_120, -30.0), 80.0) * 0.03
        + min(max(recent_big_up_pct - 5.0, 0.0), 6.0) * 0.8
        + min(max(turnover, 0.0), 12.0) * 0.15
        + min(max(latest_range_pct, 0.0), 10.0) * 0.10
        + min(max(upper_gap_pct, 0.0), 8.0) * 0.15
        + min(max(lower_gap_pct, 0.0), 8.0) * 0.12
        - min(abs(change_pct), 6.0) * 0.05
    )
    score = min(96.0, max(0.0, 52.0 + raw_score * 2.6))
    blockers: list[str] = []
    if not hard_ready:
        if not 2.0 <= ret_10 <= 38.0:
            blockers.append("10日动量不在启动样本区间")
        if not -5.5 <= change_pct <= 3.2:
            blockers.append("当日不是小阴/窄幅洗盘")
        if not 0.35 <= volume_ratio_20 <= 1.8:
            blockers.append("洗盘量能不健康")
        if ma20 <= 0 or last_close < ma20 * 0.96:
            blockers.append("已有效跌破20日趋势")

    support_candidates = [value for value in (ma10, ma20) if 0 < value <= last_close * 1.015]
    support = max(support_candidates) if support_candidates else min(value for value in (ma10, ma20) if value > 0)
    resistance = prior_high_20
    support_gap_pct = (last_close / support - 1.0) * 100.0 if support > 0 else 100.0
    stats = {
        "pct_10": round(ret_10, 2),
        "pct_20": round(ret_20, 2),
        "pct_60": round(ret_60, 2),
        "pct_120": round(ret_120, 2),
        "position_20": round(position_20, 3),
        "position_60": round(
            (last_close - min(clean_lows[-60:])) / max(max(clean_highs[-60:]) - min(clean_lows[-60:]), 0.01),
            3,
        ),
        "position_120": round(
            (last_close - min(clean_lows[-120:])) / max(max(clean_highs[-120:]) - min(clean_lows[-120:]), 0.01),
            3,
        ),
        "volume_ratio_20": round(volume_ratio_20, 3),
        "dry_up_ratio": round(volume_ratio_20, 3),
        "recent_big_up_pct": round(recent_big_up_pct, 2),
        "latest_range_pct": round(latest_range_pct, 2),
        "upper_gap_pct": round(upper_gap_pct, 2),
        "lower_gap_pct": round(lower_gap_pct, 2),
        "ma10": round(ma10, 3),
        "ma20": round(ma20, 3),
        "support": round(support, 3),
        "resistance": round(resistance, 3),
        "support_gap_pct": round(support_gap_pct, 2),
        "distance_to_resistance_pct": round((last_close / resistance - 1.0) * 100.0, 2) if resistance > 0 else -100.0,
        "max_drawdown_20": round((prior_high_20 - min(clean_lows[-20:])) / prior_high_20 * 100.0, 2),
        "board_like_count_20": sum(1 for value in daily_changes[-20:] if value >= 8.8),
        "turnover": round(turnover, 2),
        "setup_state": "armed_pullback" if -1.5 <= support_gap_pct <= 4.0 else "armed_breakout",
        "prediction_only": True,
    }
    if not hard_ready or score < 74.0 or resistance <= support:
        return {
            "is_momentum_shakeout_pattern": False,
            "momentum_shakeout_score": round(score, 1),
            "momentum_shakeout_label": "",
            "momentum_shakeout_tags": [],
            "momentum_shakeout_blockers": blockers[:4] or ["动量洗盘综合分不足"],
            "momentum_shakeout_stats": stats,
        }

    tags = [
        f"10日动量{ret_10:.1f}%",
        f"120日趋势{ret_120:.1f}%",
        f"洗盘量{volume_ratio_20:.2f}倍",
        f"近期强阳{recent_big_up_pct:.1f}%",
    ]
    if change_pct <= 0:
        tags.append(f"小阴洗盘{change_pct:.1f}%")
    return {
        "is_momentum_shakeout_pattern": True,
        "momentum_shakeout_score": round(score, 1),
        "momentum_shakeout_label": "连板前兆-中期动量缩量洗盘",
        "momentum_shakeout_tags": tags[:5],
        "momentum_shakeout_blockers": [],
        "momentum_shakeout_stats": stats,
    }


def _score_pre_board_ignition_kline_pattern(
    *,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    change_pct: float = 0.0,
    turnover: float = 0.0,
    volume_ratio: float = 1.0,
) -> dict:
    """连板前兆: 250日背景、120日结构和20日准备态分层识别。"""
    clean_closes = [float(v) for v in closes if v and v > 0]
    clean_highs = [float(v) for v in highs if v and v > 0]
    clean_lows = [float(v) for v in lows if v and v > 0]
    clean_volumes = [float(v) for v in volumes if v and v > 0]
    if len(clean_closes) < 25 or len(clean_highs) < 25 or len(clean_lows) < 25 or len(clean_volumes) < 25:
        return {
            "is_pre_board_ignition_pattern": False,
            "pre_board_ignition_score": 0.0,
            "pre_board_ignition_label": "",
            "pre_board_ignition_tags": [],
            "pre_board_ignition_blockers": ["K线/成交量样本不足"],
            "pre_board_ignition_stats": {},
            "pre_board_ignition_source": "",
        }

    long_cycle_profile = build_long_cycle_profile(
        closes=clean_closes,
        highs=clean_highs,
        lows=clean_lows,
        volumes=clean_volumes,
    )
    last_close = clean_closes[-1]
    ma5 = _mean_positive(clean_closes[-5:])
    ma10 = _mean_positive(clean_closes[-10:])
    ma20 = _mean_positive(clean_closes[-20:])
    ma20_prev = _mean_positive(clean_closes[-25:-5])
    platform_high = max(clean_highs[-16:-1])
    platform_low = min(clean_lows[-16:-1])
    high_20 = max(clean_highs[-20:])
    low_20 = min(clean_lows[-20:])
    high_60 = max(clean_highs[-60:]) if len(clean_highs) >= 60 else high_20
    low_60 = min(clean_lows[-60:]) if len(clean_lows) >= 60 else low_20
    has_long_sample = min(len(clean_closes), len(clean_highs), len(clean_lows)) >= 120
    high_120 = max(clean_highs[-120:]) if has_long_sample else high_60
    low_120 = min(clean_lows[-120:]) if has_long_sample else low_60
    platform_range_pct = (platform_high - platform_low) / platform_low * 100 if platform_low > 0 else 100.0
    range_20_pct = (high_20 - low_20) / low_20 * 100 if low_20 > 0 else 100.0
    pos_60 = (last_close - low_60) / (high_60 - low_60) if high_60 > low_60 else 0.5
    pos_120 = (last_close - low_120) / (high_120 - low_120) if high_120 > low_120 else 0.5
    range_120_pct = (high_120 - low_120) / low_120 * 100 if low_120 > 0 else 100.0
    range_tightening_ratio = range_20_pct / range_120_pct if range_120_pct > 0 else 1.0
    breakout_pct = (last_close / platform_high - 1) * 100 if platform_high > 0 else 0.0
    high_probe_pct = (clean_highs[-1] / platform_high - 1) * 100 if platform_high > 0 else 0.0
    pct_5 = (last_close / clean_closes[-5] - 1) * 100 if clean_closes[-5] > 0 else 0.0
    pct_10 = (last_close / clean_closes[-10] - 1) * 100 if clean_closes[-10] > 0 else 0.0
    pct_20 = (last_close / clean_closes[-20] - 1) * 100 if clean_closes[-20] > 0 else 0.0
    vol_base_20 = _mean_positive(clean_volumes[-25:-5]) if len(clean_volumes) >= 25 else 0.0
    vol_recent_3 = _mean_positive(clean_volumes[-3:])
    vol_today = clean_volumes[-1] if clean_volumes else 0.0
    vol_expansion = vol_recent_3 / vol_base_20 if vol_base_20 > 0 else 0.0
    vol_today_ratio = vol_today / vol_base_20 if vol_base_20 > 0 else 0.0
    dry_up_ratio = min(clean_volumes[-5:]) / vol_base_20 if vol_base_20 > 0 and clean_volumes[-5:] else 1.0
    up_days_5 = sum(1 for i in range(len(clean_closes) - 4, len(clean_closes)) if clean_closes[i] > clean_closes[i - 1])
    strong_probe_days_8 = sum(
        1 for i in range(len(clean_closes) - 7, len(clean_closes))
        if clean_closes[i - 1] > 0 and 3 <= (clean_closes[i] / clean_closes[i - 1] - 1) * 100 < 8.8
    )
    strong_probe_days_10 = sum(
        1 for i in range(len(clean_closes) - 9, len(clean_closes))
        if clean_closes[i - 1] > 0 and 3 <= (clean_closes[i] / clean_closes[i - 1] - 1) * 100 < 8.8
    )
    strong_probe_days_60 = sum(
        1 for i in range(max(1, len(clean_closes) - 59), len(clean_closes))
        if clean_closes[i - 1] > 0 and 3 <= (clean_closes[i] / clean_closes[i - 1] - 1) * 100 < 8.8
    )
    board_like_count_20 = sum(
        1 for i in range(len(clean_closes) - 19, len(clean_closes))
        if clean_closes[i - 1] > 0 and (clean_closes[i] / clean_closes[i - 1] - 1) * 100 >= 8.8
    )
    lower_shadow_pct = (
        (min(last_close, clean_closes[-1]) - clean_lows[-1]) / clean_lows[-1] * 100
        if clean_lows[-1] > 0 else 0.0
    )
    upper_shadow_ratio = (
        (clean_highs[-1] - last_close) / last_close * 100
        if last_close > 0 else 0.0
    )
    ma_turning = ma5 >= ma10 * 0.995 and ma10 >= ma20 * 0.985 and ma20 >= ma20_prev * 0.995
    long_base_turning = (
        last_close >= ma20 * 0.97
        and ma5 >= ma10 * 0.98
        and ma10 >= ma20 * 0.96
    )
    long_base_support = ma20
    long_base_resistance = platform_high

    pattern_stats = {
        "sample_days": min(len(clean_closes), len(clean_highs), len(clean_lows), len(clean_volumes)),
        "platform_range_pct": round(platform_range_pct, 2),
        "range_20_pct": round(range_20_pct, 2),
        "range_120_pct": round(range_120_pct, 2),
        "range_tightening_ratio": round(range_tightening_ratio, 3),
        "pos_60": round(pos_60, 3),
        "pos_120": round(pos_120, 3),
        "breakout_pct": round(breakout_pct, 2),
        "high_probe_pct": round(high_probe_pct, 2),
        "pct_5": round(pct_5, 2),
        "pct_10": round(pct_10, 2),
        "pct_20": round(pct_20, 2),
        "vol_expansion": round(vol_expansion, 2),
        "vol_today_ratio": round(vol_today_ratio, 2),
        "dry_up_ratio": round(dry_up_ratio, 2),
        "strong_probe_days_8": strong_probe_days_8,
        "strong_probe_days_10": strong_probe_days_10,
        "strong_probe_days_60": strong_probe_days_60,
        "board_like_count_20": board_like_count_20,
        "lower_shadow_pct": round(lower_shadow_pct, 2),
        "ma5": round(ma5, 2),
        "ma10": round(ma10, 2),
        "ma20": round(ma20, 2),
        "support": round(long_base_support, 2),
        "resistance": round(long_base_resistance, 2),
        "setup_state": "armed_breakout",
        **long_cycle_profile,
    }

    momentum_shakeout = _score_momentum_shakeout_kline_pattern(
        closes=clean_closes,
        highs=clean_highs,
        lows=clean_lows,
        volumes=clean_volumes,
        change_pct=change_pct,
        turnover=turnover,
    )
    if (board_like_count_20 > 0 or change_pct >= 8.5) and not momentum_shakeout.get("is_momentum_shakeout_pattern"):
        return {
            "is_pre_board_ignition_pattern": False,
            "pre_board_ignition_score": 0.0,
            "pre_board_ignition_label": "",
            "pre_board_ignition_tags": [],
            "pre_board_ignition_blockers": ["已有涨停/类涨停明牌，不归入连板前兆"],
            "pre_board_ignition_stats": pattern_stats,
            "pre_board_ignition_source": "",
        }

    probe_wash = _score_probe_wash_kline_pattern(
        closes=clean_closes,
        highs=clean_highs,
        lows=clean_lows,
        volumes=clean_volumes,
        change_pct=change_pct,
        turnover=turnover,
    )
    variants: list[tuple[float, str, str, list[str], list[str]]] = []
    if momentum_shakeout.get("is_momentum_shakeout_pattern"):
        variants.append((
            _safe_float(momentum_shakeout.get("momentum_shakeout_score")),
            "pre_board_momentum_shakeout",
            momentum_shakeout.get("momentum_shakeout_label") or "连板前兆-中期动量缩量洗盘",
            list(momentum_shakeout.get("momentum_shakeout_tags") or []),
            list(momentum_shakeout.get("momentum_shakeout_blockers") or []),
        ))
    if probe_wash.get("is_probe_wash_pattern"):
        variants.append((
            _safe_float(probe_wash.get("probe_wash_score")),
            "pre_board_probe_wash",
            probe_wash.get("probe_wash_label") or "连板前兆-试盘缩量洗盘",
            list(probe_wash.get("probe_wash_tags") or []),
            list(probe_wash.get("probe_wash_blockers") or []),
        ))

    long_base_score = 0.0
    long_base_tags: list[str] = []
    long_base_blockers: list[str] = []
    long_cycle_regime = str(long_cycle_profile.get("long_cycle_regime") or "neutral")
    long_cycle_ready = bool(long_cycle_profile.get("shape_ready"))
    long_cycle_score = _safe_float(long_cycle_profile.get("quality_setup_score"))
    if long_cycle_ready and long_cycle_regime in {
        "virgin_low_base",
        "historical_board_reset",
        "oversold_reset",
    }:
        long_base_score += 18
        long_base_tags.append(str(long_cycle_profile.get("long_cycle_regime_label") or "长周期底座"))
    elif has_long_sample and pos_120 <= 0.30:
        long_base_score += 14
        long_base_tags.append(f"120日低位{pos_120 * 100:.0f}%")
    elif has_long_sample and pos_120 <= 0.38:
        long_base_score += 10
        long_base_tags.append(f"120日中低位{pos_120 * 100:.0f}%")
    else:
        long_base_blockers.append("120日位置不够低或样本不足")
    if range_20_pct <= 28 and range_tightening_ratio <= 0.55:
        long_base_score += 12
        long_base_tags.append("长底后波动收窄")
    elif range_20_pct <= 35 and range_tightening_ratio <= 0.65:
        long_base_score += 8
        long_base_tags.append("长底后平台收敛")
    else:
        long_base_blockers.append("低位平台尚未收敛")
    if strong_probe_days_60 >= 5:
        long_base_score += min(14, 8 + strong_probe_days_60)
        long_base_tags.append(f"60日{strong_probe_days_60}次试盘")
    elif strong_probe_days_60 >= 3:
        long_base_score += 8
        long_base_tags.append(f"60日{strong_probe_days_60}次试盘")
    else:
        long_base_blockers.append("中期试盘次数不足")
    if strong_probe_days_10 >= 1:
        long_base_score += min(12, 4 + strong_probe_days_10 * 3)
        long_base_tags.append("近期再度试盘")
    else:
        long_base_blockers.append("近期没有点火动作")
    if long_base_turning:
        long_base_score += 18
        long_base_tags.append("低位均线转强")
    else:
        long_base_blockers.append("仍处下跌中继")
    if 1.05 <= vol_expansion <= 2.8 or 1.05 <= volume_ratio <= 2.8:
        long_base_score += 12
        long_base_tags.append("量能预热")
    elif dry_up_ratio <= 0.75:
        long_base_score += 8
        long_base_tags.append("缩量锁筹")
    else:
        long_base_blockers.append("量能未见沉淀或预热")
    if 1 <= turnover <= 12:
        long_base_score += 6
    elif 0.5 <= turnover <= 18:
        long_base_score += 3
    elif turnover > 20:
        long_base_blockers.append("低位换手过热")
    if 1.5 <= change_pct <= 5.5:
        long_base_score += 8
    elif -2.5 <= change_pct <= 6.5:
        long_base_score += 4
    else:
        long_base_blockers.append("当日波动不适合提前埋伏")
    if 0 <= pct_10 <= 15:
        long_base_score += 8
    elif -5 <= pct_10 <= 22:
        long_base_score += 4
    if (
        long_base_score >= 80
        and has_long_sample
        and (
            pos_120 <= 0.38
            or long_cycle_regime in {"historical_board_reset", "oversold_reset"}
        )
        and range_20_pct <= 35
        and range_tightening_ratio <= 0.65
        and strong_probe_days_60 >= 3
        and strong_probe_days_10 >= 1
        and long_base_turning
        and -2.5 <= change_pct <= 6.5
        and turnover <= 20
        and long_base_resistance > long_base_support > 0
        and long_cycle_regime != "high_overheat"
        and (long_cycle_ready or long_cycle_score >= 58.0)
    ):
        variants.append((
            long_base_score,
            "pre_board_long_base",
            f"连板前兆-{long_cycle_profile.get('long_cycle_regime_label') or '长周期蓄势'}",
            long_base_tags,
            long_base_blockers,
        ))

    compression_score = 0.0
    compression_tags: list[str] = []
    compression_blockers: list[str] = []
    if platform_range_pct <= 12 or range_20_pct <= 15:
        compression_score += 22
        compression_tags.append(f"极窄平台{min(platform_range_pct, range_20_pct):.1f}%")
    elif platform_range_pct <= 18:
        compression_score += 14
        compression_tags.append(f"压缩蓄势{platform_range_pct:.1f}%")
    else:
        compression_blockers.append("平台压缩不足")
    if dry_up_ratio <= 0.75 or vol_today_ratio <= 0.8:
        compression_score += 18
        compression_tags.append("缩量蓄势")
    else:
        compression_blockers.append("筹码未缩量沉淀")
    if ma_turning and last_close >= ma20 * 0.99:
        compression_score += 18
        compression_tags.append("均线粘合转强")
    else:
        compression_blockers.append("均线未粘合转强")
    if -2 <= change_pct <= 4.5:
        compression_score += 12
        compression_tags.append("未涨停前低吸区")
    else:
        compression_blockers.append("当日涨幅不适合低吸预兆")
    if 0.35 <= pos_60 <= 0.82:
        compression_score += 10
        compression_tags.append("位置不过高")
    elif pos_60 > 0.9:
        compression_blockers.append("60日位置过高")
    if pct_20 <= 25 and board_like_count_20 == 0:
        compression_score += 10
        compression_tags.append("无涨停明牌")
    if high_probe_pct >= -1.5:
        compression_score += 6
        compression_tags.append("贴近箱体上沿")
    if compression_score >= 74 and change_pct < 8.8 and pct_20 <= 32 and pos_60 <= 0.88:
        variants.append((compression_score, "pre_board_compression", "连板前兆-压缩蓄势", compression_tags, compression_blockers))

    probe_score = 0.0
    probe_tags: list[str] = []
    probe_blockers: list[str] = []
    if 2 <= change_pct < 8.8 or strong_probe_days_8 >= 2:
        probe_score += 18
        probe_tags.append("资金试盘")
    else:
        probe_blockers.append("未见试盘推进")
    if high_probe_pct >= 1.0 or breakout_pct >= -0.5:
        probe_score += 18
        probe_tags.append(f"试探平台{max(high_probe_pct, breakout_pct):.1f}%")
    else:
        probe_blockers.append("未贴近平台压力")
    if 1.05 <= vol_expansion <= 2.8 or 1.05 <= volume_ratio <= 2.8:
        probe_score += 16
        probe_tags.append("温和放量")
    else:
        probe_blockers.append("试盘量能不健康")
    if ma_turning or last_close > ma5 > ma10 > 0:
        probe_score += 14
        probe_tags.append("短均转多")
    else:
        probe_blockers.append("短均未转多")
    if pct_10 <= 22 and pct_20 <= 35:
        probe_score += 10
        probe_tags.append("涨幅未透支")
    else:
        probe_blockers.append("短线涨幅已透支")
    if turnover <= 15:
        probe_score += 8
        probe_tags.append("换手可接力")
    elif turnover > 22:
        probe_blockers.append("换手过热")
    if upper_shadow_ratio <= 4:
        probe_score += 6
        probe_tags.append("上影压力可控")
    if probe_score >= 74 and change_pct < 8.8 and pct_20 <= 38 and board_like_count_20 == 0:
        variants.append((probe_score, "pre_board_probe_breakout", "连板前兆-试盘突破", probe_tags, probe_blockers))

    if not variants:
        blockers = list(dict.fromkeys(
            list(momentum_shakeout.get("momentum_shakeout_blockers") or [])
            + list(probe_wash.get("probe_wash_blockers") or [])
            + long_base_blockers
            + compression_blockers
            + probe_blockers
        ))[:4]
        return {
            "is_pre_board_ignition_pattern": False,
            "pre_board_ignition_score": round(float(max(long_base_score, compression_score, probe_score)), 1),
            "pre_board_ignition_label": "",
            "pre_board_ignition_tags": [],
            "pre_board_ignition_blockers": blockers or ["连板前兆确认度不足"],
            "pre_board_ignition_stats": pattern_stats,
            "pre_board_ignition_source": "",
        }

    # “中期动量→缩量洗盘”和“试盘→缩量洗盘”都包含明确时间顺序，
    # 比静态低位/压缩更有时效；分数接近时优先保留。
    best_score, source, label, tags, blockers = max(
        variants,
        key=lambda item: (
            item[0]
            + (6.0 if item[1] == "pre_board_momentum_shakeout" else 0.0)
            + (5.0 if item[1] == "pre_board_probe_wash" else 0.0),
            2 if item[1] == "pre_board_momentum_shakeout" else 1 if item[1] == "pre_board_probe_wash" else 0,
        ),
    )
    selected_stats = (
        {
            **pattern_stats,
            **(probe_wash.get("probe_wash_stats") or {}),
        }
        if source == "pre_board_probe_wash"
        else {
            **pattern_stats,
            **(momentum_shakeout.get("momentum_shakeout_stats") or {}),
        }
        if source == "pre_board_momentum_shakeout"
        else pattern_stats
    )
    # 长周期制度画像是第一道门，但“明确试盘→缩量洗盘”本身包含更强的
    # 时间顺序信息。只对非过热、中低位且高分的特异序列开放观察池，
    # 普通中性/压缩结构仍不得进入盘中买点推送链路。
    sequence_override_ready = bool(
        long_cycle_regime == "neutral"
        and long_cycle_score >= 58.0
        and pos_120 <= 0.68
        and (
            (source == "pre_board_probe_wash" and best_score >= 82.0)
            or (source == "pre_board_probe_breakout" and best_score >= 86.0)
            or (source == "pre_board_momentum_shakeout" and best_score >= 80.0)
        )
    )
    selected_stats = {
        **selected_stats,
        "pre_board_long_cycle_ready": bool(long_cycle_ready or sequence_override_ready),
        "pre_board_long_cycle_ready_reason": (
            "long_cycle_regime"
            if long_cycle_ready
            else "ordered_probe_sequence"
            if sequence_override_ready
            else "not_ready"
        ),
    }
    return {
        "is_pre_board_ignition_pattern": True,
        "pre_board_ignition_score": round(float(best_score), 1),
        "pre_board_ignition_label": label,
        "pre_board_ignition_tags": tags[:5],
        "pre_board_ignition_blockers": blockers[:3],
        "pre_board_ignition_stats": selected_stats,
        "pre_board_ignition_source": source,
    }


def _score_high_board_ignition_kline_pattern(
    *,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    change_pct: float = 0.0,
    turnover: float = 0.0,
    volume_ratio: float = 1.0,
    trade_dates: list[date | str] | None = None,
    is_st: bool = False,
) -> dict:
    """高标连板起爆: 平台突破、缩量锁筹加速、高位换手二波三类结构。"""
    clean_closes = [float(v) for v in closes if v and v > 0]
    clean_highs = [float(v) for v in highs if v and v > 0]
    clean_lows = [float(v) for v in lows if v and v > 0]
    clean_volumes = [float(v) for v in volumes if v and v > 0]
    if len(clean_closes) < 25 or len(clean_highs) < 25 or len(clean_lows) < 25:
        return {
            "is_high_board_ignition_pattern": False,
            "high_board_ignition_score": 0.0,
            "high_board_ignition_label": "",
            "high_board_ignition_tags": [],
            "high_board_ignition_blockers": ["K线样本不足"],
            "high_board_ignition_stats": {},
            "high_board_ignition_source": "",
        }

    last_close = clean_closes[-1]
    ma5 = _mean_positive(clean_closes[-5:])
    ma10 = _mean_positive(clean_closes[-10:])
    ma20 = _mean_positive(clean_closes[-20:])
    high_20 = max(clean_highs[-20:])
    high_30 = max(clean_highs[-30:]) if len(clean_highs) >= 30 else high_20
    near_high_ratio = last_close / high_20 if high_20 > 0 else 0.0
    last_range = clean_highs[-1] - clean_lows[-1]
    close_location = (
        (last_close - clean_lows[-1]) / last_range
        if last_range > 0
        else 0.5
    )
    pct_10 = (last_close / clean_closes[-10] - 1) * 100 if clean_closes[-10] > 0 else 0.0
    pct_20 = (last_close / clean_closes[-20] - 1) * 100 if clean_closes[-20] > 0 else 0.0
    platform_high = max(clean_highs[-16:-1])
    platform_low = min(clean_lows[-16:-1])
    platform_range_pct = (platform_high - platform_low) / platform_low * 100 if platform_low > 0 else 100.0
    breakout_pct = (last_close / platform_high - 1) * 100 if platform_high > 0 else 0.0
    vol_base_20 = _mean_positive(clean_volumes[-25:-5]) if len(clean_volumes) >= 25 else 0.0
    vol_recent_3 = _mean_positive(clean_volumes[-3:])
    vol_today = clean_volumes[-1] if clean_volumes else 0.0
    vol_expansion = vol_recent_3 / vol_base_20 if vol_base_20 > 0 else 0.0
    vol_today_ratio = vol_today / vol_base_20 if vol_base_20 > 0 else 0.0
    ma_bull = ma5 > ma10 > ma20 > 0 and last_close > ma5

    aligned_dates = list(trade_dates or [])
    board_like_indexes = []
    for i in range(1, len(clean_closes)):
        if clean_closes[i - 1] <= 0:
            continue
        derived_change = (clean_closes[i] / clean_closes[i - 1] - 1) * 100
        trade_date_value = aligned_dates[i] if i < len(aligned_dates) else None
        if _is_board_like_change(derived_change, trade_date_value, is_st=is_st):
            board_like_indexes.append(i)
    recent_board_like = [i for i in board_like_indexes if i >= len(clean_closes) - 10]
    board_like_count_10 = len(recent_board_like)
    board_like_count_20 = sum(1 for i in board_like_indexes if i >= len(clean_closes) - 20)

    max_streak = 0
    current_streak = 0
    previous_idx = None
    for idx in board_like_indexes:
        if previous_idx is not None and idx == previous_idx + 1:
            current_streak += 1
        else:
            current_streak = 1
        max_streak = max(max_streak, current_streak)
        previous_idx = idx

    cluster_end = recent_board_like[-1] if recent_board_like else (board_like_indexes[-1] if board_like_indexes else -1)
    post_cluster_lows = clean_lows[cluster_end + 1:] if 0 <= cluster_end < len(clean_lows) - 1 else clean_lows[-5:]
    cluster_start = max(0, cluster_end - max(1, max_streak) + 1)
    cluster_highs = clean_highs[cluster_start: cluster_end + 1] if cluster_end >= 0 else []
    cluster_peak = max(cluster_highs) if cluster_highs else high_20
    post_low = min(post_cluster_lows) if post_cluster_lows else min(clean_lows[-5:])
    break_down_pct = (cluster_peak - post_low) / cluster_peak * 100 if cluster_peak > 0 else 0.0
    reclaim_peak_ratio = last_close / cluster_peak if cluster_peak > 0 else 0.0

    variants: list[tuple[float, str, str, list[str], list[str]]] = []

    platform_score = 0.0
    platform_tags: list[str] = []
    platform_blockers: list[str] = []
    if platform_range_pct <= 22:
        platform_score += 18
        platform_tags.append(f"平台压缩{platform_range_pct:.1f}%")
    else:
        platform_blockers.append("平台不够压缩")
    if breakout_pct >= 2:
        platform_score += 20
        platform_tags.append(f"突破平台{breakout_pct:.1f}%")
    else:
        platform_blockers.append("未突破平台")
    if change_pct >= 6:
        platform_score += 14
        platform_tags.append(f"首板强攻{change_pct:.1f}%")
    elif change_pct >= 3:
        platform_score += 8
        platform_tags.append(f"突破日上攻{change_pct:.1f}%")
    else:
        platform_blockers.append("攻击力度不足")
    if ma_bull or last_close > ma10 > ma20 > 0:
        platform_score += 14
        platform_tags.append("均线转多")
    else:
        platform_blockers.append("均线未转多")
    if vol_expansion >= 1.2 or volume_ratio >= 1.2 or turnover >= 4:
        platform_score += 14
        platform_tags.append("放量突破")
    else:
        platform_blockers.append("突破量能不足")
    if near_high_ratio >= 0.94:
        platform_score += 8
        platform_tags.append("突破近20日高位")
    if pct_20 <= 35:
        platform_score += 6
        platform_tags.append("起爆前涨幅可控")
    if platform_score >= 72 and platform_range_pct <= 25 and breakout_pct >= 2 and change_pct >= 3:
        variants.append((platform_score, "high_board_platform_breakout", "连板起爆-平台突破", platform_tags, platform_blockers))

    lock_score = 0.0
    lock_tags: list[str] = []
    lock_blockers: list[str] = []
    if ma_bull:
        lock_score += 18
        lock_tags.append("趋势多头")
    else:
        lock_blockers.append("趋势多头不足")
    latest_trade_date = aligned_dates[-1] if aligned_dates else None
    if _is_board_like_change(change_pct, latest_trade_date, is_st=is_st) or board_like_count_10 >= 1:
        lock_score += 18
        lock_tags.append("强板加速")
    else:
        lock_blockers.append("未出现强板加速")
    if turnover <= 5 or vol_today_ratio <= 0.8 or volume_ratio <= 0.8:
        lock_score += 18
        lock_tags.append("缩量锁筹")
    else:
        lock_blockers.append("未见锁筹缩量")
    if near_high_ratio >= 0.96:
        lock_score += 12
        lock_tags.append("新高附近锁仓")
    if pct_20 >= 18:
        lock_score += 10
        lock_tags.append(f"20日趋势{pct_20:.1f}%")
    if high_30 > 0 and last_close >= high_30 * 0.92:
        lock_score += 8
        lock_tags.append("高位一致")
    if lock_score >= 72 and (change_pct >= 6 or board_like_count_10 >= 1) and near_high_ratio >= 0.92:
        variants.append((lock_score, "high_board_lockup_acceleration", "连板起爆-缩量锁筹", lock_tags, lock_blockers))

    turnover_score = 0.0
    turnover_tags: list[str] = []
    turnover_blockers: list[str] = []
    if max_streak >= 2 or board_like_count_20 >= 2:
        turnover_score += 20
        turnover_tags.append(f"前高标{max_streak}连板")
    else:
        turnover_blockers.append("高标记忆不足")
    if 4 <= break_down_pct <= 28:
        turnover_score += 16
        turnover_tags.append(f"断板洗盘{break_down_pct:.1f}%")
    else:
        turnover_blockers.append("断板洗盘不清晰")
    if reclaim_peak_ratio >= 0.82:
        turnover_score += 16
        turnover_tags.append("回到前高区间")
    else:
        turnover_blockers.append("未修复到前高区")
    if turnover >= 8 or vol_expansion >= 1.1:
        turnover_score += 14
        turnover_tags.append("换手接力")
    else:
        turnover_blockers.append("换手接力不足")
    if last_close > ma10 > 0:
        turnover_score += 10
        turnover_tags.append("修复站回均线")
    if change_pct >= 2.0 and close_location >= 0.58:
        turnover_score += 10
        turnover_tags.append(f"当日主动上攻{change_pct:.1f}%")
    else:
        turnover_blockers.append("当日没有主动上攻确认")
    if (
        turnover_score >= 78
        and reclaim_peak_ratio >= 0.86
        and change_pct >= 2.0
        and close_location >= 0.58
    ):
        variants.append((turnover_score, "high_board_turnover_second_wave", "高标换手二波", turnover_tags, turnover_blockers))

    if not variants:
        _, best_variant_blockers = max(
            (
                (platform_score, platform_blockers),
                (lock_score, lock_blockers),
                (turnover_score, turnover_blockers),
            ),
            key=lambda item: item[0],
        )
        blockers = list(dict.fromkeys(best_variant_blockers))[:4]
        return {
            "is_high_board_ignition_pattern": False,
            "high_board_ignition_score": round(float(max(platform_score, lock_score, turnover_score)), 1),
            "high_board_ignition_label": "",
            "high_board_ignition_tags": [],
            "high_board_ignition_blockers": blockers or ["高标连板起爆确认度不足"],
            "high_board_ignition_stats": {
                "platform_range_pct": round(platform_range_pct, 2),
                "breakout_pct": round(breakout_pct, 2),
                "pct_10": round(pct_10, 2),
                "pct_20": round(pct_20, 2),
                "near_high_ratio": round(near_high_ratio, 3),
                "close_location": round(close_location, 3),
                "board_like_count_10": board_like_count_10,
                "board_like_count_20": board_like_count_20,
                "max_board_streak": max_streak,
                "break_down_pct": round(break_down_pct, 2),
                "reclaim_peak_ratio": round(reclaim_peak_ratio, 3),
                "vol_expansion": round(vol_expansion, 2),
                "vol_today_ratio": round(vol_today_ratio, 2),
            },
            "high_board_ignition_source": "",
        }

    best_score, source, label, tags, blockers = max(variants, key=lambda item: item[0])
    return {
        "is_high_board_ignition_pattern": True,
        "high_board_ignition_score": round(float(best_score), 1),
        "high_board_ignition_label": label,
        "high_board_ignition_tags": tags[:5],
        "high_board_ignition_blockers": blockers[:3],
        "high_board_ignition_stats": {
            "platform_range_pct": round(platform_range_pct, 2),
            "breakout_pct": round(breakout_pct, 2),
            "pct_10": round(pct_10, 2),
            "pct_20": round(pct_20, 2),
            "near_high_ratio": round(near_high_ratio, 3),
            "close_location": round(close_location, 3),
            "board_like_count_10": board_like_count_10,
            "board_like_count_20": board_like_count_20,
            "max_board_streak": max_streak,
            "break_down_pct": round(break_down_pct, 2),
            "reclaim_peak_ratio": round(reclaim_peak_ratio, 3),
            "vol_expansion": round(vol_expansion, 2),
            "vol_today_ratio": round(vol_today_ratio, 2),
        },
        "high_board_ignition_source": source,
    }


def _pick_plan_kline_pattern(
    main_wave: dict,
    second_wave: dict,
    trend_wave: dict | None = None,
    platform_wave: dict | None = None,
    high_board_wave: dict | None = None,
    pre_board_wave: dict | None = None,
    trend_driver_wave: dict | None = None,
    second_wave_reset: dict | None = None,
) -> dict:
    trend_wave = trend_wave or {}
    platform_wave = platform_wave or {}
    high_board_wave = high_board_wave or {}
    pre_board_wave = pre_board_wave or {}
    trend_driver_wave = trend_driver_wave or {}
    second_wave_reset = second_wave_reset or {}
    ready_patterns: list[tuple[float, int, dict]] = []
    source_priority = {
        "pre_board_momentum_shakeout": 95,
        "pre_board_probe_wash": 90,
        "trend_driver_setup": 80,
        "pre_board_long_base": 72,
        "pre_board_compression": 70,
        "high_board_turnover_second_wave": 65,
        "high_board_lockup_acceleration": 62,
        "platform_breakout_pattern": 55,
        "second_wave_pattern": 50,
        "second_wave_reset_watch": 48,
        "main_wave_pattern": 45,
        "pre_board_probe_breakout": 40,
        "high_board_platform_breakout": 35,
        "trend_main_wave_pattern": 20,
    }

    def _add_pattern(*, ready: bool, score: float, label: str, tags: list, stats: dict, source: str) -> None:
        if not ready:
            return
        pattern = {
            "is_ready": True,
            "score": _safe_float(score),
            "label": label,
            "tags": list(tags or []),
            "stats": stats or {},
            "source": source,
        }
        source_score = _safe_float(score)
        calibrated_score = source_score + _PLAN_PATTERN_SCORE_ADJUST.get(source, 0.0)
        ready_patterns.append((
            calibrated_score,
            source_priority.get(source, 0),
            pattern,
        ))

    _add_pattern(
        ready=bool(trend_driver_wave.get("is_trend_driver_pattern")),
        score=_safe_float(trend_driver_wave.get("trend_driver_score")),
        label=trend_driver_wave.get("trend_driver_label") or "趋势驱动候选",
        tags=list(trend_driver_wave.get("trend_driver_tags") or []),
        stats=trend_driver_wave.get("trend_driver_stats") or {},
        source="trend_driver_setup",
    )
    _add_pattern(
        ready=bool(pre_board_wave.get("is_pre_board_ignition_pattern")),
        score=_safe_float(pre_board_wave.get("pre_board_ignition_score")),
        label=pre_board_wave.get("pre_board_ignition_label") or "连板前兆",
        tags=list(pre_board_wave.get("pre_board_ignition_tags") or []),
        stats=pre_board_wave.get("pre_board_ignition_stats") or {},
        source=pre_board_wave.get("pre_board_ignition_source") or "pre_board_ignition_pattern",
    )
    _add_pattern(
        ready=bool(high_board_wave.get("is_high_board_ignition_pattern")),
        score=_safe_float(high_board_wave.get("high_board_ignition_score")),
        label=high_board_wave.get("high_board_ignition_label") or "高标连板起爆",
        tags=list(high_board_wave.get("high_board_ignition_tags") or []),
        stats=high_board_wave.get("high_board_ignition_stats") or {},
        source=high_board_wave.get("high_board_ignition_source") or "high_board_ignition_pattern",
    )
    _add_pattern(
        ready=bool(platform_wave.get("is_platform_breakout_pattern")),
        score=_safe_float(platform_wave.get("platform_breakout_score")),
        label=platform_wave.get("platform_breakout_label") or "平台突破主升跟踪",
        tags=list(platform_wave.get("platform_breakout_tags") or []),
        stats=platform_wave.get("platform_breakout_stats") or {},
        source="platform_breakout_pattern",
    )
    _add_pattern(
        ready=bool(trend_wave.get("is_trend_main_wave_pattern")),
        score=_safe_float(trend_wave.get("trend_main_wave_score")),
        label=trend_wave.get("trend_main_wave_label") or "趋势主升加速跟踪",
        tags=list(trend_wave.get("trend_main_wave_tags") or []),
        stats=trend_wave.get("trend_main_wave_stats") or {},
        source="trend_main_wave_pattern",
    )
    _add_pattern(
        ready=bool(second_wave_reset.get("is_second_wave_reset_watch")),
        score=_safe_float(second_wave_reset.get("second_wave_reset_score")),
        label=second_wave_reset.get("second_wave_reset_label") or "高标下杀二波准备",
        tags=list(second_wave_reset.get("second_wave_reset_tags") or []),
        stats=second_wave_reset.get("second_wave_reset_stats") or {},
        source="second_wave_reset_watch",
    )
    _add_pattern(
        ready=bool(second_wave.get("is_second_wave_pattern")),
        score=_safe_float(second_wave.get("second_wave_score")),
        label=second_wave.get("second_wave_label") or "断板二波主升跟踪",
        tags=list(second_wave.get("second_wave_tags") or []),
        stats=second_wave.get("second_wave_stats") or {},
        source="second_wave_pattern",
    )
    _add_pattern(
        ready=bool(main_wave.get("is_main_wave_pattern")),
        score=_safe_float(main_wave.get("main_wave_score")),
        label=main_wave.get("main_wave_label") or "主升浪形态",
        tags=list(main_wave.get("main_wave_tags") or []),
        stats=main_wave.get("main_wave_stats") or {},
        source="main_wave_pattern",
    )
    if ready_patterns:
        return max(ready_patterns, key=lambda item: (item[0], item[1]))[2]
    return {
        "is_ready": False,
        "score": max(
            _safe_float(main_wave.get("main_wave_score")),
            _safe_float(second_wave.get("second_wave_score")),
            _safe_float(trend_wave.get("trend_main_wave_score")),
            _safe_float(platform_wave.get("platform_breakout_score")),
            _safe_float(high_board_wave.get("high_board_ignition_score")),
            _safe_float(pre_board_wave.get("pre_board_ignition_score")),
            _safe_float(trend_driver_wave.get("trend_driver_score")),
            _safe_float(second_wave_reset.get("second_wave_reset_score")),
        ),
        "label": "",
        "tags": [],
        "stats": {},
        "source": "",
    }


def _merge_plan_candidates_by_code(candidates: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for item in candidates:
        code = str(item.get("code") or "")
        if not code:
            continue
        old = merged.get(code)
        if old is None:
            merged[code] = item
            continue
        high_board_types = list(dict.fromkeys(
            list(old.get("high_board_types") or [])
            + list(item.get("high_board_types") or [])
        ))
        if high_board_types:
            old["high_board_types"] = high_board_types
            old["high_board_type_labels"] = [
                _HIGH_BOARD_TYPE_LABELS[key]
                for key in high_board_types
                if key in _HIGH_BOARD_TYPE_LABELS
            ]
            primary_type = str(
                item.get("high_board_primary_type")
                or old.get("high_board_primary_type")
                or high_board_types[0]
            )
            old["high_board_primary_type"] = primary_type
            old["high_board_primary_type_label"] = _HIGH_BOARD_TYPE_LABELS.get(primary_type, "")
            old["high_board_reasons"] = list(dict.fromkeys(
                list(old.get("high_board_reasons") or [])
                + list(item.get("high_board_reasons") or [])
            ))
            old["is_high_board_candidate"] = True
        if item.get("research_only"):
            old.update({
                "is_st": bool(item.get("is_st")),
                "research_only": True,
                "is_tradeable": False,
                "tag": item.get("tag") or "⚠️ 研究池",
            })
        old_is_pattern = bool(old.get("candidate_source"))
        new_is_pattern = bool(item.get("candidate_source"))
        if new_is_pattern:
            old_pattern_score = _safe_float(old.get("main_wave_score"))
            new_pattern_score = _safe_float(item.get("main_wave_score"))
            new_source = str(item.get("candidate_source") or "")
            new_is_direct_event = new_source in {
                "event_first_board",
                "event_high_board",
                "second_board_relay",
                *set(_HIGH_BOARD_TYPE_SOURCE.values()),
            }
            if not old_is_pattern or new_pattern_score >= old_pattern_score or new_is_direct_event:
                if old_is_pattern and new_is_direct_event:
                    old["secondary_pattern"] = {
                        "candidate_source": old.get("candidate_source"),
                        "candidate_source_label": old.get("candidate_source_label", ""),
                        "main_wave_score": old.get("main_wave_score", 0),
                        "main_wave_stats": old.get("main_wave_stats", {}),
                    }
                old["candidate_source"] = item.get("candidate_source")
                old["candidate_source_label"] = item.get("candidate_source_label", "")
                old["main_wave_score"] = item.get("main_wave_score", 0)
                old["main_wave_stats"] = item.get("main_wave_stats", {})
                if new_is_direct_event and item.get("spot_fallback"):
                    old["spot_fallback"] = item.get("spot_fallback")
            if item.get("dynamic_pool_source"):
                old["dynamic_pool_source"] = item.get("dynamic_pool_source")
                old["dynamic_pool_label"] = item.get("dynamic_pool_label", "")
                old["dynamic_pool_score"] = item.get("dynamic_pool_score", 0)
                old["dynamic_pool_stats"] = item.get("dynamic_pool_stats", {})
            old["top_signals"] = list(dict.fromkeys(
                list(item.get("top_signals") or []) + list(old.get("top_signals") or [])
            ))[:8]
            old["risk_warnings"] = list(dict.fromkeys(
                list(old.get("risk_warnings") or []) + list(item.get("risk_warnings") or [])
            ))[:6]
            if _safe_float(item.get("total_score")) > _safe_float(old.get("total_score")):
                old["total_score"] = item.get("total_score")
                old["level"] = item.get("level", old.get("level"))
        elif not old_is_pattern and _safe_float(item.get("total_score")) > _safe_float(old.get("total_score")):
            merged[code] = item
    return list(merged.values())


def _enrich_plan_candidates_with_direct_catalysts(
    candidates: list[dict],
    catalyst_map: dict[str, dict],
) -> list[dict]:
    """把直接关联个股的公告并入既有技术候选，不用事后涨停结果扩池。"""
    enriched: list[dict] = []
    for raw_item in candidates:
        item = dict(raw_item)
        code = str(item.get("code") or "")
        catalyst = dict(catalyst_map.get(code) or {})
        if not catalyst:
            enriched.append(item)
            continue

        stats = {**dict(item.get("main_wave_stats") or {}), **catalyst}
        item["main_wave_stats"] = stats
        if item.get("dynamic_pool_stats"):
            item["dynamic_pool_stats"] = {
                **dict(item.get("dynamic_pool_stats") or {}),
                **catalyst,
            }
        title = str(catalyst.get("news_title") or "").strip()
        if title:
            item["top_signals"] = list(dict.fromkeys(
                [f"直接公告:{title[:54]}"] + list(item.get("top_signals") or [])
            ))[:8]
        event_score = _safe_float(catalyst.get("news_catalyst_score"))
        item["main_wave_score"] = round(
            min(96.0, max(_safe_float(item.get("main_wave_score")), event_score + 18.0)),
            1,
        )
        item["total_score"] = round(
            min(96.0, max(_safe_float(item.get("total_score")), event_score + 15.0)),
            1,
        )
        if item.get("candidate_source") == "low_expectation_trend_watch":
            grade = str(catalyst.get("news_event_grade") or "")
            label = "盘后硬利好-低位趋势确认" if grade == "hard" else "盘后事件-低位趋势确认"
            item["candidate_source_label"] = label
            item["dynamic_pool_label"] = label
            item["risk_warnings"] = list(dict.fromkeys(
                list(item.get("risk_warnings") or [])
                + ["公告只提升关注优先级，不改变零仓位等待盘中量价确认的规则"]
            ))[:6]
        enriched.append(item)
    return enriched


_REASON_TOKEN_SPLIT = re.compile(r"[+/、，,；;\-]")
_REASON_TOKEN_MIN_LEN = 2


def _reason_cohort_count_map(rows) -> dict[str, int]:
    """按**共享题材词**统计「同类涨停家数」→ {涨停原因: 家数}。

    为什么不能只比字符串相等（2026-09-17 实测，见同日提交的迁移）
    ------------------------------------------------------------
    涨停原因的粒度取决于写入源：
      * 东财：单一行业名（`通信设备`），字符串相等 ≈ 同行业；
      * 问财：`A+B+C` 题材组合（`黄金珠宝+黄金涨价+年报增长`），
        组合近乎唯一 —— 实测 2026-08-17 / 08-21 / 08-24 三天，
        「字符串相等的同类数」**恒为 1**，`>=3` 命中率 0%；
        而东财格式为 19%~60%。

    迁移到问财源后若仍按字符串相等，`same_reason_limit_up_count >= 3`
    （追板预案的「板块梯队」门槛，`has_board_cohort`）会永久失效。

    口径：两只涨停股只要有**至少一个共同题材词**即视为同类。
    东财格式下与旧口径**逐股等价** —— 单一行业名切分后只有一个 token，
    交集即字符串相等：实测 09-15、09-16 命中率 21.9% / 59.6% 均不变；
    问财格式下把 0% 恢复为 41%~55%，孤立股占比与东财格式相当
    （28%~42% vs 25%~41%）。

    计数含股票自身，与旧口径一致（同一 reason 的 N 只各自计数都是 N）。
    空原因不入表；读取方 `reason_count_map.get(reason, 0)` 语义不变。
    """
    pairs: list[tuple[str, frozenset[str]]] = []
    for _, reason in rows:
        text = str(reason or "").strip()
        if not text or text.lower() == "nan":
            continue
        tokens = frozenset(
            token
            for token in _REASON_TOKEN_SPLIT.split(text)
            if len(token) >= _REASON_TOKEN_MIN_LEN
        )
        pairs.append((text, tokens))

    counts: dict[str, int] = {}
    for text, tokens in pairs:
        if tokens:
            cohort = sum(1 for _, other in pairs if other and (tokens & other))
        else:
            # 切不出题材词（如纯单字原因）：退化为字符串相等
            cohort = sum(1 for other_text, _ in pairs if other_text == text)
        counts[text] = cohort
    return counts


async def _load_event_limit_up_plan_candidates(
    db: AsyncSession,
    target_date: date,
    catalyst_map: dict[str, dict],
) -> tuple[list[dict], dict[str, dict]]:
    """把有直接事件依据的涨停股带入预案；是否可追仍由次日确认协议判断。"""
    if not catalyst_map:
        return [], {}
    reason_count_result = await db.execute(
        select(LimitUpPool.code, LimitUpPool.limit_up_reason)
        .where(LimitUpPool.trade_date == target_date)
    )
    reason_count_map = _reason_cohort_count_map(reason_count_result.all())
    recent_kline_result = await db.execute(
        select(
            StockKline.code,
            StockKline.trade_date,
            StockKline.close,
            StockKline.change_pct,
            StockKline.open,
            StockKline.high,
            StockKline.low,
        )
        .where(
            StockKline.code.in_(list(catalyst_map)),
            StockKline.trade_date <= target_date,
            StockKline.trade_date >= target_date - timedelta(days=45),
        )
        .order_by(StockKline.code, StockKline.trade_date)
    )
    recent_klines: dict[str, list[tuple[float, float, float, float, float]]] = {}
    for code, _trade_date, close, change_pct, open_price, high, low in recent_kline_result.all():
        recent_klines.setdefault(str(code or ""), []).append(
            (
                _safe_float(close),
                _safe_float(change_pct),
                _safe_float(open_price),
                _safe_float(high),
                _safe_float(low),
            )
        )
    result = await db.execute(
        select(LimitUpPool, StockSpot)
        .outerjoin(StockSpot, StockSpot.code == LimitUpPool.code)
        .where(
            LimitUpPool.trade_date == target_date,
            LimitUpPool.code.in_(list(catalyst_map)),
        )
    )
    candidates: list[dict] = []
    limit_context_map: dict[str, dict] = {}
    for limit_up, spot in result.all():
        code = str(limit_up.code or "")
        catalyst = dict(catalyst_map.get(code) or {})
        if not catalyst:
            continue
        kline_rows = recent_klines.get(code, [])[-20:]
        latest_kline = kline_rows[-1] if kline_rows else (0.0, 0.0, 0.0, 0.0, 0.0)
        if spot is None:
            fallback_price = _safe_float(limit_up.limit_up_price) or latest_kline[0]
            fallback_change = latest_kline[1]
            fallback_prev_close = (
                fallback_price / (1.0 + fallback_change / 100.0)
                if fallback_price > 0 and fallback_change > -99
                else 0.0
            )
            spot = SimpleNamespace(
                code=code,
                name=str(limit_up.name or code),
                price=fallback_price,
                prev_close=fallback_prev_close,
                open=latest_kline[2],
                high=latest_kline[3],
                low=latest_kline[4],
                limit_up=fallback_price,
                change_pct=fallback_change,
                volume_ratio=1.0,
                turnover=_safe_float(limit_up.turnover),
                circ_market_cap=0.0,
            )
        consecutive_days = max(1, _safe_int(limit_up.consecutive_days, 1))
        is_one_word = bool(
            _safe_float(spot.limit_up) > 0
            and _safe_float(spot.open) >= _safe_float(spot.limit_up) * 0.999
            and _safe_float(spot.low) >= _safe_float(spot.limit_up) * 0.999
        )
        circ_cap_yi = _safe_float(spot.circ_market_cap)
        seal_yi = _safe_float(limit_up.seal_amount) / 1e8
        seal_to_circ_pct = seal_yi / circ_cap_yi * 100.0 if circ_cap_yi > 0 else 0.0
        event_grade = str(catalyst.get("news_event_grade") or "")
        source = "event_first_board" if consecutive_days == 1 else "event_high_board"
        closes = [row[0] for row in kline_rows if row[0] > 0]
        return_20d = (
            (closes[-1] / closes[0] - 1.0) * 100.0
            if len(closes) >= 2 and closes[0] > 0
            else 0.0
        )
        board_like_count_20 = sum(1 for _close, change, *_rest in kline_rows if change >= 8.8)
        limit_reason = str(limit_up.limit_up_reason or "")
        label = (
            "重大利好首板接力观察"
            if event_grade == "hard" and consecutive_days == 1
            else "事件首板接力观察"
            if consecutive_days == 1
            else "高标事件续强观察"
        )
        event_stats = {
            "setup_state": "armed_event_relay",
            "support": round(_safe_float(limit_up.limit_up_price or spot.price) * 0.985, 2),
            "resistance": round(_safe_float(limit_up.limit_up_price or spot.price) * 1.035, 2),
            "board_count": consecutive_days,
            "limit_up_time": str(limit_up.limit_up_time or ""),
            "break_count": _safe_int(limit_up.break_count),
            "turnover": round(_safe_float(limit_up.turnover or spot.turnover), 2),
            "is_one_word": is_one_word,
            "seal_amount_yi": round(seal_yi, 2),
            "seal_to_circ_pct": round(seal_to_circ_pct, 3),
            "limit_up_reason": limit_reason,
            "same_reason_limit_up_count": reason_count_map.get(limit_reason, 0),
            "return_20d": round(return_20d, 2),
            "board_like_count_20": board_like_count_20,
            **catalyst,
        }
        risk_warnings: list[str] = []
        if is_one_word:
            risk_warnings.append("一字涨停无正常成交机会，禁止排队追单")
        if consecutive_days >= 4:
            risk_warnings.append(f"已连续{consecutive_days}板，高位加速不按首板逻辑追涨")
        if _safe_int(limit_up.break_count) > 3:
            risk_warnings.append(f"当日炸板{_safe_int(limit_up.break_count)}次，封板分歧过大")
        candidates.append({
            "code": code,
            "name": str(limit_up.name or spot.name or code),
            "level": "A" if event_grade == "hard" else "B",
            "total_score": round(max(68.0, _safe_float(catalyst.get("news_catalyst_score"))), 2),
            "change_pct": _safe_float(spot.change_pct),
            "volume_ratio": _safe_float(spot.volume_ratio, 1.0),
            "turnover": _safe_float(limit_up.turnover or spot.turnover),
            "dimensions": {},
            "top_signals": [
                f"直接事件:{str(catalyst.get('news_title') or '')[:46]}",
                f"{consecutive_days}板/封板{str(limit_up.limit_up_time or '--')}",
            ],
            "risk_warnings": risk_warnings,
            "spot_fallback": {
                "price": _safe_float(spot.price),
                "prev_close": _safe_float(spot.prev_close),
                "open": _safe_float(spot.open),
                "high": _safe_float(spot.high),
                "low": _safe_float(spot.low),
                "limit_up": _safe_float(spot.limit_up),
                "change_pct": _safe_float(spot.change_pct),
                "volume_ratio": _safe_float(spot.volume_ratio, 1.0),
                "turnover": _safe_float(spot.turnover),
            },
            "candidate_source": source,
            "candidate_source_label": label,
            "main_wave_score": round(_safe_float(catalyst.get("news_catalyst_score")), 2),
            "main_wave_stats": event_stats,
        })
        limit_context_map[code] = event_stats
    return candidates, limit_context_map


async def _load_high_board_plan_candidates(
    db: AsyncSession,
    target_date: date,
    catalyst_map: dict[str, dict] | None = None,
    *,
    limit: int = 80,
) -> list[dict]:
    """扫描当日主板涨停梯队，并按五类高标驱动生成零仓位次日观察预案。"""
    limit_up_rows = list((await db.execute(
        select(LimitUpPool).where(LimitUpPool.trade_date == target_date)
    )).scalars().all())
    limit_up_rows = [
        row for row in limit_up_rows
        if _is_main_board_code(str(row.code or ""))
        and not str(row.name or "").strip().upper().startswith("退")
    ]
    if not limit_up_rows:
        return []

    codes = [str(row.code) for row in limit_up_rows]
    spot_map = {
        str(spot.code): spot
        for spot in (await db.execute(
            select(StockSpot).where(StockSpot.code.in_(codes))
        )).scalars().all()
    }
    tag_map = {
        str(tag.code): tag
        for tag in (await db.execute(
            select(StockTag).where(StockTag.code.in_(codes))
        )).scalars().all()
    }
    close_map = {
        str(code): (_safe_float(close), _safe_float(open_price), _safe_float(high), _safe_float(low))
        for code, close, open_price, high, low in (await db.execute(
            select(
                StockKline.code,
                StockKline.close,
                StockKline.open,
                StockKline.high,
                StockKline.low,
            ).where(
                StockKline.trade_date == target_date,
                StockKline.code.in_(codes),
            )
        )).all()
    }

    history_rows = (await db.execute(
        select(
            LimitUpPool.code,
            LimitUpPool.trade_date,
            LimitUpPool.consecutive_days,
        ).where(
            LimitUpPool.code.in_(codes),
            LimitUpPool.trade_date < target_date,
            LimitUpPool.trade_date >= target_date - timedelta(days=120),
        ).order_by(LimitUpPool.code, desc(LimitUpPool.trade_date))
    )).all()
    history_map: dict[str, dict] = {}
    for code, history_date, consecutive_days in history_rows:
        state = history_map.setdefault(str(code), {
            "previous_max_board_streak": 0,
            "previous_high_board_date": None,
        })
        streak = _safe_int(consecutive_days, 1)
        if streak > _safe_int(state.get("previous_max_board_streak")):
            state["previous_max_board_streak"] = streak
        if streak >= 3 and state.get("previous_high_board_date") is None:
            state["previous_high_board_date"] = history_date

    reason_count_map: dict[str, int] = _reason_cohort_count_map(
        (row.code, row.limit_up_reason) for row in limit_up_rows
    )

    catalysts = dict(catalyst_map or {})
    missing_catalyst_codes = set(codes) - set(catalysts)
    if missing_catalyst_codes:
        try:
            catalysts.update(await load_direct_stock_catalyst_map(
                db,
                target_date,
                candidate_codes=missing_catalyst_codes,
                limit=max(500, len(missing_catalyst_codes) * 10),
                news_end_time=datetime.now() if target_date == date.today() else None,
            ))
        except Exception as exc:
            logger.debug(f"[next_day_plan] 高标直接公告加载降级: {exc}")

    candidates: list[dict] = []
    for limit_up in limit_up_rows:
        code = str(limit_up.code or "")
        spot = spot_map.get(code)
        tag = tag_map.get(code)
        name = str(limit_up.name or getattr(spot, "name", "") or code)
        is_st = _is_st_name(name) or bool(getattr(tag, "is_st", False))
        close, open_price, high, low = close_map.get(code, (0.0, 0.0, 0.0, 0.0))
        price = _safe_float(getattr(spot, "price", 0), close or _safe_float(limit_up.limit_up_price))
        if price <= 0:
            continue
        limit_price = _safe_float(getattr(spot, "limit_up", 0), _safe_float(limit_up.limit_up_price, price))
        spot_open = _safe_float(getattr(spot, "open", 0), open_price)
        is_one_word = bool(
            limit_price > 0
            and spot_open >= limit_price * 0.999
            and _safe_float(getattr(spot, "low", 0), low or price) >= limit_price * 0.999
        )
        board_count = max(1, _safe_int(limit_up.consecutive_days, 1))
        turnover = _safe_float(limit_up.turnover, _safe_float(getattr(spot, "turnover", 0)))
        volume_ratio = _safe_float(getattr(spot, "volume_ratio", 1.0), 1.0)
        history = history_map.get(code, {})
        previous_high_date = history.get("previous_high_board_date")
        days_since_previous_high = (
            (target_date - previous_high_date).days
            if isinstance(previous_high_date, date)
            else 0
        )
        catalyst = dict(catalysts.get(code) or {})
        reason = str(limit_up.limit_up_reason or "").strip()
        stats = {
            "setup_state": "armed_high_board_research",
            "support": round(price * (0.985 if board_count <= 2 else 0.97), 3),
            "resistance": round(price * 1.035, 3),
            "board_count": board_count,
            "consecutive_days": board_count,
            "is_current_limit_up": True,
            "limit_up_time": str(limit_up.limit_up_time or ""),
            "break_count": _safe_int(limit_up.break_count),
            "turnover": round(turnover, 2),
            "is_one_word": is_one_word,
            "limit_up_reason": reason,
            "same_reason_limit_up_count": reason_count_map.get(reason, 0),
            "previous_max_board_streak": _safe_int(history.get("previous_max_board_streak")),
            "days_since_previous_high_board": days_since_previous_high,
            "position_before_trigger": 0,
            **catalyst,
        }
        base_score = min(
            96.0,
            62.0
            + min(board_count, 5) * 5.0
            + (5.0 if 2.0 <= turnover <= 18.0 and not is_one_word else 0.0)
            + (4.0 if volume_ratio >= 1.2 else 0.0)
            + min(_safe_float(catalyst.get("news_catalyst_score")) * 0.12, 8.0),
        )
        provisional = {
            "code": code,
            "name": name,
            "level": "A" if base_score >= 78 else "B",
            "total_score": round(base_score, 1),
            "change_pct": _safe_float(getattr(spot, "change_pct", 0), 10.0 if is_st else 9.9),
            "volume_ratio": volume_ratio,
            "turnover": turnover,
            "circ_market_cap_billion": _safe_float(getattr(spot, "circ_market_cap", 0)),
            "pe_ttm": _safe_float(getattr(spot, "pe_ttm", 0)),
            "net_profit_growth": _safe_float(getattr(spot, "net_profit_growth", 0)),
            "dimensions": {},
            "top_signals": [
                f"当前{board_count}板/封板{str(limit_up.limit_up_time or '--')}",
                f"涨停逻辑:{reason or '待核验'}",
            ],
            "risk_warnings": [
                "高标次日仅做竞价、换手和承接确认，确认前仓位为0",
            ],
            "spot_fallback": {
                "price": price,
                "prev_close": _safe_float(getattr(spot, "prev_close", 0)),
                "open": spot_open,
                "high": _safe_float(getattr(spot, "high", 0), high or price),
                "low": _safe_float(getattr(spot, "low", 0), low or price),
                "limit_up": limit_price,
                "change_pct": _safe_float(getattr(spot, "change_pct", 0)),
                "volume_ratio": volume_ratio,
                "turnover": turnover,
            },
            "candidate_source": "high_board_theme_turnover",
            "candidate_source_label": "高标驱动待分类",
            "main_wave_score": round(base_score, 1),
            "main_wave_stats": stats,
            "event_catalyst": catalyst,
            "is_st": is_st,
        }
        classified = _classify_high_board_candidate(provisional)
        if not classified.get("is_high_board_candidate"):
            continue
        primary_type = str(classified.get("high_board_primary_type") or "")
        classified["candidate_source"] = _HIGH_BOARD_TYPE_SOURCE[primary_type]
        classified["candidate_source_label"] = _HIGH_BOARD_TYPE_LABELS[primary_type]
        if is_st:
            classified.update({
                "research_only": True,
                "is_tradeable": False,
                "tag": "⚠️ ST研究池",
            })
            classified["risk_warnings"] = list(dict.fromkeys(
                classified["risk_warnings"]
                + ["ST仅研究展示，不进入交易、模拟盘和飞书买点推送"]
            ))
        candidates.append(classified)

    candidates.sort(
        key=lambda item: (
            _safe_int((item.get("main_wave_stats") or {}).get("board_count")),
            len(item.get("high_board_types") or []),
            _safe_float(item.get("main_wave_score")),
        ),
        reverse=True,
    )
    return candidates[:limit]


async def _load_st_high_board_watch_candidates(
    db: AsyncSession,
    target_date: date,
    *,
    limit: int = 30,
) -> list[dict]:
    """从已落库ST日K识别涨停/强攻和历史高标记忆；只生成研究观察。"""
    st_tags = list((await db.execute(
        select(StockTag).where(
            StockTag.is_st == True,
            StockTag.is_suspended == False,
        )
    )).scalars().all())
    st_tags = [
        tag for tag in st_tags
        if _is_main_board_code(str(tag.code or ""))
        and not str(tag.name or "").strip().upper().startswith("退")
    ]
    if not st_tags:
        return []
    tag_map = {str(tag.code): tag for tag in st_tags}
    codes = list(tag_map)
    rows = (await db.execute(
        select(
            StockKline.code,
            StockKline.trade_date,
            StockKline.open,
            StockKline.close,
            StockKline.high,
            StockKline.low,
            StockKline.volume,
            StockKline.turnover,
            StockKline.change_pct,
        ).where(
            StockKline.code.in_(codes),
            StockKline.trade_date <= target_date,
            StockKline.trade_date >= target_date - timedelta(days=120),
        ).order_by(StockKline.code, StockKline.trade_date)
    )).all()
    grouped: dict[str, list[tuple]] = {}
    for row in rows:
        grouped.setdefault(str(row[0]), []).append(row[1:])

    technical_rows: list[dict] = []
    current_kline_codes: set[str] = set()
    for code, kline_rows in grouped.items():
        if len(kline_rows) < 20 or kline_rows[-1][0] != target_date:
            continue
        current_kline_codes.add(code)
        latest = kline_rows[-1]
        trade_date_value, open_price, close, high, low, volume, turnover, change_pct = latest
        change_pct = _safe_float(change_pct)
        volumes = [_safe_float(row[5]) for row in kline_rows]
        volume_base = _mean_positive(volumes[-6:-1])
        volume_ratio = _safe_float(volume) / volume_base if volume_base > 0 else 1.0
        threshold = _board_like_change_threshold(trade_date_value, is_st=True)
        is_current_board = change_pct >= threshold
        board_attack_ready = bool(
            is_current_board
            or (change_pct >= threshold * 0.65 and volume_ratio >= 1.25 and _safe_float(turnover) >= 1.5)
        )
        if not board_attack_ready:
            continue

        current_streak = 0
        previous_max_streak = 0
        previous_high_date: date | None = None
        running = 0
        for row in kline_rows:
            row_date = row[0]
            if _is_board_like_change(_safe_float(row[7]), row_date, is_st=True):
                running += 1
                if row_date == target_date:
                    current_streak = running
                elif running >= 3:
                    previous_max_streak = max(previous_max_streak, running)
                    previous_high_date = row_date
            else:
                running = 0
        days_since_previous_high = (
            (target_date - previous_high_date).days if previous_high_date else 0
        )
        tag = tag_map[code]
        price = _safe_float(close)
        stats = {
            "setup_state": "armed_st_high_board_research",
            "support": round(max(_safe_float(low), price * 0.965), 3),
            "resistance": round(price * 1.035, 3),
            "board_count": current_streak,
            "is_current_limit_up": is_current_board,
            "board_attack_ready": board_attack_ready,
            "turnover": round(_safe_float(turnover), 2),
            "is_one_word": bool(
                is_current_board
                and _safe_float(open_price) >= price * 0.999
                and _safe_float(low) >= price * 0.999
            ),
            "previous_max_board_streak": previous_max_streak,
            "days_since_previous_high_board": days_since_previous_high,
            "position_before_trigger": 0,
        }
        score = min(
            90.0,
            60.0
            + (8.0 if is_current_board else 3.0)
            + min(current_streak, 3) * 4.0
            + min(previous_max_streak, 4) * 2.0
            + min(volume_ratio, 3.0) * 3.0,
        )
        technical_rows.append({
            "code": code,
            "name": str(tag.name or code),
            "level": "B",
            "total_score": round(score, 1),
            "change_pct": change_pct,
            "volume_ratio": round(volume_ratio, 2),
            "turnover": _safe_float(turnover),
            "dimensions": {},
            "top_signals": [
                f"ST制度涨停阈值{threshold:.1f}%",
                f"量比{volume_ratio:.2f}/换手{_safe_float(turnover):.1f}%",
            ],
            "risk_warnings": [
                "ST高波动研究样本；不进入交易、模拟盘和飞书买点推送",
            ],
            "spot_fallback": {
                "price": price,
                "prev_close": price / (1.0 + change_pct / 100.0) if change_pct > -99 else 0,
                "open": _safe_float(open_price),
                "high": _safe_float(high),
                "low": _safe_float(low),
                "limit_up": price if is_current_board else 0,
                "change_pct": change_pct,
                "volume_ratio": volume_ratio,
                "turnover": _safe_float(turnover),
            },
            "candidate_source": "high_board_theme_turnover",
            "candidate_source_label": "ST高标研究",
            "main_wave_score": round(score, 1),
            "main_wave_stats": stats,
            "is_st": True,
            "research_only": True,
            "is_tradeable": False,
            "tag": "⚠️ ST研究池",
        })

    # 旧版本日K调度曾排除blocked标签，导致当前ST整组没有target_date日线。
    # 兜底只读腾讯原始88字段，并严格校验字段[30]的交易日；不匹配即拒绝，
    # 因而历史回放不会偷看当前报价，也不会把网络时间当成交易日期。
    live_codes = [code for code in codes if code not in current_kline_codes]
    has_newer_kline = bool((await db.execute(
        select(func.count(StockKline.id)).where(StockKline.trade_date > target_date)
    )).scalar())
    if (
        live_codes
        and not has_newer_kline
        and 0 <= (date.today() - target_date).days <= 4
    ):
        try:
            from app.data.sources.tencent_source import TencentSource

            tencent = TencentSource()
            raw_batches = await asyncio.gather(*(
                tencent._fetch_batch(live_codes[index:index + 100])
                for index in range(0, len(live_codes), 100)
            ))
            raw_map = {
                code: fields
                for batch in raw_batches
                for code, fields in batch.items()
            }
            for code in live_codes:
                fields = raw_map.get(code) or []
                quote_time = str(fields[30] if len(fields) > 30 else "")
                if quote_time[:8] != target_date.strftime("%Y%m%d"):
                    continue
                quote = tencent._parse_spot(code, fields)
                if not quote:
                    continue
                change_pct = _safe_float(quote.get("change_pct"))
                turnover = _safe_float(quote.get("turnover"))
                volume_ratio = _safe_float(quote.get("volume_ratio"), 1.0)
                threshold = _board_like_change_threshold(target_date, is_st=True)
                is_current_board = bool(
                    _safe_float(quote.get("limit_up")) > 0
                    and _safe_float(quote.get("price")) >= _safe_float(quote.get("limit_up")) * 0.999
                    and change_pct >= threshold
                )
                board_attack_ready = bool(
                    is_current_board
                    or (
                        change_pct >= threshold * 0.65
                        and volume_ratio >= 1.25
                        and turnover >= 1.5
                    )
                )
                if not board_attack_ready:
                    continue

                history_rows = grouped.get(code, [])
                running = 0
                previous_max_streak = 0
                previous_high_date: date | None = None
                for history_row in history_rows:
                    history_date = history_row[0]
                    if _is_board_like_change(
                        _safe_float(history_row[7]),
                        history_date,
                        is_st=True,
                    ):
                        running += 1
                        if running >= 3:
                            previous_max_streak = max(previous_max_streak, running)
                            previous_high_date = history_date
                    else:
                        running = 0
                if (
                    history_rows
                    and isinstance(history_rows[-1][0], date)
                    and (target_date - history_rows[-1][0]).days > 4
                ):
                    running = 0
                current_streak = running + 1 if is_current_board else 0
                price = _safe_float(quote.get("price"))
                low = _safe_float(quote.get("low"), price)
                days_since_previous_high = (
                    (target_date - previous_high_date).days if previous_high_date else 0
                )
                stats = {
                    "setup_state": "armed_st_high_board_research",
                    "support": round(max(low, price * 0.965), 3),
                    "resistance": round(price * 1.035, 3),
                    "board_count": current_streak,
                    "is_current_limit_up": is_current_board,
                    "board_attack_ready": board_attack_ready,
                    "turnover": round(turnover, 2),
                    "is_one_word": bool(
                        is_current_board
                        and _safe_float(quote.get("open")) >= _safe_float(quote.get("limit_up")) * 0.999
                        and low >= _safe_float(quote.get("limit_up")) * 0.999
                    ),
                    "previous_max_board_streak": previous_max_streak,
                    "days_since_previous_high_board": days_since_previous_high,
                    "position_before_trigger": 0,
                    "quote_source": "tencent_live_fallback",
                    "quote_time": quote_time,
                }
                score = min(
                    90.0,
                    60.0
                    + (8.0 if is_current_board else 3.0)
                    + min(current_streak, 3) * 4.0
                    + min(previous_max_streak, 4) * 2.0
                    + min(volume_ratio, 3.0) * 3.0,
                )
                technical_rows.append({
                    "code": code,
                    "name": str(quote.get("name") or tag_map[code].name or code),
                    "level": "B",
                    "total_score": round(score, 1),
                    "change_pct": change_pct,
                    "volume_ratio": round(volume_ratio, 2),
                    "turnover": turnover,
                    "circ_market_cap_billion": _safe_float(quote.get("circ_market_cap")),
                    "pe_ttm": _safe_float(quote.get("pe_ttm")),
                    "net_profit_growth": _safe_float(quote.get("net_profit_growth")),
                    "dimensions": {},
                    "top_signals": [
                        f"ST制度涨停阈值{threshold:.1f}%",
                        f"量比{volume_ratio:.2f}/换手{turnover:.1f}%",
                        f"报价时间{quote_time}",
                    ],
                    "risk_warnings": [
                        "ST高波动研究样本；不进入交易、模拟盘和飞书买点推送",
                    ],
                    "spot_fallback": dict(quote),
                    "candidate_source": "high_board_theme_turnover",
                    "candidate_source_label": "ST高标研究",
                    "main_wave_score": round(score, 1),
                    "main_wave_stats": stats,
                    "is_st": True,
                    "research_only": True,
                    "is_tradeable": False,
                    "tag": "⚠️ ST研究池",
                })
        except Exception as exc:
            logger.warning(f"[next_day_plan] ST实时报价兜底失败，保持空研究池: {exc}")

    if not technical_rows:
        return []
    catalyst_map: dict[str, dict] = {}
    try:
        catalyst_map = await load_direct_stock_catalyst_map(
            db,
            target_date,
            candidate_codes={str(item["code"]) for item in technical_rows},
            limit=max(300, len(technical_rows) * 12),
            news_end_time=datetime.now() if target_date == date.today() else None,
        )
    except Exception as exc:
        logger.debug(f"[next_day_plan] ST研究池公告加载降级: {exc}")

    candidates: list[dict] = []
    for row in technical_rows:
        catalyst = dict(catalyst_map.get(str(row["code"])) or {})
        row["main_wave_stats"] = {**dict(row["main_wave_stats"]), **catalyst}
        row["event_catalyst"] = catalyst
        classified = _classify_high_board_candidate(row)
        if not classified.get("is_high_board_candidate"):
            continue
        primary_type = str(classified.get("high_board_primary_type") or "")
        classified["candidate_source"] = _HIGH_BOARD_TYPE_SOURCE[primary_type]
        classified["candidate_source_label"] = f"ST研究·{_HIGH_BOARD_TYPE_LABELS[primary_type]}"
        candidates.append(classified)
    candidates.sort(key=lambda item: _safe_float(item.get("main_wave_score")), reverse=True)
    return candidates[:limit]


async def _load_second_board_relay_plan_candidates(
    db: AsyncSession,
    target_date: date,
) -> tuple[list[dict], dict[str, dict]]:
    """复用“晋级预测”已经评分的首板，带入明日预案和盘中动态池。

    候选入池不等于买入；所有标的次日仓位默认为0，由异动扫描再做竞价/
    前板价/VWAP/增量成交/题材前排确认。
    """
    from app.api.v1 import promotion as promotion_api

    limit_ups = await promotion_api._load_filtered_limit_ups(db, target_date)
    market_context = await promotion_api._enrich_market_ladder_context(
        db,
        target_date,
        promotion_api._build_market_ladder_context(limit_ups),
    )
    rows = await promotion_api._build_second_board_candidates(
        db,
        target_date,
        limit_ups,
        market_context,
        120,
        include_live_dragon_tiger=False,
    )
    rows = promotion_api._rank_second_board_candidates(rows, 120)

    candidates: list[dict] = []
    context_map: dict[str, dict] = {}
    for row in rows:
        if not bool(row.get("relay_pool_ready")):
            continue
        code = str(row.get("code") or "")
        price = _safe_float(row.get("current_price"))
        if not code or price <= 0:
            continue
        quality_score = _safe_float(row.get("relay_quality_score"))
        stats = {
            "setup_state": "armed_second_board_relay",
            "support": round(price * 0.992, 3),
            "resistance": round(price * 1.035, 3),
            "board_count": 1,
            "limit_up_time": str(row.get("limit_up_time") or ""),
            "break_count": _safe_int(row.get("break_count")),
            "turnover": round(_safe_float(row.get("turnover")), 2),
            "is_one_word": bool((row.get("probability_factors") or {}).get("is_one_word_shape")),
            "limit_up_reason": str(row.get("limit_up_reason") or ""),
            "same_reason_limit_up_count": _safe_int(
                (row.get("probability_factors") or {}).get("reason_cohort_count")
            ),
            "second_board_probability": round(_safe_float(row.get("probability")), 4),
            "relay_quality_score": quality_score,
            "relay_route": str(row.get("second_board_route") or ""),
            "relay_route_label": str(row.get("second_board_route_label") or ""),
            "sector_name": str(row.get("sector_name") or ""),
            "sector_strength_score": _safe_float(row.get("sector_strength_score")),
            "relay_pool_ready": True,
            "position_before_trigger": 0,
            **{
                key: value
                for key, value in (row.get("probability_factors") or {}).items()
                if key.startswith("preboard_")
                or key in {
                    "market_risk_level",
                    "sector_morning_strengthening",
                    "main_net_inflow",
                    "main_net_inflow_pct",
                }
            },
        }
        candidates.append({
            "code": code,
            "name": str(row.get("name") or code),
            "level": "A" if quality_score >= 80 else "B",
            "total_score": quality_score,
            "change_pct": _safe_float(row.get("change_pct")),
            "volume_ratio": _safe_float(row.get("volume_ratio"), 1.0),
            "turnover": _safe_float(row.get("turnover")),
            "dimensions": {},
            "top_signals": [
                f"冲二板质量{quality_score:.0f}分",
                str(row.get("secondary_reason") or "")[:80],
            ],
            "risk_warnings": ["当前只是首板晋级池，次日二次确认前仓位为0"],
            "candidate_source": "second_board_relay",
            "candidate_source_label": "首板质量-冲二板确认",
            "main_wave_score": quality_score,
            "main_wave_stats": stats,
        })
        context_map[code] = stats
    return candidates, context_map


async def _load_leader_linkage_plan_candidates(
    db: AsyncSession,
    target_date: date,
    dragon_rows: list[dict],
) -> list[dict]:
    """把龙头A映射出的低位B加入零仓位观察池，盘中二次确认后才能推送。"""
    linked_rows: list[dict] = []
    for row in dragon_rows:
        links = list(row.get("leader_links") or [])
        if str(row.get("link_role") or "") == "follower_b" and row.get("leader_code"):
            links.insert(0, {
                "leader_code": row.get("leader_code"),
                "leader_name": row.get("leader_name"),
                "sector_code": row.get("sector_code"),
                "sector_name": row.get("sector_name"),
                "linkage_score": row.get("linkage_score"),
                "business_relevance_score": row.get("business_relevance_score"),
                "follower_shape_score": row.get("follower_shape_score"),
                "theme_alignment_score": row.get("theme_alignment_score"),
                "leader_driver_reason": row.get("leader_driver_reason"),
                "leader_industry": row.get("leader_industry"),
                "follower_industry": row.get("follower_industry"),
            })
        if not links:
            continue
        best_link = max(links, key=lambda item: _safe_float(item.get("linkage_score")))
        leader_code = str(best_link.get("leader_code") or "")
        leader_name = str(best_link.get("leader_name") or "")
        # 牛股雷达可交易池已排除ST/退市，A→B也不能借观察板块高标为B背书。
        # 否则页面虽然没显示ST本身，仍会间接生成“看*ST做B”的误导预案。
        if (
            not stock_tagger.is_tradeable(leader_code)
            or leader_name.upper().startswith(("ST", "*ST", "退"))
        ):
            continue
        linked_rows.append({**row, **best_link})
    if not linked_rows:
        return []

    linked_rows.sort(key=lambda item: _safe_float(item.get("linkage_score")), reverse=True)
    best_by_code: dict[str, dict] = {}
    for row in linked_rows:
        code = str(row.get("code") or "")
        if code and code not in best_by_code:
            best_by_code[code] = row
    codes = list(best_by_code)

    spot_result = await db.execute(select(StockSpot).where(StockSpot.code.in_(codes)))
    spot_map = {str(spot.code): spot for spot in spot_result.scalars().all()}
    kline_result = await db.execute(
        select(
            StockKline.code,
            StockKline.trade_date,
            StockKline.close,
            StockKline.high,
            StockKline.low,
        ).where(
            StockKline.code.in_(codes),
            StockKline.trade_date <= target_date,
            StockKline.trade_date >= target_date - timedelta(days=120),
        ).order_by(StockKline.code, StockKline.trade_date)
    )
    grouped: dict[str, list[tuple[float, float, float]]] = {}
    for code, _trade_date, close, high, low in kline_result.all():
        if _safe_float(close) > 0:
            grouped.setdefault(str(code), []).append(
                (_safe_float(close), _safe_float(high), _safe_float(low))
            )

    candidates: list[dict] = []
    for code, row in best_by_code.items():
        spot = spot_map.get(code)
        klines = grouped.get(code, [])
        if spot is None or len(klines) < 21:
            continue
        closes = [item[0] for item in klines]
        highs = [item[1] for item in klines if item[1] > 0]
        price = _safe_float(spot.price)
        if price <= 0:
            continue
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        support_candidates = [value for value in (ma5, ma10) if 0 < value <= price * 1.015]
        support = max(support_candidates) if support_candidates else min(ma5, ma10)
        real_resistances = sorted({value for value in highs[-60:] if value > price * 1.01})
        if support <= 0 or not real_resistances:
            continue
        resistance = real_resistances[0]
        return_20d = (price / closes[-21] - 1.0) * 100.0 if closes[-21] > 0 else 0.0
        linkage_score = _safe_float(row.get("linkage_score"))
        business_relevance_score = _safe_float(row.get("business_relevance_score"))
        follower_shape_score = _safe_float(row.get("follower_shape_score"))
        theme_alignment_score = _safe_float(row.get("theme_alignment_score"))
        if (
            return_20d > 25
            or linkage_score < 72
            or business_relevance_score < 76
            or follower_shape_score < 68
            or theme_alignment_score < 75
        ):
            continue
        stats = {
            "setup_state": "armed_leader_linkage",
            "support": round(support, 2),
            "resistance": round(resistance, 2),
            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "return_20d": round(return_20d, 2),
            "leader_code": str(row.get("leader_code") or ""),
            "leader_name": str(row.get("leader_name") or ""),
            "leader_recognition_score": _safe_float(row.get("leader_recognition_score")),
            "leader_tradability_score": _safe_float(row.get("leader_tradability_score")),
            "leader_type": str(row.get("leader_origin_type") or "sector_leader"),
            "linkage_score": round(linkage_score, 1),
            "business_relevance_score": round(business_relevance_score, 1),
            "follower_shape_score": round(follower_shape_score, 1),
            "theme_alignment_score": round(theme_alignment_score, 1),
            "leader_driver_reason": str(row.get("leader_driver_reason") or ""),
            "leader_industry": str(row.get("leader_industry") or ""),
            "follower_industry": str(row.get("follower_industry") or ""),
            "link_sector_code": str(row.get("sector_code") or ""),
            "link_sector_name": str(
                row.get("leader_driver_reason")
                or row.get("sector_name")
                or ""
            ),
            "sector_limit_up_count": _safe_int(row.get("sector_limit_up_count")),
            "requires_leader_stable": True,
            "requires_vwap_reclaim": True,
        }
        leader_name = stats["leader_name"] or stats["leader_code"]
        candidates.append({
            "code": code,
            "name": str(spot.name or row.get("name") or code),
            "level": "B",
            "total_score": round(max(62.0, linkage_score), 1),
            "change_pct": _safe_float(spot.change_pct),
            "volume_ratio": _safe_float(spot.volume_ratio, 1.0),
            "turnover": _safe_float(spot.turnover),
            "circ_market_cap_billion": _safe_float(spot.circ_market_cap),
            "pe_ttm": _safe_float(spot.pe_ttm),
            "dimensions": {},
            "top_signals": [
                f"龙头A:{leader_name}",
                f"同链路:{stats['link_sector_name'] or '--'}",
                f"联动评分{linkage_score:.0f}",
                f"主营相关{business_relevance_score:.0f}",
                f"B形态{follower_shape_score:.0f}",
                f"归因匹配{theme_alignment_score:.0f}",
            ],
            "risk_warnings": ["A未稳定或B未放量站回VWAP前仓位为0"],
            "candidate_source": "leader_linkage_follow",
            "candidate_source_label": f"看{leader_name}做B",
            "main_wave_score": round(linkage_score, 1),
            "main_wave_stats": stats,
            "dynamic_pool_source": "leader_linkage_follow",
            "dynamic_pool_label": f"看{leader_name}做B",
            "dynamic_pool_score": round(linkage_score, 1),
            "dynamic_pool_stats": stats,
        })
    return candidates[:24]


async def _load_sector_core_laggard_plan_candidates(
    db: AsyncSession,
    target_date: date,
    *,
    limit: int = 40,
) -> list[dict]:
    """从已形成扩散的强主线反向扫描未涨停成员，弥补纯个股形态池的漏选。

    单一概念映射容易把非主营跟风股混入，因此至少要求同时属于两个当日强势、
    有资金和涨停宽度确认的同花顺产业/题材板块，并且主营一级行业与这些板块
    当天已涨停成员至少有一项一致。这里只定义盘中检测池，仓位为0。
    """
    previous_date_result = await db.execute(
        select(func.max(SectorPersistence.trade_date)).where(
            SectorPersistence.trade_date < target_date
        )
    )
    previous_date = previous_date_result.scalar_one_or_none()
    previous_sector_map: dict[str, dict] = {}
    if previous_date:
        previous_result = await db.execute(
            select(
                SectorPersistence.sector_code,
                SectorPersistence.change_pct,
                SectorPersistence.fund_flow,
                SectorPersistence.strength_score,
            ).where(SectorPersistence.trade_date == previous_date)
        )
        previous_sector_map = {
            str(row[0] or ""): {
                "change_pct": _safe_float(row[1]),
                "fund_flow": _safe_float(row[2]),
                "strength_score": _safe_float(row[3]),
            }
            for row in previous_result.all()
            if row[0]
        }

    result = await db.execute(
        select(
            StockSectorMapping,
            SectorPersistence,
            SectorLifecycle,
            StockSpot,
        ).join(
            SectorPersistence,
            StockSectorMapping.sector_code == SectorPersistence.sector_code,
        ).join(
            SectorLifecycle,
            and_(
                SectorLifecycle.sector_code == SectorPersistence.sector_code,
                SectorLifecycle.trade_date == SectorPersistence.trade_date,
            ),
        ).join(
            StockSpot,
            StockSpot.code == StockSectorMapping.code,
        ).where(
            SectorPersistence.trade_date == target_date,
            StockSectorMapping.source == "pywencai",
            StockSectorMapping.sector_type.in_(("concept", "industry")),
            SectorPersistence.strength_score >= 70,
            SectorPersistence.fund_flow > 0,
            or_(
                and_(
                    SectorLifecycle.lifecycle_state.in_(tuple(_NEXT_DAY_ALLOWED_SECTOR_LIFECYCLES)),
                    SectorLifecycle.state_score >= 60,
                    SectorLifecycle.active_days >= 2,
                    SectorPersistence.change_pct >= 2.0,
                    SectorPersistence.limit_up_count >= 3,
                    SectorPersistence.consecutive_days >= 2,
                ),
                # 冰点修复日允许“当天首次形成的高宽度主线”进入零仓位检测池；
                # 仍需至少两个精确板块映射、主营同行业涨停验证和盘中量价确认。
                and_(
                    SectorPersistence.strength_score >= 82,
                    SectorPersistence.change_pct >= 3.0,
                    SectorPersistence.fund_flow >= 5.0,
                    SectorPersistence.limit_up_count >= 5,
                ),
            ),
        ).order_by(
            desc(SectorPersistence.strength_score),
            desc(SectorPersistence.limit_up_count),
            desc(SectorPersistence.fund_flow),
        )
    )

    grouped: dict[str, dict] = {}
    for mapping, persistence, lifecycle, spot in result.all():
        previous_sector = previous_sector_map.get(str(persistence.sector_code or ""), {})
        factor = {
            "sector_code": str(persistence.sector_code or ""),
            "sector_name": str(mapping.sector_name or persistence.sector_name or ""),
            "sector_type": str(mapping.sector_type or ""),
            "source": str(mapping.source or ""),
            "strength_score": _safe_float(persistence.strength_score),
            "change_pct": _safe_float(persistence.change_pct),
            "fund_flow": _safe_float(persistence.fund_flow),
            "limit_up_count": _safe_int(persistence.limit_up_count),
            "consecutive_days": _safe_int(persistence.consecutive_days),
            "lifecycle_state": str(lifecycle.lifecycle_state or ""),
            "lifecycle_score": _safe_float(lifecycle.state_score),
            "active_days": _safe_int(lifecycle.active_days),
            "previous_trade_date": str(previous_date or ""),
            "previous_change_pct": _safe_float(previous_sector.get("change_pct")),
            "previous_fund_flow": _safe_float(previous_sector.get("fund_flow")),
            "previous_strength_score": _safe_float(previous_sector.get("strength_score")),
        }
        if not _is_causal_trade_driver_sector(factor):
            continue
        code = str(mapping.code or "")
        if not code:
            continue
        bucket = grouped.setdefault(code, {"spot": spot, "factors": {}})
        bucket["factors"][factor["sector_code"]] = factor

    hot_sector_codes = {
        str(factor.get("sector_code") or "")
        for bucket in grouped.values()
        for factor in bucket["factors"].values()
        if str(factor.get("sector_code") or "")
    }
    target_limit_up_codes = set((await db.execute(
        select(LimitUpPool.code).where(LimitUpPool.trade_date == target_date)
    )).scalars().all())
    reference_codes = set(grouped) | {str(code or "") for code in target_limit_up_codes if code}
    primary_industry_map: dict[str, str] = {}
    if reference_codes:
        industry_result = await db.execute(
            select(StockSectorMapping.code, StockSectorMapping.sector_name).where(
                StockSectorMapping.code.in_(list(reference_codes)),
                StockSectorMapping.source == "pywencai",
                StockSectorMapping.sector_type == "industry",
            )
        )
        primary_industry_map = {
            str(code): str(sector_name or "").strip()
            for code, sector_name in industry_result.all()
            if code and str(sector_name or "").strip()
        }

    sector_limit_up_industry_families: dict[str, set[str]] = {}
    if hot_sector_codes and target_limit_up_codes:
        member_result = await db.execute(
            select(StockSectorMapping.sector_code, StockSectorMapping.code).where(
                StockSectorMapping.sector_code.in_(list(hot_sector_codes)),
                StockSectorMapping.code.in_(list(target_limit_up_codes)),
                StockSectorMapping.source == "pywencai",
                StockSectorMapping.sector_type.in_(("concept", "industry")),
            )
        )
        for sector_code, member_code in member_result.all():
            primary_industry = primary_industry_map.get(str(member_code or ""), "")
            industry_family = primary_industry.split("-", 1)[0].strip()
            if industry_family:
                sector_limit_up_industry_families.setdefault(
                    str(sector_code or ""), set()
                ).add(industry_family)

    candidates: list[dict] = []
    for code, bucket in grouped.items():
        spot = bucket["spot"]
        name = str(spot.name or code)
        factors = list(bucket["factors"].values())
        primary_industry = primary_industry_map.get(code, "")
        industry_family = primary_industry.split("-", 1)[0].strip()
        business_aligned_sector_count = sum(
            1
            for factor in factors
            if industry_family
            and industry_family in sector_limit_up_industry_families.get(
                str(factor.get("sector_code") or ""), set()
            )
        )
        if (
            len(factors) < 2
            or business_aligned_sector_count < 1
            or not stock_tagger.is_tradeable(code)
            or name.upper().startswith(("ST", "*ST", "退"))
        ):
            continue
        price = _safe_float(spot.price)
        limit_up = _safe_float(spot.limit_up)
        change_pct = _safe_float(spot.change_pct)
        volume_ratio = _safe_float(spot.volume_ratio, 1.0)
        turnover = _safe_float(spot.turnover)
        traded_amount = _safe_float(spot.amount)
        if (
            price <= 0
            or _safe_float(spot.volume) <= 0
            or (limit_up > 0 and price >= limit_up * 0.985)
            or not (-3.5 <= change_pct <= 4.8)
            or not (1.0 <= volume_ratio <= 5.5)
            or not (0.4 <= turnover <= 15.0)
            or traded_amount < 3e7
        ):
            continue

        factors.sort(
            key=lambda item: (
                _safe_float(item.get("strength_score")),
                _safe_int(item.get("limit_up_count")),
                _safe_float(item.get("fund_flow")),
            ),
            reverse=True,
        )
        primary = factors[0]
        avg_price = _safe_float(spot.avg_price)
        low_price = _safe_float(spot.low)
        high_price = _safe_float(spot.high)
        support_base = min(price, avg_price) if avg_price > 0 else price
        support = max(low_price, support_base * 0.995)
        resistance = max(high_price, price * 1.005)
        if support <= 0 or resistance <= support:
            continue

        membership_count = len(factors)
        breadth_climax_risk = bool(
            _safe_float(primary.get("change_pct")) >= 4.0
            and _safe_int(primary.get("limit_up_count")) >= 8
        )
        one_day_reversal_burst = bool(
            _safe_float(primary.get("previous_change_pct")) <= -1.5
            and _safe_float(primary.get("change_pct")) >= 3.0
            and _safe_int(primary.get("limit_up_count")) >= 5
        )
        score = 30.0
        score += min(_safe_float(primary.get("strength_score")), 100.0) * 0.24
        score += min(_safe_float(primary.get("lifecycle_score")), 100.0) * 0.10
        score += min(_safe_int(primary.get("limit_up_count")), 16) * 1.15
        score += min(membership_count, 5) * 4.0
        score += min(max(volume_ratio - 1.0, 0.0), 2.0) * 3.0
        score += 4.0 if -1.5 <= change_pct <= 2.5 else 1.0
        if breadth_climax_risk:
            score -= 10.0
        if one_day_reversal_burst:
            score -= 8.0
        score = round(max(55.0, min(score, 96.0)), 1)
        hot_sector_names = [str(item.get("sector_name") or "") for item in factors[:6]]
        stats = {
            "setup_state": "armed_sector_core_laggard",
            "support": round(support, 3),
            "resistance": round(resistance, 3),
            "snapshot_price": round(price, 3),
            "snapshot_change_pct": round(change_pct, 2),
            "sector_code": str(primary.get("sector_code") or ""),
            "sector_name": str(primary.get("sector_name") or ""),
            "sector_type": str(primary.get("sector_type") or ""),
            "sector_source": str(primary.get("source") or ""),
            "sector_strength_score": _safe_float(primary.get("strength_score")),
            "sector_change_pct": _safe_float(primary.get("change_pct")),
            "sector_fund_flow": _safe_float(primary.get("fund_flow")),
            "sector_limit_up_count": _safe_int(primary.get("limit_up_count")),
            "sector_consecutive_days": _safe_int(primary.get("consecutive_days")),
            "sector_lifecycle_state": str(primary.get("lifecycle_state") or ""),
            "sector_lifecycle_score": _safe_float(primary.get("lifecycle_score")),
            "sector_active_days": _safe_int(primary.get("active_days")),
            "sector_context_trade_date": target_date.isoformat(),
            "fresh_breadth_expansion": bool(
                _safe_int(primary.get("limit_up_count")) >= 5
                and _safe_float(primary.get("strength_score")) >= 82
                and _safe_float(primary.get("change_pct")) >= 3.0
            ),
            "breadth_climax_risk": breadth_climax_risk,
            "one_day_reversal_burst": one_day_reversal_burst,
            "previous_sector_trade_date": str(primary.get("previous_trade_date") or ""),
            "previous_sector_change_pct": _safe_float(primary.get("previous_change_pct")),
            "previous_sector_fund_flow": _safe_float(primary.get("previous_fund_flow")),
            "overnight_revalidation_required": True,
            "hot_sector_membership_count": membership_count,
            "hot_sector_names": hot_sector_names,
            "primary_industry": primary_industry,
            "business_aligned_sector_count": business_aligned_sector_count,
            "position_before_trigger": 0,
            "requires_rolling_60s_incremental_amount": True,
            "requires_vwap_reclaim": True,
            "requires_exact_sector_expansion": True,
        }
        candidates.append({
            "code": code,
            "name": name,
            "level": "A" if score >= 85 else "B",
            "total_score": score,
            "change_pct": change_pct,
            "volume_ratio": volume_ratio,
            "turnover": turnover,
            "circ_market_cap_billion": _safe_float(spot.circ_market_cap),
            "pe_ttm": _safe_float(spot.pe_ttm),
            "dimensions": {
                "technical": round(min(100.0, 55.0 + volume_ratio * 8.0), 1),
                "momentum": round(min(100.0, max(change_pct, 0.0) * 12.0 + 45.0), 1),
                "fund": round(min(100.0, 55.0 + max(_safe_float(primary.get("fund_flow")), 0.0) * 2.0), 1),
                "activity": round(min(100.0, turnover * 8.0), 1),
            },
            "top_signals": [
                f"主线:{primary.get('sector_name')}",
                f"板块强度{_safe_float(primary.get('strength_score')):.0f}",
                f"涨停扩散{_safe_int(primary.get('limit_up_count'))}家",
                f"多重产业映射{membership_count}项",
                f"主营同行业涨停验证{business_aligned_sector_count}项",
                f"量比{volume_ratio:.2f}",
            ],
            "risk_warnings": [
                "昨日主线补涨只入零仓位池，次日当前板块重验与60秒量价确认前不买",
                *(["单日涨停扩散过热，次日优先防兑现"] if breadth_climax_risk else []),
                *(["下跌后的单日反转脉冲，未证明可隔夜延续"] if one_day_reversal_burst else []),
                "板块扩散收缩或个股涨幅超过5.5%后才触发则取消",
            ],
            "suggestion": "观察强主线内部低位补涨，等待盘中A2二次确认",
            # Pattern history has no qualified current fund row; no spot fallback.
            "main_net_inflow_billion": None,
            "main_net_inflow_pct": None,
            "main_fund_status": "unknown",
            "volume_ratio_level": _volume_ratio_bucket(volume_ratio),
            "turnover_level": _turnover_bucket(turnover),
            "price_volume_relation": _price_volume_relation(change_pct, volume_ratio),
            "chip_signal": 0,
            "candidate_source": "sector_core_laggard",
            "candidate_source_label": "主线核心补涨-量价确认",
            "main_wave_score": score,
            "main_wave_stats": stats,
            "dynamic_pool_source": "sector_core_laggard",
            "dynamic_pool_label": "主线核心补涨-量价确认",
            "dynamic_pool_score": score,
            "dynamic_pool_stats": stats,
        })

    candidates.sort(
        key=lambda item: (
            _safe_int((item.get("main_wave_stats") or {}).get("hot_sector_membership_count")),
            _safe_float(item.get("main_wave_score")),
            _safe_float(item.get("volume_ratio")),
        ),
        reverse=True,
    )
    return candidates[:limit]


async def _refresh_intraday_sector_core_pool(
    db: AsyncSession,
    trade_date: date,
    *,
    force: bool = False,
) -> bool:
    """每分钟增量刷新强主线补涨池，避免整份购买预案的重计算开销。"""
    now_monotonic = time.monotonic()
    state_date = str(_DYNAMIC_SECTOR_CORE_REFRESH_STATE.get("trade_date") or "")
    last_refresh = _safe_float(_DYNAMIC_SECTOR_CORE_REFRESH_STATE.get("refreshed_monotonic"))
    if (
        not force
        and state_date == trade_date.isoformat()
        and now_monotonic - last_refresh < _DYNAMIC_SECTOR_CORE_REFRESH_INTERVAL_SECONDS
    ):
        return False

    try:
        fresh_candidates = await _load_sector_core_laggard_plan_candidates(
            db,
            trade_date,
            limit=48,
        )
    except Exception as exc:
        logger.warning(f"盘中强主线补涨池增量刷新失败: {exc}")
        return False

    _DYNAMIC_SECTOR_CORE_REFRESH_STATE.update({
        "trade_date": trade_date.isoformat(),
        "refreshed_monotonic": now_monotonic,
    })
    if not fresh_candidates:
        return False

    retained_candidates: list[dict] = []
    for item in anomaly_scanner.dynamic_trend_pool.values():
        if str(item.get("source") or "") == "sector_core_laggard":
            continue
        stats = dict(item.get("stats") or {})
        retained_candidates.append({
            "code": item.get("code"),
            "name": item.get("name"),
            "candidate_source": item.get("source"),
            "candidate_source_label": item.get("label"),
            "main_wave_score": item.get("score"),
            "main_wave_stats": stats,
            "support": item.get("support"),
            "resistance": item.get("resistance"),
            "setup_state": item.get("setup_state"),
        })
    merged = _select_dynamic_trend_pool_candidates(
        retained_candidates + fresh_candidates,
        limit=100,
    )
    anomaly_scanner.update_dynamic_trend_pool(merged)
    logger.info(
        f"盘中强主线补涨池增量刷新: trade_date={trade_date} "
        f"fresh={len(fresh_candidates)} total={len(merged)}"
    )
    return True


async def _load_sent_repair_plan_candidates(db: AsyncSession, target_date: date) -> list[dict]:
    """把当天真正发出成功的A2强修复信号带入购买预案和下一交易日动态池。"""
    next_date = target_date + timedelta(days=1)
    result = await db.execute(
        select(SignalPerformance, StockSpot).join(
            StockSpot,
            StockSpot.code == SignalPerformance.stock_code,
        ).where(
            SignalPerformance.signal_time >= datetime.combine(target_date, datetime.min.time()),
            SignalPerformance.signal_time < datetime.combine(next_date, datetime.min.time()),
            SignalPerformance.signal_variant.in_((
                "sector_repair_reversal",
                "old_hot_oversold_repair",
            )),
            SignalPerformance.setup_grade.like("A2%"),
        ).order_by(desc(SignalPerformance.signal_score))
    )
    rows = result.all()
    if not rows:
        return []

    codes = list(dict.fromkeys(str(signal.stock_code) for signal, _spot in rows))
    kline_result = await db.execute(
        select(StockKline).where(
            StockKline.trade_date == target_date,
            StockKline.code.in_(codes),
        )
    )
    kline_map = {item.code: item for item in kline_result.scalars().all()}

    labels = {
        "sector_repair_reversal": "产业链强修复A2",
        "old_hot_oversold_repair": "旧高标超跌修复A2",
    }
    candidates: list[dict] = []
    seen_codes: set[str] = set()
    morning_start = datetime.min.replace(hour=9, minute=30).time()
    morning_end = datetime.min.replace(hour=11, minute=30).time()
    afternoon_start = datetime.min.replace(hour=13).time()
    afternoon_end = datetime.min.replace(hour=15).time()
    for signal, spot in rows:
        code = str(signal.stock_code or "")
        if not code or code in seen_codes:
            continue
        signal_clock = signal.signal_time.time()
        in_trading_session = (
            morning_start <= signal_clock <= morning_end
            or afternoon_start <= signal_clock <= afternoon_end
        )
        if not in_trading_session:
            continue
        seen_codes.add(code)
        source = str(signal.signal_variant or "")
        signal_price = _safe_float(signal.signal_price)
        kline = kline_map.get(code)
        spot_updated_at = getattr(spot, "updated_at", None)
        spot_is_target_date = bool(
            isinstance(spot_updated_at, datetime)
            and spot_updated_at.date() == target_date
        )
        if spot_is_target_date:
            day_low = _safe_float(spot.low)
            day_high = _safe_float(spot.high)
            day_close = _safe_float(spot.price)
            day_change_pct = _safe_float(spot.change_pct)
            day_turnover = _safe_float(spot.turnover)
        else:
            day_low = _safe_float(getattr(kline, "low", 0)) or _safe_float(spot.low)
            day_high = _safe_float(getattr(kline, "high", 0)) or _safe_float(spot.high)
            day_close = _safe_float(getattr(kline, "close", 0)) or _safe_float(spot.price)
            day_change_pct = _safe_float(getattr(kline, "change_pct", None), _safe_float(spot.change_pct))
            day_turnover = _safe_float(getattr(kline, "turnover", None), _safe_float(spot.turnover))
        close_followthrough_pct = (
            (day_close / signal_price - 1.0) * 100.0
            if signal_price > 0 and day_close > 0
            else -100.0
        )
        # 收盘已经跌破信号价的A2视为当日失效，不再占用次日预案和监控池。
        if close_followthrough_pct < -1.5:
            continue
        support = max(day_low, signal_price * 0.985) if signal_price > 0 else day_low
        resistance = (
            max(signal_price * 1.02, support * 1.02)
            if spot_is_target_date and signal_price > 0
            else max(day_high, support * 1.02)
        )
        raw_score = max(82.0, min(96.0, _safe_float(signal.signal_score)))
        score = min(96.0, raw_score + max(-2.0, min(2.0, close_followthrough_pct)))
        candidates.append({
            "code": code,
            "name": spot.name or code,
            "total_score": score,
            "level": "A",
            "change_pct": round(day_change_pct, 2),
            "dimensions": {
                "technical": score,
                "momentum": min(100.0, score + 2.0),
                "fund": score,
                "activity": min(100.0, _safe_float(spot.turnover) * 5.0),
            },
            "top_signals": [
                labels.get(source, "盘中A2异动"),
                str(signal.top_factor or "盘口与板块确认"),
                "异动推送成功并已进入绩效追踪",
            ],
            "risk_warnings": ["首日强修复波动较大，次日必须等待回踩或突破二次确认"],
            "suggestion": "盘中A2信号次日跟踪，不按收盘价追高",
            "circ_market_cap_billion": round(_safe_float(spot.circ_market_cap), 1),
            "pe_ttm": spot.pe_ttm,
            "turnover": day_turnover,
            "volume_ratio": _safe_float(spot.volume_ratio, 1.0),
            # Pattern history has no qualified current fund row; no spot fallback.
            "main_net_inflow_billion": None,
            "main_net_inflow_pct": None,
            "main_fund_status": "unknown",
            "candidate_source": source,
            "candidate_source_label": labels.get(source, "盘中A2异动"),
            "main_wave_score": score,
            "main_wave_stats": {
                "setup_state": "armed_pullback",
                "support": round(support, 2),
                "resistance": round(resistance, 2),
                "signal_price": round(signal_price, 3),
                "signal_time": signal.signal_time.isoformat(),
                "sent_signal": True,
                "close_followthrough_pct": round(close_followthrough_pct, 2),
                "current_amplitude": round(
                    _safe_float(spot.amplitude)
                    if spot_is_target_date
                    else (
                        (day_high - day_low) / _safe_float(getattr(kline, "prev_close", 0)) * 100.0
                        if _safe_float(getattr(kline, "prev_close", 0)) > 0
                        else 0.0
                    ),
                    2,
                ),
                "current_support_strength": round(
                    _safe_float(spot.support_strength_score) if spot_is_target_date else 0.0,
                    1,
                ),
                "current_orderbook_imbalance": round(
                    _safe_float(spot.orderbook_imbalance) if spot_is_target_date else 0.0,
                    4,
                ),
            },
            "dynamic_pool_source": source,
            "dynamic_pool_label": labels.get(source, "盘中A2异动"),
            "dynamic_pool_score": score,
            "dynamic_pool_stats": {
                "setup_state": "armed_pullback",
                "support": round(support, 2),
                "resistance": round(resistance, 2),
                "signal_price": round(signal_price, 3),
                "sent_signal": True,
                "close_followthrough_pct": round(close_followthrough_pct, 2),
                "current_amplitude": round(_safe_float(spot.amplitude), 2) if spot_is_target_date else 0.0,
            },
            "_carry_rank": round(score + max(-2.0, min(5.0, close_followthrough_pct)), 3),
        })
    candidates.sort(
        key=lambda item: (
            _safe_float(item.get("_carry_rank")),
            _safe_float(item.get("main_wave_score")),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )
    for item in candidates:
        item.pop("_carry_rank", None)
    # 预案仅保留收盘确认最强的少量A2；全量信号仍在绩效表中用于结算。
    return candidates[:20]


async def _load_tenbagger_pullback_plan_candidates(
    db: AsyncSession,
    target_date: date,
    exclude_codes: set[str] | None = None,
    *,
    limit: int = 12,
) -> list[dict]:
    """十倍潜力与健康趋势的交集池；潜力分只入池，盘中回踩确认后才可推送。"""
    exclude_codes = exclude_codes or set()
    rank_payload = await _tenbagger_rank(db)
    ranked = [
        item
        for item in (rank_payload.get("rank") or [])
        if str(item.get("code") or "") not in exclude_codes
        and str(item.get("level") or "") in {"T", "A"}
        and _safe_float(item.get("total_score")) >= 72.0
        and 15.0 <= _safe_float(item.get("circ_market_cap_billion")) <= 300.0
        and 20.0 <= _safe_float(item.get("net_profit_growth")) <= 250.0
        and 0 < _safe_float(item.get("pe_ttm")) <= 100.0
        and _safe_float(item.get("change_pct")) <= 5.5
    ]
    if not ranked:
        return []

    rank_map = {str(item.get("code") or ""): item for item in ranked}
    codes = list(rank_map)
    date_result = await db.execute(
        select(StockKline.trade_date)
        .where(StockKline.trade_date <= target_date)
        .group_by(StockKline.trade_date)
        .order_by(desc(StockKline.trade_date))
        .limit(120)
    )
    recent_dates = [row[0] for row in date_result.all()]
    if len(recent_dates) < 100:
        return []
    earliest_date = min(recent_dates)
    spot_result = await db.execute(
        select(StockSpot).where(StockSpot.code.in_(codes), StockSpot.price > 0)
    )
    spot_map = {spot.code: spot for spot in spot_result.scalars().all()}
    kline_result = await db.execute(
        select(
            StockKline.code,
            StockKline.trade_date,
            StockKline.close,
            StockKline.high,
            StockKline.low,
            StockKline.volume,
        ).where(
            StockKline.code.in_(codes),
            StockKline.trade_date >= earliest_date,
            StockKline.trade_date <= target_date,
        ).order_by(StockKline.code, desc(StockKline.trade_date))
    )
    grouped: dict[str, list[tuple[date, float, float, float, float]]] = {}
    for code, trade_day, close, high, low, volume in kline_result.all():
        rows = grouped.setdefault(str(code), [])
        if len(rows) < 120:
            rows.append((trade_day, close, high, low, volume))

    candidates: list[dict] = []
    for code, rank_item in rank_map.items():
        spot = spot_map.get(code)
        rows = list(reversed(grouped.get(code) or []))
        if spot is None or len(rows) < 100:
            continue
        closes = [_safe_float(row[1]) for row in rows]
        highs = [_safe_float(row[2]) for row in rows]
        lows = [_safe_float(row[3]) for row in rows]
        if min(closes[-60:]) <= 0 or min(lows[-60:]) <= 0:
            continue
        close = closes[-1]
        ma5 = sum(closes[-5:]) / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        ma20_old = sum(closes[-25:-5]) / 20
        ma20_slope_5d = (ma20 / ma20_old - 1.0) * 100.0 if ma20_old > 0 else -100.0
        return_20d = (close / closes[-20] - 1.0) * 100.0
        return_60d = (close / closes[-60] - 1.0) * 100.0
        low_120d = min(lows[-120:])
        high_120d = max(highs[-120:])
        high_60d = max(highs[-60:])
        high_20d = max(highs[-20:])
        position_120 = (
            (close - low_120d) / (high_120d - low_120d)
            if high_120d > low_120d
            else 1.0
        )
        near_high_60 = close / high_60d if high_60d > 0 else 0.0
        range_20d = (high_20d / min(lows[-20:]) - 1.0) * 100.0
        if not (
            3.0 <= return_20d <= 28.0
            and -25.0 <= return_60d <= 45.0
            and ma20_slope_5d >= 0.50
            and close >= ma20 * 0.985
            and 0.10 <= position_120 <= 0.72
            and near_high_60 >= 0.70
            and range_20d <= 35.0
        ):
            continue

        support_candidates = [
            value for value in (ma5, ma10, ma20, min(lows[-5:]))
            if 0 < value <= close * 1.002
        ]
        support = max(support_candidates, default=ma20)
        resistance = high_20d
        if support <= 0 or resistance <= support * 1.05:
            continue
        tenbagger_score = _safe_float(rank_item.get("total_score"))
        potential_quality = min(
            96.0,
            tenbagger_score * 0.60
            + min(return_20d, 20.0) * 0.50
            + min(ma20_slope_5d, 5.0) * 2.0
            + (5.0 if close >= ma10 else 0.0)
            + (5.0 if position_120 <= 0.55 else 0.0),
        )
        stats = {
            "setup_state": "armed_pullback",
            "support": round(support, 3),
            "resistance": round(resistance, 3),
            "ma5": round(ma5, 3),
            "ma10": round(ma10, 3),
            "ma20": round(ma20, 3),
            "ma60": round(ma60, 3),
            "tenbagger_score": round(tenbagger_score, 1),
            "potential_quality_score": round(potential_quality, 1),
            "pct_20": round(return_20d, 2),
            "pct_60": round(return_60d, 2),
            "ma20_slope_5d": round(ma20_slope_5d, 2),
            "position_120": round(position_120, 3),
            "near_high_ratio": round(near_high_60, 3),
            "range_20d": round(range_20d, 2),
            "high_20d": round(high_20d, 3),
        }
        candidates.append({
            **rank_item,
            "candidate_source": "tenbagger_pullback_watch",
            "candidate_source_label": "牛股潜质-回踩等待",
            "main_wave_score": round(max(72.0, potential_quality), 1),
            "main_wave_stats": stats,
            "dynamic_pool_source": "tenbagger_pullback_watch",
            "dynamic_pool_label": "牛股潜质-回踩等待",
            "dynamic_pool_score": round(max(72.0, potential_quality), 1),
            "dynamic_pool_stats": stats,
            "risk_warnings": list(dict.fromkeys(
                list(rank_item.get("risk_warnings") or [])
                + ["潜力评分不等于买点；仅在支撑回收且60秒增量成交确认后提醒"]
            )),
            "suggestion": "高潜力与健康趋势交集，盘中只等回踩确认，不追红盘加速",
        })

    candidates.sort(
        key=lambda item: (
            _safe_float((item.get("main_wave_stats") or {}).get("potential_quality_score")),
            _safe_float((item.get("main_wave_stats") or {}).get("tenbagger_score")),
        ),
        reverse=True,
    )
    return candidates[:limit]


async def _load_main_wave_plan_candidates(
    db: AsyncSession,
    target_date: date,
    exclude_codes: set[str] | None = None,
    *,
    limit: int = 80,
) -> list[dict]:
    """全市场130日初筛，入选候选再用260日画像复核，兼顾形态完整性与页面性能。"""
    exclude_codes = exclude_codes or set()
    date_result = await db.execute(
        select(StockKline.trade_date)
        .where(StockKline.trade_date <= target_date)
        .group_by(StockKline.trade_date)
        .order_by(desc(StockKline.trade_date))
        .limit(260)
    )
    recent_dates = [row[0] for row in date_result.all()]
    if len(recent_dates) < 60:
        return []

    # target_date 在盘中通常是“今天”，但正式日 K 可能仍只到上一交易日。
    # 后续形态、涨停记忆和资金窗口必须锚定同一根最新可见日 K；否则会用
    # 今日竞价字段给昨日形态打分，并且错过腾讯昨收对昨日尾盘快照的校正。
    kline_target_date = recent_dates[0]
    earliest_date = min(recent_dates)
    # 中期动量洗盘需要120个完整收益间隔（至少121根K线）；先对全市场
    # 做130日初筛，
    # 再只为最终候选补齐260日画像，避免页面冷启动读取并排序约百万根K线。
    screening_dates = recent_dates[:130]
    screening_earliest_date = min(screening_dates)
    spot_result = await db.execute(
        # 盘前竞价大多数股票成交量仍为0，但昨晚正式日K与形态仍然有效。
        # 用实时volume做历史候选存在性条件，会把全市场缩成极少数竞价成交股。
        select(StockSpot).where(StockSpot.price > 0)
    )
    spot_map = {spot.code: spot for spot in spot_result.scalars().all()}
    if not spot_map:
        return []

    # 只读取最近直接关联个股的公告用于同形态内排序；公告不能绕过下方
    # K线、位置、涨幅和风险闸门，也不直接生成候选。
    direct_catalyst_map: dict[str, dict] = {}
    try:
        direct_catalyst_map = await load_direct_stock_catalyst_map(
            db,
            target_date,
            limit=1500,
            news_end_time=datetime.now() if target_date == date.today() else None,
        )
    except Exception as exc:
        logger.debug(f"[next_day_plan] 低位趋势公告排序降级: {exc}")

    lu_result = await db.execute(
        select(LimitUpPool).where(LimitUpPool.trade_date == kline_target_date)
    )
    limit_up_map = {lu.code: lu for lu in lu_result.scalars().all()}

    fund_window = await load_main_fund_window(
        db, through_date=kline_target_date, decision_at=datetime.now(), codes=spot_map,
    )
    fund_5d_map = {code: item["total"] for code, item in fund_window["items"].items()}

    kline_result = await db.execute(
        select(
            StockKline.code,
            StockKline.trade_date,
            StockKline.open,
            StockKline.close,
            StockKline.high,
            StockKline.low,
            StockKline.volume,
            StockKline.turnover,
            StockKline.change_pct,
        ).where(
            StockKline.trade_date >= screening_earliest_date,
            StockKline.trade_date <= target_date,
        ).order_by(StockKline.code, StockKline.trade_date)
    )

    from collections import defaultdict
    grouped: dict[str, list[tuple[float, float, float, float, float, float, float, date]]] = defaultdict(list)
    for code, _trade_date, open_price, close, high, low, volume, turnover, row_change_pct in kline_result.all():
        if code not in spot_map:
            continue
        grouped[code].append((
            _safe_float(open_price),
            _safe_float(close),
            _safe_float(high),
            _safe_float(low),
            _safe_float(volume),
            _safe_float(turnover),
            _safe_float(row_change_pct),
            _trade_date,
        ))

    candidates: list[dict] = []
    for code, rows in grouped.items():
        # 60根可识别短结构；120/250日字段会按实际样本降级，绝不补造历史。
        if code in exclude_codes or len(rows) < 60:
            continue
        spot = spot_map.get(code)
        if not spot:
            continue
        opens = [row[0] for row in rows]
        closes = [row[1] for row in rows]
        highs = [row[2] for row in rows]
        lows = [row[3] for row in rows]
        volumes = [row[4] for row in rows]
        row_trade_dates = [row[7] for row in rows]
        target_turnover = _safe_float(rows[-1][5])
        kline_close_reconciled = False
        spot_snapshot_date = (
            spot.updated_at.date()
            if getattr(spot, "updated_at", None)
            else None
        )
        # 下一交易日盘前，腾讯昨收已经是交易所确认的上一日收盘。若其与
        # 日K尾值不一致，说明15:10 CDN/盘中fallback快照尚未最终化；评分时
        # 以可见的昨收纠正尾值，避免把错误收盘继续喂给学习与次日预案。
        if (
            spot_snapshot_date
            and spot_snapshot_date > kline_target_date
            and (spot_snapshot_date - kline_target_date).days <= 4
            and _safe_float(spot.prev_close) > 0
            and closes[-1] > 0
            and abs(_safe_float(spot.prev_close) / closes[-1] - 1.0) >= 0.001
        ):
            closes[-1] = _safe_float(spot.prev_close)
            highs[-1] = max(highs[-1], closes[-1])
            lows[-1] = min(lows[-1], closes[-1])
            kline_close_reconciled = True
        target_change_pct = (
            (closes[-1] / closes[-2] - 1.0) * 100.0
            if len(closes) >= 2 and closes[-2] > 0
            else _safe_float(rows[-1][6])
        )
        previous_volume_5 = _mean_positive(volumes[-6:-1])
        target_volume_ratio = (
            volumes[-1] / previous_volume_5
            if previous_volume_5 > 0
            else 1.0
        )
        main_wave = _score_main_wave_kline_pattern(
            opens=opens,
            closes=closes,
            highs=highs,
            lows=lows,
            volumes=volumes,
            change_pct=target_change_pct,
            turnover=target_turnover,
            volume_ratio=target_volume_ratio,
        )
        second_wave = _score_second_wave_kline_pattern(
            closes=closes,
            highs=highs,
            lows=lows,
            volumes=volumes,
            change_pct=target_change_pct,
            turnover=target_turnover,
            volume_ratio=target_volume_ratio,
        )
        second_wave_reset = _score_second_wave_reset_watch_pattern(
            closes=closes,
            highs=highs,
            lows=lows,
            volumes=volumes,
            change_pct=target_change_pct,
        )
        trend_wave = _score_trend_main_wave_kline_pattern(
            opens=opens,
            closes=closes,
            highs=highs,
            lows=lows,
            volumes=volumes,
            change_pct=target_change_pct,
            turnover=target_turnover,
            volume_ratio=target_volume_ratio,
        )
        trend_driver_wave = _score_trend_driver_kline_pattern(
            closes=closes,
            highs=highs,
            lows=lows,
            volumes=volumes,
            change_pct=target_change_pct,
            turnover=target_turnover,
            volume_ratio=target_volume_ratio,
        )
        low_expectation_wave = _build_low_expectation_trend_pattern(
            trend_driver_wave,
            change_pct=target_change_pct,
            turnover=target_turnover,
            volume_ratio=target_volume_ratio,
            news_catalyst=direct_catalyst_map.get(code),
        )
        platform_wave = _score_platform_breakout_kline_pattern(
            closes=closes,
            highs=highs,
            lows=lows,
            volumes=volumes,
            change_pct=target_change_pct,
            turnover=target_turnover,
            volume_ratio=target_volume_ratio,
        )
        if len(rows) >= 60:
            pre_board_wave = _score_pre_board_ignition_kline_pattern(
                closes=closes,
                highs=highs,
                lows=lows,
                volumes=volumes,
                change_pct=target_change_pct,
                turnover=target_turnover,
                volume_ratio=target_volume_ratio,
            )
        else:
            pre_board_wave = {
                "is_pre_board_ignition_pattern": False,
                "pre_board_ignition_score": 0.0,
                "pre_board_ignition_label": "",
                "pre_board_ignition_tags": [],
                "pre_board_ignition_blockers": ["连板前兆要求至少60日位置样本"],
                "pre_board_ignition_stats": {},
                "pre_board_ignition_source": "",
            }
        high_board_wave = _score_high_board_ignition_kline_pattern(
            closes=closes,
            highs=highs,
            lows=lows,
            volumes=volumes,
            change_pct=target_change_pct,
            turnover=target_turnover,
            volume_ratio=target_volume_ratio,
            trade_dates=row_trade_dates,
            is_st=_is_st_name(str(spot.name or "")),
        )
        pattern = _pick_plan_kline_pattern(
            main_wave,
            second_wave,
            trend_wave,
            platform_wave,
            high_board_wave,
            pre_board_wave,
            trend_driver_wave,
            second_wave_reset,
        )
        standalone_pullback_source: dict = {}
        for wave, score_key, stats_key in (
            (trend_wave, "trend_main_wave_score", "trend_main_wave_stats"),
            (main_wave, "main_wave_score", "main_wave_stats"),
        ):
            wave_stats = dict(wave.get(stats_key) or {})
            quality_tier = str(wave_stats.get("pullback_quality_tier") or "")
            if not wave_stats.get("pullback_shape_ready") or quality_tier not in {
                "strict",
                "reclaim",
            }:
                continue
            wave_score = _safe_float(wave.get(score_key))
            pct_20 = _safe_float(wave_stats.get("pct_20"))
            near_high_ratio = max(
                _safe_float(wave_stats.get("near_high_ratio")),
                _safe_float(wave_stats.get("pullback_near_high_ratio")),
            )
            if quality_tier == "reclaim" and not (
                wave_score >= 68.0
                and pct_20 >= 25.0
                and near_high_ratio >= 0.86
            ):
                continue
            shape_support = _safe_float(wave_stats.get("pullback_anchor_low"))
            shape_reclaim = _safe_float(wave_stats.get("pullback_reclaim_price"))
            shape_resistance = max(
                _safe_float(wave_stats.get("resistance")),
                shape_reclaim,
            )
            if shape_support <= 0 or shape_resistance <= shape_support:
                continue
            shape_stats = {
                **wave_stats,
                "setup_state": "armed_main_wave_shape_pullback",
                "support": round(shape_support, 3),
                "resistance": round(shape_resistance, 3),
            }
            standalone_pullback_source = {
                "is_ready": True,
                "source": "main_wave_pullback_pattern",
                "label": wave_stats.get("pullback_shape_label") or "主升浪回踩",
                "score": max(78.0, wave_score + 4.0),
                "tags": [wave_stats.get("pullback_quality_label") or "主升回踩确认"],
                "stats": shape_stats,
            }
            break
        # 深洗首阴会让原主升评分暂时失效，但这正是次日支撑回收检测所需的
        # 形态记忆。仅将 strict/reclaim 两档独立送入观察池，不生成直接买点。
        if not pattern.get("is_ready") and standalone_pullback_source:
            pattern = dict(standalone_pullback_source)
        # 低位趋势临界是独立的“预期差等待”赛道。它已通过趋势驱动硬闸门，
        # 应覆盖更宽泛的长底座展示标签，避免被大量历史高分形态挤出Top100。
        if low_expectation_wave.get("is_ready") and not standalone_pullback_source:
            pattern = low_expectation_wave
        if not pattern.get("is_ready"):
            continue

        lu = limit_up_map.get(code)
        is_limit_up = bool(lu)
        pattern_source = str(pattern.get("source") or "")
        if pattern_source.startswith("pre_board_") and (is_limit_up or lu):
            continue
        if pattern_source == "pre_board_long_base" and not (
            15 <= _safe_float(spot.circ_market_cap) <= 300
            and target_turnover >= 1.0
        ):
            continue

        dynamic_pool_pattern: dict = dict(standalone_pullback_source)
        pre_board_source = str(pre_board_wave.get("pre_board_ignition_source") or "")
        if not dynamic_pool_pattern and (
            pre_board_wave.get("is_pre_board_ignition_pattern")
            and pre_board_source in {
                "pre_board_momentum_shakeout",
                "pre_board_probe_wash",
                "pre_board_probe_breakout",
            }
            and not is_limit_up
            and not lu
        ):
            dynamic_pool_pattern = {
                "source": pre_board_source,
                "label": pre_board_wave.get("pre_board_ignition_label") or "连板前兆-试盘确认",
                "score": _safe_float(pre_board_wave.get("pre_board_ignition_score")),
                "stats": pre_board_wave.get("pre_board_ignition_stats") or {},
            }
        elif not dynamic_pool_pattern and pattern_source == "low_expectation_trend_watch":
            dynamic_pool_pattern = dict(pattern)
        elif not dynamic_pool_pattern and trend_driver_wave.get("is_trend_driver_pattern"):
            dynamic_pool_pattern = {
                "source": "trend_driver_setup",
                "label": trend_driver_wave.get("trend_driver_label") or "趋势驱动候选",
                "score": _safe_float(trend_driver_wave.get("trend_driver_score")),
                "stats": trend_driver_wave.get("trend_driver_stats") or {},
            }
        elif not dynamic_pool_pattern and (
            main_wave.get("is_main_wave_pattern")
            and _safe_float(main_wave.get("main_wave_score")) >= 82.0
            and _safe_float((main_wave.get("main_wave_stats") or {}).get("pct_12")) >= 25.0
            and _safe_float((main_wave.get("main_wave_stats") or {}).get("near_high_ratio")) >= 0.92
        ):
            # 旧高标记忆可能让同股的展示形态被选成“二波”，但盘中检测应以
            # 当前12日主升结构为准，避免风华高科式绿开回收因二波分桶拥挤漏掉。
            dynamic_pool_pattern = {
                "source": "main_wave_pattern",
                "label": main_wave.get("main_wave_label") or "主升浪形态",
                "score": _safe_float(main_wave.get("main_wave_score")),
                "stats": main_wave.get("main_wave_stats") or {},
            }
        elif not dynamic_pool_pattern and trend_wave.get("is_trend_main_wave_pattern"):
            dynamic_pool_pattern = {
                "source": "trend_main_wave_pattern",
                "label": trend_wave.get("trend_main_wave_label") or "趋势主升加速跟踪",
                "score": _safe_float(trend_wave.get("trend_main_wave_score")),
                "stats": trend_wave.get("trend_main_wave_stats") or {},
            }
        elif not dynamic_pool_pattern and pattern_source in _DYNAMIC_TREND_POOL_SOURCE_PRIORITY:
            dynamic_pool_pattern = dict(pattern)
        main_wave_score = _safe_float(pattern.get("score"))
        fund_5d = fund_5d_map.get(code)
        total_score = max(70.0, min(96.0, main_wave_score))
        risk_warnings = []
        stats = pattern.get("stats") or {}
        if _safe_float(stats.get("pct_12")) >= 60:
            risk_warnings.append("短期涨幅较大，优先等回踩确认")
        if pattern.get("source") == "second_wave_pattern":
            risk_warnings.append("断板二波波动较大，必须等分时承接确认")
            if not _is_second_wave_direct_buy_ready(stats):
                risk_warnings.append("二波直接买点确认不足，先观察回踩不破和量能承接")
        if pattern.get("source") == "second_wave_reset_watch":
            risk_warnings.append("高标下杀二波准备态，只入检测池，不在下跌途中直接低吸")
            risk_warnings.append("盘中出现止跌急拉、放量与盘口/板块共振后才推送")
        if pattern.get("source") == "trend_main_wave_pattern":
            risk_warnings.append("趋势主升加速回测胜率偏低，先观察回踩确认")
        if (
            pattern.get("source") == "main_wave_pullback_pattern"
            and str(stats.get("pullback_quality_tier") or "") == "reclaim"
        ):
            risk_warnings.append("放量深阴只保留趋势记忆；次日收回首阴收盘价且60秒增量成交确认后才提示")
        if pattern.get("source") == "trend_driver_setup":
            risk_warnings.append("趋势驱动候选已入池，必须等待盘中支撑回收或放量突破确认")
        if pattern.get("source") == "low_expectation_trend_watch":
            risk_warnings.append("低位趋势候选不是直接买点，须等滚动60秒增量成交、VWAP及资金/板块二次确认")
        if str(pattern.get("source") or "").startswith("pre_board_"):
            risk_warnings.append("连板前兆不是涨停确认，必须等题材发酵和盘口主动性")
            if pattern.get("source") == "pre_board_long_base":
                risk_warnings.append("250日低位底座只入检测池，盘中放量突破或支撑回收后才推送")
            if pattern.get("source") == "pre_board_probe_breakout":
                risk_warnings.append("试盘突破样本不稳定，先观察题材发酵和盘口主动性")
            if pattern.get("source") == "pre_board_momentum_shakeout":
                risk_warnings.append("中期动量缩量洗盘只扩大预测池，盘中未确认前仓位保持为0")
            if pattern.get("source") == "pre_board_probe_wash":
                risk_warnings.append("试盘缩量洗盘只定义准备态，盘中支撑回收或放量突破后才生效")
        if str(pattern.get("source") or "").startswith("high_board_"):
            risk_warnings.append("高标连板形态必须等题材合力和竞价承接确认")
        if is_limit_up:
            risk_warnings.append("当日涨停后不追高，次日看承接")

        momentum_pct = max(
            _safe_float(stats.get("pct_12")),
            _safe_float(stats.get("pct_20")) * 0.75,
            _safe_float(stats.get("pct_10")),
        )
        dimensions = {
            "technical": round(min(100.0, main_wave_score), 1),
            "momentum": round(min(100.0, max(0.0, momentum_pct) * 1.5), 1),
            "fund": 65 if fund_5d is not None and fund_5d > 0 else 50,
            "activity": round(min(100.0, target_turnover * 5), 1),
        }
        tags = list(pattern.get("tags") or [])
        candidates.append({
            "code": code,
            "name": spot.name or code,
            "total_score": round(total_score, 1),
            "level": "A" if total_score >= 85 else "B",
            "change_pct": round(target_change_pct, 2),
            "dimensions": dimensions,
            "top_signals": [pattern.get("label") or "主升浪形态"] + tags,
            "risk_warnings": risk_warnings,
            "suggestion": "主升浪形态候选，次日优先等承接和支撑位确认",
            "circ_market_cap_billion": round(_safe_float(spot.circ_market_cap), 1),
            "pe_ttm": spot.pe_ttm,
            "turnover": round(target_turnover, 3),
            "volume_ratio": round(target_volume_ratio, 3),
            # Pattern history has no qualified current fund row; no spot fallback.
            "main_net_inflow_billion": None,
            "main_net_inflow_pct": None,
            "main_fund_status": "unknown",
            "volume_ratio_level": _volume_ratio_bucket(target_volume_ratio),
            "turnover_level": _turnover_bucket(target_turnover),
            "price_volume_relation": _price_volume_relation(
                target_change_pct,
                target_volume_ratio,
            ),
            "chip_signal": 0,
            "candidate_source": pattern.get("source") or "main_wave_pattern",
            "candidate_source_label": pattern.get("label") or "主升浪形态",
            "main_wave_score": main_wave_score,
            "main_wave_stats": stats,
            "dynamic_pool_source": dynamic_pool_pattern.get("source") or "",
            "dynamic_pool_label": dynamic_pool_pattern.get("label") or "",
            "dynamic_pool_score": _safe_float(dynamic_pool_pattern.get("score")),
            "dynamic_pool_stats": dynamic_pool_pattern.get("stats") or {},
            "is_limit_up": is_limit_up,
            "consecutive_days": lu.consecutive_days if lu else 0,
            "kline_trade_date": str(kline_target_date),
            **fund_window_payload(fund_window, code),
            "kline_close_reconciled_from_next_spot": kline_close_reconciled,
        })

    candidates.sort(
        key=lambda item: (
            _safe_float(item.get("main_wave_score")),
            _safe_float((item.get("main_wave_stats") or {}).get("pct_12")),
        ),
        reverse=True,
    )
    # 低位趋势临界与连板前兆不是同一赛道，必须在全市场截断前独立预留。
    # 否则长底座候选数量较多时，已经满足低位、未过热、趋势上拐的股票
    # 仍会因为同分排序而完全消失在页面与盘中动态池。
    low_expectation_limit = min(limit, max(8, int(limit * 0.20)))
    low_expectation_candidates = sorted(
        [
            item for item in candidates
            if item.get("candidate_source") == "low_expectation_trend_watch"
        ],
        key=lambda item: (
            _safe_float((item.get("main_wave_stats") or {}).get("low_expectation_quality_score")),
            -_safe_float((item.get("main_wave_stats") or {}).get("position_60"), 1.0),
            _safe_float(item.get("volume_ratio")),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )[:low_expectation_limit]
    low_expectation_codes = {
        str(item.get("code") or "") for item in low_expectation_candidates
    }
    # 首阴/缩量十字星和试盘洗盘都是短时效状态，必须在全市场截断前预留名额；
    # 否则大量高分二波形态会先占满limit，页面和盘中检测池都看不到它们。
    pullback_shape_limit = min(limit, max(8, int(limit * 0.15)))
    pullback_shape_candidates = sorted(
        [
            item for item in candidates
            if str(item.get("code") or "") not in low_expectation_codes
            and item.get("dynamic_pool_source") == "main_wave_pullback_pattern"
        ],
        key=lambda item: (
            _safe_float(item.get("dynamic_pool_score") or item.get("main_wave_score")),
            _safe_float((item.get("dynamic_pool_stats") or {}).get("pullback_prior_gain_pct")),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )[:pullback_shape_limit]
    pullback_shape_codes = {
        str(item.get("code") or "") for item in pullback_shape_candidates
    }

    momentum_shakeout_limit = min(limit, max(8, int(limit * 0.20)))
    momentum_shakeout_candidates = sorted(
        [
            item for item in candidates
            if str(item.get("code") or "") not in pullback_shape_codes
            and (
                item.get("candidate_source") == "pre_board_momentum_shakeout"
                or item.get("dynamic_pool_source") == "pre_board_momentum_shakeout"
            )
        ],
        key=lambda item: (
            _safe_float(item.get("dynamic_pool_score") or item.get("main_wave_score")),
            _safe_float((item.get("dynamic_pool_stats") or item.get("main_wave_stats") or {}).get("pct_10")),
            _safe_float((item.get("dynamic_pool_stats") or item.get("main_wave_stats") or {}).get("recent_big_up_pct")),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )[:momentum_shakeout_limit]
    momentum_shakeout_codes = {
        str(item.get("code") or "") for item in momentum_shakeout_candidates
    }

    probe_wash_limit = min(limit, max(5, int(limit * 0.20)))
    probe_wash_candidates = sorted(
        [
            item for item in candidates
            if str(item.get("code") or "") not in pullback_shape_codes
            and str(item.get("code") or "") not in momentum_shakeout_codes
            and (
                item.get("candidate_source") == "pre_board_probe_wash"
                or item.get("dynamic_pool_source") == "pre_board_probe_wash"
            )
        ],
        key=lambda item: (
            _safe_float(item.get("dynamic_pool_score") or item.get("main_wave_score")),
            -_safe_int((item.get("dynamic_pool_stats") or item.get("main_wave_stats") or {}).get("days_since_probe"), 99),
            -_safe_float((item.get("dynamic_pool_stats") or item.get("main_wave_stats") or {}).get("position_120"), 1.0),
            str(item.get("code") or ""),
        ),
        reverse=True,
    )[:probe_wash_limit]
    probe_wash_codes = {str(item.get("code") or "") for item in probe_wash_candidates}
    remaining_candidates = [
        item for item in candidates
        if str(item.get("code") or "") not in low_expectation_codes
        and str(item.get("code") or "") not in probe_wash_codes
        and str(item.get("code") or "") not in momentum_shakeout_codes
        and str(item.get("code") or "") not in pullback_shape_codes
    ]

    # 长周期低位形态天然比点火形态宽，只预留部分观察池名额，避免挤掉趋势/突破候选。
    long_base_limit = max(1, int(limit * 0.25))
    long_base_candidates = [
        item for item in remaining_candidates
        if item.get("candidate_source") == "pre_board_long_base"
    ][:long_base_limit]
    long_base_codes = {str(item.get("code") or "") for item in long_base_candidates}
    other_candidates = [
        item for item in remaining_candidates
        if str(item.get("code") or "") not in long_base_codes
    ][:max(
        0,
        limit
        - len(low_expectation_candidates)
        - len(pullback_shape_candidates)
        - len(momentum_shakeout_candidates)
        - len(probe_wash_candidates)
        - len(long_base_candidates),
    )]
    selected = (
        low_expectation_candidates
        + pullback_shape_candidates
        + momentum_shakeout_candidates
        + probe_wash_candidates
        + other_candidates
        + long_base_candidates
    )
    selected.sort(
        key=lambda item: (
            _safe_float(item.get("main_wave_score")),
            _safe_float((item.get("main_wave_stats") or {}).get("pct_12")),
        ),
        reverse=True,
    )
    selected = selected[:limit]
    if not selected:
        return []

    # 第二阶段只为最多limit只候选读取完整260日历史。长周期画像负责复核和解释，
    # 不直接产生买点；若首板前兆在完整历史下失效，则从检测池剔除。
    selected_codes = [str(item.get("code") or "") for item in selected]
    full_kline_result = await db.execute(
        select(
            StockKline.code,
            StockKline.open,
            StockKline.close,
            StockKline.high,
            StockKline.low,
            StockKline.volume,
        ).where(
            StockKline.code.in_(selected_codes),
            StockKline.trade_date >= earliest_date,
            StockKline.trade_date <= target_date,
        ).order_by(StockKline.code, StockKline.trade_date)
    )
    full_grouped: dict[str, list[tuple[float, float, float, float, float]]] = defaultdict(list)
    for code, open_price, close, high, low, volume in full_kline_result.all():
        full_grouped[str(code)].append((
            _safe_float(open_price),
            _safe_float(close),
            _safe_float(high),
            _safe_float(low),
            _safe_float(volume),
        ))

    validated: list[dict] = []
    for candidate in selected:
        code = str(candidate.get("code") or "")
        full_rows = full_grouped.get(code) or []
        if len(full_rows) < 120:
            # 不补造历史；非首板前兆仍保留原短周期结构，前端明确显示样本降级。
            if str(candidate.get("candidate_source") or "").startswith("pre_board_"):
                continue
            validated.append(candidate)
            continue

        full_closes = [row[1] for row in full_rows]
        full_highs = [row[2] for row in full_rows]
        full_lows = [row[3] for row in full_rows]
        full_volumes = [row[4] for row in full_rows]
        long_profile = build_long_cycle_profile(
            closes=full_closes,
            highs=full_highs,
            lows=full_lows,
            volumes=full_volumes,
        )
        candidate = dict(candidate)
        candidate["main_wave_stats"] = {
            **dict(candidate.get("main_wave_stats") or {}),
            **long_profile,
        }
        if candidate.get("dynamic_pool_stats"):
            candidate["dynamic_pool_stats"] = {
                **dict(candidate.get("dynamic_pool_stats") or {}),
                **long_profile,
            }

        candidate_source = str(candidate.get("candidate_source") or "")
        dynamic_source = str(candidate.get("dynamic_pool_source") or "")
        if candidate_source.startswith("pre_board_") or dynamic_source.startswith("pre_board_"):
            spot = spot_map.get(code)
            full_pre_board = _score_pre_board_ignition_kline_pattern(
                closes=full_closes,
                highs=full_highs,
                lows=full_lows,
                volumes=full_volumes,
                change_pct=_safe_float(getattr(spot, "change_pct", 0)),
                turnover=_safe_float(getattr(spot, "turnover", 0)),
                volume_ratio=_safe_float(getattr(spot, "volume_ratio", 1.0), 1.0),
            )
            full_ready = bool(full_pre_board.get("is_pre_board_ignition_pattern"))
            if candidate_source.startswith("pre_board_") and not full_ready:
                continue
            if full_ready:
                full_source = str(full_pre_board.get("pre_board_ignition_source") or "")
                full_label = str(full_pre_board.get("pre_board_ignition_label") or "连板前兆")
                full_score = _safe_float(full_pre_board.get("pre_board_ignition_score"))
                full_stats = dict(full_pre_board.get("pre_board_ignition_stats") or {})
                if candidate_source.startswith("pre_board_"):
                    candidate["candidate_source"] = full_source
                    candidate["candidate_source_label"] = full_label
                    candidate["main_wave_score"] = full_score
                    candidate["main_wave_stats"] = full_stats
                    candidate["top_signals"] = [full_label] + list(
                        full_pre_board.get("pre_board_ignition_tags") or []
                    )
                if dynamic_source.startswith("pre_board_"):
                    candidate["dynamic_pool_source"] = full_source
                    candidate["dynamic_pool_label"] = full_label
                    candidate["dynamic_pool_score"] = full_score
                    candidate["dynamic_pool_stats"] = full_stats
            elif dynamic_source.startswith("pre_board_"):
                candidate["dynamic_pool_source"] = ""
                candidate["dynamic_pool_label"] = ""
                candidate["dynamic_pool_score"] = 0.0
                candidate["dynamic_pool_stats"] = {}
        validated.append(candidate)

    validated.sort(
        key=lambda item: (
            _safe_float(item.get("main_wave_score")),
            _safe_float((item.get("main_wave_stats") or {}).get("pct_12")),
        ),
        reverse=True,
    )
    return validated[:limit]


async def _bull_rank(db: AsyncSession) -> dict:
    """短线强势排行 — 两轮评分策略 v3.0

    性能优化:
    - 第1轮: 粗筛(放宽条件)→快速算6维(技术=30基准), 取Top300
    - 第2轮: Top300从K线算技术形态维度(MA/BOLL/MACD/RSI/KDJ/突破), 最终排序
    - 60秒服务端缓存, 命中时<100ms

    v3.0改进:
    - 粗筛条件放宽: change_pct>-3 OR turnover>0.5 OR volume_ratio>1.5 OR main_net_inflow>0
    - Top200→Top300进入第2轮
    - 新增MACD/RSI/KDJ技术指标计算
    - 新高判断用high而非price
    - BOLL计算用ddof=1
    """
    global _RANK_CACHE, _last_rank_fetch

    target_date = await resolve_latest_trade_date(db, SectorPersistence.trade_date)
    snapshot_key = _rank_snapshot_key("bull")
    cache_key = _snapshot_cache_key(snapshot_key, target_date)
    now = time.time()
    if cache_key in _RANK_CACHE:
        cached_data, cached_at = _RANK_CACHE[cache_key]
        if now - cached_at < _RANK_CACHE_TTL:
            logger.debug(f"[bull_rank] 缓存命中, 年龄{now-cached_at:.1f}s")
            return cached_data

    persisted = await _get_persisted_dashboard_snapshot(
        db,
        snapshot_key,
        target_date,
        _RANK_CACHE_TTL,
    )
    if persisted is not None:
        _RANK_CACHE[cache_key] = (persisted, time.time())
        return persisted

    logger.info(f"[bull_rank] 缓存未命中, 重新计算...")
    fund_decision_at = datetime.now()
    current_fund_map = await _get_current_eastmoney_main_fund_items(db, trade_date=target_date)
    positive_fund_codes = {
        code for code, item in current_fund_map.items()
        if _safe_float(item.get("main_net_inflow")) > 0
    }
    # ===== 1. 批量读取spot(粗筛放宽: 跌幅>-3% 或 换手>0.5% 或 量比>1.5 或 主力净流入>0) =====
    # [v3.0] 放宽粗筛: 捕捉逆势建仓/蓄势股
    result = await db.execute(
        select(StockSpot).where(
            StockSpot.price > 0,
            StockSpot.volume > 0,
        )
    )
    all_spots_raw = result.scalars().all()
    all_spots = [
        spot for spot in all_spots_raw
        if (
            (spot.change_pct or 0) > -3
            or (spot.turnover or 0) > 0.5
            or (spot.volume_ratio or 0) > 1.5
            or spot.code in positive_fund_codes
        )
    ]

    if not all_spots:
        return {"rank": [], "mode": "bull"}

    # ===== 2. 批量读取今日涨停池 =====
    today = target_date  # 使用resolve_latest_trade_date确定的实际交易日
    lu_result = await db.execute(
        select(LimitUpPool).where(LimitUpPool.trade_date == today)
    )
    limit_up_map = {lu.code: lu for lu in lu_result.scalars().all()}

    # ===== 3. 批量读取5日资金流 =====
    fund_window = await load_main_fund_window(
        db, through_date=today, decision_at=fund_decision_at, codes=[spot.code for spot in all_spots],
    )
    fund_5d_map = {code: item["total"] for code, item in fund_window["items"].items()}

    # ===== 4. 批量读取近5日K线(算5日涨幅) =====
    kline_5d_result = await db.execute(
        select(StockKline.code, StockKline.close, StockKline.trade_date).where(
            StockKline.trade_date >= today - timedelta(days=10),
            StockKline.trade_date <= today,
        ).order_by(StockKline.code, desc(StockKline.trade_date))
    )
    kline_5d_rows = kline_5d_result.all()

    kline_5d_map = {}
    for code, close, trade_date in reversed(kline_5d_rows):
        if code not in kline_5d_map:
            kline_5d_map[code] = []
        kline_5d_map[code].append(close)

    change_5d_map = {}
    for code, closes in kline_5d_map.items():
        if len(closes) >= 2:
            change_5d_map[code] = round((closes[-1] / closes[0] - 1) * 100, 2)

    # [v3.2 P1] 计算市场环境
    advances = sum(1 for s in all_spots if (s.change_pct or 0) > 0.5)
    declines = sum(1 for s in all_spots if (s.change_pct or 0) < -0.5)
    ad_ratio = advances / declines if declines > 0 else float(advances)
    limit_up_count = len(limit_up_map)
    if ad_ratio > 2 and limit_up_count > 50:
        market_regime = "trend"
    elif ad_ratio < 0.8 or limit_up_count < 20:
        market_regime = "decline"
    elif 0.8 <= ad_ratio <= 2:
        market_regime = "rotation"
    else:
        market_regime = "unknown"
    logger.info(f"[bull_rank] 市场环境: {market_regime} (涨跌比={ad_ratio:.2f}, 涨停={limit_up_count})")

    # ===== 5. 第1轮: 用spot字段快速评分(技术维度=40基准) =====
    first_round = []
    for spot in all_spots:
        code = spot.code
        lu = limit_up_map.get(code)
        is_limit_up = (spot.limit_up and spot.price and spot.price >= spot.limit_up)
        is_one_word = is_limit_up and spot.open and spot.open >= spot.limit_up
        consecutive_days = lu.consecutive_days if lu else 0
        current_fund = _get_current_main_fund_item(current_fund_map, code)
        current_main_inflow = _safe_float((current_fund or {}).get("main_net_inflow"))

        quick_score = _bull_model.score_from_spot(
            code=code, name=spot.name or code,
            change_pct=spot.change_pct if spot.change_pct is not None else 0,
            volume_ratio=spot.volume_ratio if spot.volume_ratio is not None else 1.0,
            turnover=spot.turnover if spot.turnover is not None else 0,
            amplitude=spot.amplitude if spot.amplitude is not None else 0,
            main_net_inflow=current_main_inflow,
            main_net_inflow_pct=_safe_float((current_fund or {}).get("main_net_inflow_pct")),
            super_net_inflow=_safe_float((current_fund or {}).get("super_net_inflow")),
            super_net_inflow_pct=_safe_float((current_fund or {}).get("super_net_inflow_pct")),
            big_net_inflow=_safe_float((current_fund or {}).get("big_net_inflow")),
            big_net_inflow_pct=_safe_float((current_fund or {}).get("big_net_inflow_pct")),
            circ_market_cap=spot.circ_market_cap if spot.circ_market_cap is not None else 0,
            pe_ttm=spot.pe_ttm if spot.pe_ttm is not None else 0,
            pb=spot.pb if spot.pb is not None else 0,
            net_profit_growth=spot.net_profit_growth if spot.net_profit_growth is not None else 0,
            price=spot.price if spot.price is not None else 0,
            open=spot.open if spot.open is not None else 0,
            high=spot.high if spot.high is not None else 0,
            low=spot.low if spot.low is not None else 0,
            limit_up=spot.limit_up if spot.limit_up is not None else 0,
            main_net_inflow_5d=fund_5d_map.get(code),
            change_pct_5d=change_5d_map.get(code, 0),
            consecutive_days=consecutive_days,
            is_limit_up=is_limit_up,
            is_one_word=is_one_word,
            market_regime=market_regime,
        )

        first_round.append({
            "spot": spot,
            "quick_score": quick_score.total_score,
            "is_limit_up": is_limit_up,
            "is_one_word": is_one_word,
            "consecutive_days": consecutive_days,
            "fund_5d": fund_5d_map.get(code),
            "change_5d": change_5d_map.get(code, 0),
        })

    # 取Top300进入第2轮 [v3.0: 200→300]
    first_round.sort(key=lambda x: x["quick_score"], reverse=True)
    top300 = first_round[:300]

    # ===== 6. 第2轮: Top300批量计算技术指标(raw SQL) =====
    import numpy as np
    from collections import defaultdict
    from sqlalchemy import text

    top300_codes = [item["spot"].code for item in top300]
    tech_map = {}

    # ===== 优化: 批量查询K线(一次查询300只, 减少RTT) =====
    # 先获取最近120个交易日的日期列表
    date_result = await db.execute(
        text("""
            SELECT DISTINCT trade_date FROM stock_kline
            WHERE trade_date <= :target_date
            ORDER BY trade_date DESC
            LIMIT 120
        """),
        {"target_date": str(target_date)},
    )
    recent_dates = [r[0] for r in date_result.all()]
    if len(recent_dates) < 20:
        recent_dates = []  # 数据不足,回退到逐只查询

    if recent_dates and len(top300_codes) > 0:
        # 批量查询: 一次取300只的K线数据(含close/high/low用于MACD/RSI/KDJ)
        # [v4.0修复] 参数化查询防止SQL注入 — 动态生成占位符
        code_params = {f"code_{i}": c for i, c in enumerate(top300_codes)}
        code_placeholders = ", ".join([f":code_{i}" for i in range(len(top300_codes))])
        date_params = {f"date_{i}": str(d) for i, d in enumerate(recent_dates)}
        date_placeholders = ", ".join([f":date_{i}" for i in range(len(recent_dates))])
        all_params = {**code_params, **date_params}

        batch_result = await db.execute(
            text(f"""
                SELECT code, close, high, low FROM stock_kline
                WHERE code IN ({code_placeholders})
                AND trade_date IN ({date_placeholders})
                ORDER BY code, trade_date ASC
            """),
            all_params,
        )

        # 按code分组
        code_klines = defaultdict(list)
        for row in batch_result.all():
            code_klines[row[0]].append((row[1], row[2], row[3]))  # (close, high, low)

        # 计算技术指标(含MACD/RSI/KDJ)
        for code, klines in code_klines.items():
            if len(klines) < 5:
                continue
            closes = np.array([k[0] for k in klines if k[0]])
            highs = np.array([k[1] for k in klines if k[1]])
            lows = np.array([k[2] for k in klines if k[2]])
            tech = {}
            
            # MA
            if len(closes) >= 5:  tech["ma5"] = round(float(closes[-5:].mean()), 2)
            if len(closes) >= 10: tech["ma10"] = round(float(closes[-10:].mean()), 2)
            if len(closes) >= 20:
                tech["ma20"] = round(float(closes[-20:].mean()), 2)
                std20 = float(closes[-20:].std(ddof=0))  # 布林带用总体标准差(与spot.py/通达信一致)
                tech["boll_upper"] = round(tech["ma20"] + 2 * std20, 2)
                tech["boll_mid"] = tech["ma20"]
                tech["boll_lower"] = round(tech["ma20"] - 2 * std20, 2)
            if len(closes) >= 60:  tech["ma60"] = round(float(closes[-60:].mean()), 2)
            
            # 新高 [v3.0] 用high而非close
            if len(highs) >= 20:  tech["high_20d"] = round(float(highs[-20:].max()), 2)
            if len(highs) >= 60:  tech["high_60d"] = round(float(highs[-60:].max()), 2)
            
            # [v3.0新增] MACD — EMA(12), EMA(26), Signal(9)
            if len(closes) >= 35:
                ema12 = _calc_ema(closes, 12)
                ema26 = _calc_ema(closes, 26)
                dif = ema12 - ema26  # 等长数组，前25个为NaN
                # 取有效部分计算DEA(从第一个非NaN开始)
                valid_start = 25  # ema26的有效起始位置
                dif_valid = dif[valid_start:]
                dea_valid = _calc_ema(dif_valid, 9)
                macd_hist_valid = 2 * (dif_valid - dea_valid)
                # 判断金叉/死叉: 最后2根MACD柱
                if len(macd_hist_valid) >= 2:
                    # 跳过NaN
                    hist_vals = macd_hist_valid[~np.isnan(macd_hist_valid)]
                    if len(hist_vals) >= 2:
                        if hist_vals[-1] > 0 and hist_vals[-2] <= 0:
                            tech["macd_signal"] = "golden_cross"
                        elif hist_vals[-1] < 0 and hist_vals[-2] >= 0:
                            tech["macd_signal"] = "death_cross"
            
            # [v3.0新增] RSI(14)
            if len(closes) >= 15:
                tech["rsi14"] = round(float(_calc_rsi(closes, 14)), 1)
            
            # [v3.0新增] KDJ(9,3,3) — 至少20根K线才有足够样本判断金叉/死叉
            # [v4.0修复] 增加K-D差值>5确认，避免粘合区误判
            if len(closes) >= 20 and len(highs) >= 20 and len(lows) >= 20:
                kdj = _calc_kdj(highs, lows, closes, 9, 3, 3)
                if kdj:
                    k_vals, d_vals = kdj
                    if len(k_vals) >= 2 and len(d_vals) >= 2:
                        kd_diff = abs(k_vals[-1] - d_vals[-1])
                        if k_vals[-1] > d_vals[-1] and k_vals[-2] <= d_vals[-2] and kd_diff > 5:
                            tech["kdj_signal"] = "golden_cross"
                        elif k_vals[-1] < d_vals[-1] and k_vals[-2] >= d_vals[-2] and kd_diff > 5:
                            tech["kdj_signal"] = "death_cross"
            
            tech_map[code] = tech
    else:
        # 回退: 逐只查询(数据不足时)
        for code in top300_codes:
            kline_result = await db.execute(
                text("""
                    SELECT close, high, low FROM stock_kline
                    WHERE code = :code
                    AND trade_date <= :target_date
                    ORDER BY trade_date DESC
                    LIMIT 120
                """),
                {"code": code, "target_date": str(target_date)}
            )
            rows = kline_result.all()
            if len(rows) < 5:
                continue
            rows = list(reversed(rows))
            closes = np.array([r[0] for r in rows if r[0]])
            highs = np.array([r[1] for r in rows if r[1]])
            lows = np.array([r[2] for r in rows if r[2]])
            tech = {}
            if len(closes) >= 5:  tech["ma5"] = round(float(closes[-5:].mean()), 2)
            if len(closes) >= 10: tech["ma10"] = round(float(closes[-10:].mean()), 2)
            if len(closes) >= 20:
                tech["ma20"] = round(float(closes[-20:].mean()), 2)
                std20 = float(closes[-20:].std(ddof=0))  # 布林带用总体标准差(与spot.py/通达信一致)
                tech["boll_upper"] = round(tech["ma20"] + 2 * std20, 2)
                tech["boll_mid"] = tech["ma20"]
                tech["boll_lower"] = round(tech["ma20"] - 2 * std20, 2)
            if len(closes) >= 60:  tech["ma60"] = round(float(closes[-60:].mean()), 2)
            if len(highs) >= 20:  tech["high_20d"] = round(float(highs[-20:].max()), 2)
            if len(highs) >= 60:  tech["high_60d"] = round(float(highs[-60:].max()), 2)
            # MACD/RSI/KDJ
            if len(closes) >= 35:
                ema12 = _calc_ema(closes, 12)
                ema26 = _calc_ema(closes, 26)
                dif = ema12 - ema26
                valid_start = 25
                dif_valid = dif[valid_start:]
                dea_valid = _calc_ema(dif_valid, 9)
                macd_hist_valid = 2 * (dif_valid - dea_valid)
                if len(macd_hist_valid) >= 2:
                    hist_vals = macd_hist_valid[~np.isnan(macd_hist_valid)]
                    if len(hist_vals) >= 2:
                        if hist_vals[-1] > 0 and hist_vals[-2] <= 0:
                            tech["macd_signal"] = "golden_cross"
                        elif hist_vals[-1] < 0 and hist_vals[-2] >= 0:
                            tech["macd_signal"] = "death_cross"
            if len(closes) >= 15:
                tech["rsi14"] = round(float(_calc_rsi(closes, 14)), 1)
            if len(closes) >= 20 and len(highs) >= 20 and len(lows) >= 20:
                kdj = _calc_kdj(highs, lows, closes, 9, 3, 3)
                if kdj:
                    k_vals, d_vals = kdj
                    if len(k_vals) >= 2 and len(d_vals) >= 2:
                        kd_diff = abs(k_vals[-1] - d_vals[-1])
                        if k_vals[-1] > d_vals[-1] and k_vals[-2] <= d_vals[-2] and kd_diff > 5:
                            tech["kdj_signal"] = "golden_cross"
                        elif k_vals[-1] < d_vals[-1] and k_vals[-2] >= d_vals[-2] and kd_diff > 5:
                            tech["kdj_signal"] = "death_cross"
            tech_map[code] = tech

    # ===== 7. 对Top300重新评分(含技术维度+MACD/RSI/KDJ) =====
    rank = []
    for item in top300:
        spot = item["spot"]
        code = spot.code
        tech = tech_map.get(code, {})
        current_fund = _get_current_main_fund_item(current_fund_map, code)
        current_main_inflow = _safe_float((current_fund or {}).get("main_net_inflow"))

        final_result = _bull_model.score_from_spot(
            code=code, name=spot.name or code,
            change_pct=spot.change_pct if spot.change_pct is not None else 0,
            volume_ratio=spot.volume_ratio if spot.volume_ratio is not None else 1.0,
            turnover=spot.turnover if spot.turnover is not None else 0,
            amplitude=spot.amplitude if spot.amplitude is not None else 0,
            main_net_inflow=current_main_inflow,
            main_net_inflow_pct=_safe_float((current_fund or {}).get("main_net_inflow_pct")),
            super_net_inflow=_safe_float((current_fund or {}).get("super_net_inflow")),
            super_net_inflow_pct=_safe_float((current_fund or {}).get("super_net_inflow_pct")),
            big_net_inflow=_safe_float((current_fund or {}).get("big_net_inflow")),
            big_net_inflow_pct=_safe_float((current_fund or {}).get("big_net_inflow_pct")),
            circ_market_cap=spot.circ_market_cap if spot.circ_market_cap is not None else 0,
            pe_ttm=spot.pe_ttm if spot.pe_ttm is not None else 0,
            pb=spot.pb if spot.pb is not None else 0,
            net_profit_growth=spot.net_profit_growth if spot.net_profit_growth is not None else 0,
            price=spot.price if spot.price is not None else 0,
            open=spot.open if spot.open is not None else 0,
            high=spot.high if spot.high is not None else 0,
            low=spot.low if spot.low is not None else 0,
            limit_up=spot.limit_up if spot.limit_up is not None else 0,
            amount=spot.amount if spot.amount is not None else 0,
            ma5=tech.get("ma5", 0),
            ma10=tech.get("ma10", 0),
            ma20=tech.get("ma20", 0),
            ma60=tech.get("ma60", 0),
            boll_upper=tech.get("boll_upper", 0),
            boll_mid=tech.get("boll_mid", 0),
            boll_lower=tech.get("boll_lower", 0),
            high_20d=tech.get("high_20d", 0),
            high_60d=tech.get("high_60d", 0),
            macd_signal=tech.get("macd_signal", ""),
            rsi14=tech.get("rsi14"),
            kdj_signal=tech.get("kdj_signal", ""),
            main_net_inflow_5d=item["fund_5d"],
            change_pct_5d=item["change_5d"],
            consecutive_days=item["consecutive_days"],
            is_limit_up=item["is_limit_up"],
            is_one_word=item["is_one_word"],
            market_regime=market_regime,
        )
        short_trend_meta = _build_volume_price_uptrend_meta(
            price=_safe_float(spot.price),
            change_pct=_safe_float(spot.change_pct),
            volume_ratio=_safe_float(spot.volume_ratio, 1.0),
            turnover=_safe_float(spot.turnover),
            main_net_inflow=current_main_inflow,
            main_net_inflow_5d=_safe_float(item["fund_5d"]),
            ma5=_safe_float(tech.get("ma5")),
            ma10=_safe_float(tech.get("ma10")),
            ma20=_safe_float(tech.get("ma20")),
            rsi14=tech.get("rsi14"),
            is_limit_up=bool(item["is_limit_up"]),
        )

        rank.append({
            "code": code,
            "name": spot.name or code,
            "total_score": final_result.total_score,
            "level": final_result.level,
            "change_pct": spot.change_pct,
            "dimensions": final_result.dimensions,
            "top_signals": final_result.top_signals,
            "risk_warnings": final_result.risk_warnings,
            "suggestion": final_result.suggestion,
            "circ_market_cap_billion": round(spot.circ_market_cap or 0, 1),
            "pe_ttm": spot.pe_ttm,
            "turnover": spot.turnover,
            "volume_ratio": spot.volume_ratio,
            "main_net_inflow_billion": round(current_main_inflow / 1e8, 2) if current_fund else None,
            "main_fund_status": "snapshot_known" if current_fund else "unknown",
            "main_fund_source_quote_at": current_fund.get("source_quote_at"),
            "main_fund_purpose": "ranking_snapshot",
            **fund_window_payload(fund_window, code),
            "main_net_inflow_pct": _get_current_main_fund_pct(current_fund_map, code),
            # V2.2融合新增
            "volume_ratio_level": final_result.volume_ratio_level,
            "turnover_level": final_result.turnover_level,
            "price_volume_relation": final_result.price_volume_relation,
            "chip_signal": final_result.chip_signal,
            **short_trend_meta,
        })

    rank.sort(key=lambda x: x["total_score"], reverse=True)

    filtered = await stock_tagger.filter_signals(db, rank)
    result = {"rank": filtered, "mode": "bull", "total": len(filtered)}

    # 写入缓存
    _RANK_CACHE[cache_key] = (result, time.time())
    await _persist_dashboard_snapshot(db, snapshot_key, target_date, result)
    logger.info(f"[bull_rank] 计算完成, 结果已缓存")
    return result


async def _tenbagger_rank(db: AsyncSession) -> dict:
    """十倍潜力排行 — TenbaggerModel 5维度 v3.0
    
    v3.0改进:
    - 候选池从500→1500: 增速前500 + 小市值前500 + 活跃板块成分500 → 去重
    - 按市值比例调整资金维度(在模型中已修复)
    """
    global _RANK_CACHE

    target_date = await resolve_latest_trade_date(db, SectorPersistence.trade_date)
    snapshot_key = _rank_snapshot_key("tenbagger")
    cache_key = _snapshot_cache_key(snapshot_key, target_date)
    now = time.time()
    if cache_key in _RANK_CACHE:
        cached_data, cached_at = _RANK_CACHE[cache_key]
        if now - cached_at < _RANK_CACHE_TTL:
            logger.debug(f"[tenbagger_rank] 缓存命中, 年龄{now-cached_at:.1f}s")
            return cached_data

    persisted = await _get_persisted_dashboard_snapshot(
        db,
        snapshot_key,
        target_date,
        _RANK_CACHE_TTL,
    )
    if persisted is not None:
        _RANK_CACHE[cache_key] = (persisted, time.time())
        return persisted

    logger.info(f"[tenbagger_rank] 缓存未命中, 重新计算...")
    fund_decision_at = datetime.now()
    current_fund_map = await _get_current_eastmoney_main_fund_items(db, trade_date=target_date)

    # [v3.0] 候选池扩大: 多条件取并集 → 去重
    # 1) 增速前500 (排除超大市值蓝筹，如银行利润增速>100%但非十倍股)
    growth_result = await db.execute(
        select(StockSpot.code).where(
            StockSpot.price > 0,
            StockSpot.volume > 0,
            StockSpot.circ_market_cap < 500,  # 排除500亿以上大蓝筹
        ).order_by(desc(StockSpot.net_profit_growth)).limit(500)
    )
    growth_codes = {r[0] for r in growth_result.all()}
    
    # 2) 小市值前500
    cap_result = await db.execute(
        select(StockSpot.code).where(
            StockSpot.price > 0,
            StockSpot.volume > 0,
            StockSpot.circ_market_cap > 0,
            StockSpot.circ_market_cap < 200,  # 200亿以下
        ).order_by(StockSpot.circ_market_cap).limit(500)
    )
    cap_codes = {r[0] for r in cap_result.all()}
    
    # 3) 活跃板块成分股前500(板块持续活跃>=2天的成分股)
    active_sector_codes = set()
    try:
        sp_result = await db.execute(
            select(SectorPersistence.sector_code).where(
                SectorPersistence.trade_date == target_date,
                SectorPersistence.consecutive_days >= 2,
            ).limit(50)
        )
        active_sectors = [r[0] for r in sp_result.all()]
        if active_sectors:
            mapping_result = await db.execute(
                select(StockSectorMapping.code).where(
                    StockSectorMapping.sector_code.in_(active_sectors),
                ).limit(500)
            )
            active_sector_codes = {r[0] for r in mapping_result.all()}
    except Exception as e:
        logger.warning(f"[tenbagger_rank] 活跃板块查询失败: {e}")
    
    # 合并去重
    candidate_codes = growth_codes | cap_codes | active_sector_codes
    logger.info(f"[tenbagger_rank] 候选池: 增速{len(growth_codes)}+小市值{len(cap_codes)}+活跃板块{len(active_sector_codes)}→合并{len(candidate_codes)}只")
    
    if not candidate_codes:
        return {"rank": [], "mode": "tenbagger"}
    
    # 批量获取spot数据
    result = await db.execute(
        select(StockSpot).where(StockSpot.code.in_(candidate_codes))
    )
    all_spots = result.scalars().all()

    if not all_spots:
        return {"rank": [], "mode": "tenbagger"}

    # 批量获取板块持续性数据
    today = target_date  # 使用resolve_latest_trade_date确定的实际交易日
    codes = [s.code for s in all_spots]
    mapping_result = await db.execute(
        select(StockSectorMapping.code, StockSectorMapping.sector_code)
        .where(StockSectorMapping.code.in_(codes))
    )
    code_sectors = {}
    for code, sector_code in mapping_result.all():
        if code not in code_sectors:
            code_sectors[code] = []
        code_sectors[code].append(sector_code)

    all_sector_codes = set()
    for sc_list in code_sectors.values():
        all_sector_codes.update(sc_list)

    sp_result = await db.execute(
        select(SectorPersistence).where(
            SectorPersistence.sector_code.in_(all_sector_codes),
            SectorPersistence.trade_date == today,
        )
    )
    sector_persistence_map = {sp.sector_code: sp for sp in sp_result.scalars().all()}

    # 批量获取5日资金流
    fund_window = await load_main_fund_window(
        db, through_date=today, decision_at=fund_decision_at, codes=codes,
    )
    fund_5d_map = {code: item["total"] for code, item in fund_window["items"].items()}

    # 逐只评分
    rank = []
    for spot in all_spots:
        try:
            code = spot.code

            # 该股所属板块的最佳持续性
            sector_days = 0
            sector_fund = 0
            if code in code_sectors:
                best_days = 0
                best_fund = 0
                for sector_code in code_sectors[code]:
                    sp = sector_persistence_map.get(sector_code)
                    if sp:
                        if (sp.consecutive_days or 0) > best_days:
                            best_days = sp.consecutive_days or 0
                        if (sp.fund_flow or 0) > best_fund:
                            best_fund = sp.fund_flow or 0
                sector_days = best_days
                sector_fund = best_fund

            fund_5d = fund_5d_map.get(code)
            current_fund = _get_current_main_fund_item(current_fund_map, code)
            current_main_inflow = _safe_float((current_fund or {}).get("main_net_inflow"))

            tenbagger_result = tenbagger_model.score(
                code=code, name=spot.name or code,
                circ_market_cap=spot.circ_market_cap if spot.circ_market_cap is not None else 0,
                net_profit_growth=spot.net_profit_growth if spot.net_profit_growth is not None else 0,
                pe_ttm=spot.pe_ttm if spot.pe_ttm is not None else 0,
                pb=spot.pb if spot.pb is not None else 0,
                sector_consecutive_days=sector_days,
                sector_fund_flow=sector_fund,
                main_net_inflow=current_main_inflow,
                main_net_inflow_pct=_safe_float((current_fund or {}).get("main_net_inflow_pct")),
                super_net_inflow=_safe_float((current_fund or {}).get("super_net_inflow")),
                super_net_inflow_pct=_safe_float((current_fund or {}).get("super_net_inflow_pct")),
                big_net_inflow=_safe_float((current_fund or {}).get("big_net_inflow")),
                big_net_inflow_pct=_safe_float((current_fund or {}).get("big_net_inflow_pct")),
                main_net_inflow_5d=fund_5d,
                change_pct=spot.change_pct if spot.change_pct is not None else 0,
                is_limit_down=bool(spot.limit_down and spot.price and spot.price <= spot.limit_down),
                is_20pct_board=str(code).startswith(("300", "688")),
                is_limit_up=bool(spot.limit_up and spot.price and spot.price >= spot.limit_up),
                consecutive_days=0,
            )
            rank.append({
                "code": code,
                "name": spot.name or code,
                "total_score": tenbagger_result.total_score,
                "level": tenbagger_result.level,
                "dimensions": tenbagger_result.dimensions,
                "highlights": tenbagger_result.highlights,
                "risks": tenbagger_result.risks,
                "risk_warnings": tenbagger_result.risks,  # 兼容前端
                "top_signals": tenbagger_result.highlights[:3],  # 兼容前端
                "suggestion": tenbagger_result.suggestion,
                "pe_ttm": spot.pe_ttm,
                "net_profit_growth": spot.net_profit_growth,
                "pb": spot.pb,
                "circ_market_cap_billion": round(spot.circ_market_cap or 0, 1),
                "main_net_inflow_billion": round(current_main_inflow / 1e8, 2) if current_fund else None,
                "main_fund_status": "snapshot_known" if current_fund else "unknown",
                "main_fund_source_quote_at": current_fund.get("source_quote_at"),
                "main_fund_purpose": "ranking_snapshot",
                **fund_window_payload(fund_window, code),
                "main_net_inflow_pct": _get_current_main_fund_pct(current_fund_map, code),
                "dividend_yield": spot.dividend_yield,
                "turnover": spot.turnover,
                "change_pct": spot.change_pct,  # [v3.1] 前端展示涨跌幅
            })
        except Exception as e:
            logger.warning(f"[tenbagger_rank] {spot.code} 评分失败: {e}")

    rank.sort(key=lambda x: x["total_score"], reverse=True)

    filtered = await stock_tagger.filter_signals(db, rank)
    result = {"rank": filtered, "mode": "tenbagger", "total": len(filtered)}

    # 写入缓存
    _RANK_CACHE[cache_key] = (result, time.time())
    await _persist_dashboard_snapshot(db, snapshot_key, target_date, result)
    logger.info(f"[tenbagger_rank] 计算完成, 结果已缓存")
    return result


async def prewarm_dragon_snapshot(
    db: AsyncSession,
    trade_date: date | None = None,
    force_refresh: bool = False,
) -> dict:
    target_date = await resolve_latest_trade_date(db, SectorPersistence.trade_date, trade_date)
    snapshot_key = _dragon_snapshot_key()

    if not force_refresh:
        cached = _get_cached_payload(_DRAGON_CACHE, snapshot_key, target_date, _DRAGON_CACHE_TTL)
        if cached is not None:
            return cached

        persisted = await _get_persisted_dashboard_snapshot(
            db,
            snapshot_key,
            target_date,
            _DRAGON_CACHE_TTL,
        )
        if persisted is not None:
            _set_cached_payload(_DRAGON_CACHE, snapshot_key, target_date, persisted)
            return persisted

        # 盘后/午休页面不需要因 5 分钟 TTL 失效而重新扫描全市场。龙头画像只依赖
        # 已落库的日线、资金与板块快照；交易中仍走实时重算，保证盘中异动不会被旧
        # 快照遮蔽。启动预热或用户显式 force_refresh 仍可生成新的收盘快照。
        session_name = trade_calendar.get_trade_session()
        if session_name not in {"pre_auction", "morning", "afternoon"}:
            stale_payload = await _get_latest_persisted_dashboard_snapshot(
                db,
                snapshot_key,
                target_date,
            )
            if stale_payload is not None:
                stale_payload = copy.deepcopy(stale_payload)
                stale_payload["served_from_snapshot"] = True
                stale_payload["snapshot_stale"] = True
                _set_cached_payload(_DRAGON_CACHE, snapshot_key, target_date, stale_payload)
                return stale_payload

    dragons = await anomaly_scanner.scan_dragons(db, target_date=target_date)
    raw_dragons = _dedupe_dragons_by_code(
        [_serialize_dragon_result(result) for result in dragons],
        limit=50,
    )

    filtered = await stock_tagger.filter_signals(db, raw_dragons)
    payload = {
        "trade_date": str(target_date),
        "snapshot_time": datetime.now().isoformat(),
        "dragons": filtered,
    }
    await _persist_dashboard_snapshot(db, snapshot_key, target_date, payload)
    _set_cached_payload(_DRAGON_CACHE, snapshot_key, target_date, payload)
    return payload


async def prewarm_next_day_plan_snapshot(
    db: AsyncSession,
    trade_date: date | None = None,
    *,
    force_refresh: bool = False,
    limit: int = 20,
) -> dict:
    """明日预案 — 复用强势排行 _bull_rank 数据管道 + NextDayPlanEngine 策略层

    数据流: _bull_rank() → 7维评分+技术指标+5日资金+连板 → NextDayPlanEngine → 双策略
    """
    target_date = await resolve_latest_trade_date(db, SectorPersistence.trade_date, trade_date)
    snapshot_key = _plan_snapshot_key(limit)

    if not force_refresh:
        cached = _get_cached_payload(_PLAN_CACHE, snapshot_key, target_date, _PLAN_CACHE_TTL)
        if cached is not None:
            anomaly_scanner.update_dynamic_trend_pool(cached.get("trend_pool") or [])
            return cached

        persisted = await _get_persisted_dashboard_snapshot(
            db,
            snapshot_key,
            target_date,
            _PLAN_CACHE_TTL,
        )
        if persisted is not None:
            _set_cached_payload(_PLAN_CACHE, snapshot_key, target_date, persisted)
            anomaly_scanner.update_dynamic_trend_pool(persisted.get("trend_pool") or [])
            return persisted

    # Freeze the new plan's funding cutoff before any ranking/sector work.
    fund_decision_at = datetime.now()
    # ===== 1. 直接复用强势排行数据 =====
    try:
        rank_data = await _bull_rank(db)
    except Exception as e:
        logger.error(f"[next_day_plan] _bull_rank 调用失败: {e}")
        rank_data = {"rank": []}

    rank_list = rank_data.get("rank", [])

    # ===== 2. 批量获取板块涨跌幅(用于板块风控) =====
    sector_change_map = {}
    try:
        # 获取板块今日涨跌幅: 用 SectorPersistence 的 change_pct
        sp_result = await db.execute(
            select(SectorPersistence.sector_code, SectorPersistence.change_pct)
            .where(SectorPersistence.trade_date == target_date)
        )
        for row in sp_result.all():
            sector_change_map[row[0]] = _safe_float(row[1], 0)
    except Exception:
        pass

    # ===== 3. 个股→主驱动板块映射 =====
    # 不再使用数据库返回的第一条映射；候选池确定后按生命周期、强度和资金择优。
    code_sector_map = {}

    # ===== 4. 获取板块Lifecycle =====
    sector_lifecycle_map = {}
    sector_lifecycle_detail_map: dict[str, dict] = {}
    try:
        lifecycle_result = await db.execute(
            select(
                SectorLifecycle.sector_code,
                SectorLifecycle.lifecycle_state,
                SectorLifecycle.state_score,
                SectorLifecycle.limit_up_count,
                SectorLifecycle.consecutive_board_count,
                SectorLifecycle.fund_flow,
                SectorLifecycle.active_days,
                SectorLifecycle.kline_trend,
            ).where(SectorLifecycle.trade_date == target_date)
        )
        for row in lifecycle_result.all():
            state = str(row[1] or "").strip().lower()
            sector_lifecycle_map[row[0]] = state
            sector_lifecycle_detail_map[row[0]] = {
                "state": state,
                "state_score": _safe_float(row[2]),
                "limit_up_count": _safe_int(row[3]),
                "consecutive_board_count": _safe_int(row[4]),
                "fund_flow": _safe_float(row[5]),
                "active_days": _safe_int(row[6]),
                "kline_trend": str(row[7] or ""),
            }
    except Exception:
        pass

    # ===== 4.5 板块共振数据 — 延迟到candidate_codes定义后再获取 =====
    sector_resonance_map = {}

    # ===== 4.6 获取大盘环境/情绪 =====
    market_env = "neutral"
    sentiment_cycle = ""
    try:
        market_env, sentiment_cycle = await _judge_market_env(db, target_date)
    except Exception:
        pass
    market_breadth = await _get_market_breadth_context(db, target_date)
    # 需要重新算: 用 _bull_rank 的 tech_map 逻辑但只算 Top N
    # 优化: 直接从stock_spot取已有字段(ma5/ma10/ma20/ma60/boll_*) + 补算macd/rsi/kdj
    import numpy as np
    from sqlalchemy import text

    # 筛选 level >= B 的候选(评分过低不做预案)
    candidates = [r for r in rank_list if r.get("level") in ("S", "A", "B")]
    # 如果B级以上不足10只, 也加入C级前10只
    if len(candidates) < 10:
        c_level = [r for r in rank_list if r.get("level") == "C"][:10]
        candidates.extend(c_level)

    # 补充主升浪形态候选: 不依赖涨停池/强势排行，捕捉多板趋势与加速段相似结构。
    try:
        existing_codes = {str(item.get("code") or "") for item in candidates if item.get("code")}
        main_wave_candidates = await _load_main_wave_plan_candidates(
            db,
            target_date,
            set(),
            limit=max(limit * 4, 80),
        )
        if main_wave_candidates:
            candidates.extend(main_wave_candidates)
            candidates = _merge_plan_candidates_by_code(candidates)
            added_count = len({c["code"] for c in main_wave_candidates} - existing_codes)
            logger.info(
                f"[next_day_plan] 补充K线形态候选 {len(main_wave_candidates)} 只, 新增 {added_count} 只"
            )
    except Exception as e:
        logger.warning(f"[next_day_plan] 主升浪形态候选加载失败: {e}")

    # 十倍潜力榜只承担“研究候选”职责；与健康趋势交叉后才进入0仓位回踩池，
    # 盘中仍须支撑回收、滚动60秒增量成交及盘口/资金/板块确认，避免追高。
    try:
        potential_pullback_candidates = await _load_tenbagger_pullback_plan_candidates(
            db,
            target_date,
            set(),
            limit=max(12, min(30, limit)),
        )
        if potential_pullback_candidates:
            candidates.extend(potential_pullback_candidates)
            candidates = _merge_plan_candidates_by_code(candidates)
            logger.info(
                f"[next_day_plan] 补充牛股潜质回踩检测池 {len(potential_pullback_candidates)} 只"
            )
    except Exception as e:
        logger.warning(f"[next_day_plan] 牛股潜质回踩候选加载失败: {e}")

    # 盘中真正发送成功的A2强修复不能在次日预案里消失；这里每次仅查询当天记录，
    # 并复用后续批量K线/风控链路，不增加扫描循环或飞书发送频率。
    try:
        sent_repair_candidates = await _load_sent_repair_plan_candidates(db, target_date)
        if sent_repair_candidates:
            candidates.extend(sent_repair_candidates)
            candidates = _merge_plan_candidates_by_code(candidates)
            logger.info(f"[next_day_plan] 带入已发送A2强修复 {len(sent_repair_candidates)} 只")
    except Exception as e:
        logger.warning(f"[next_day_plan] A2强修复候选加载失败: {e}")

    # 个股形态评分会漏掉“板块先形成涨停扩散、后排午后才点火”的标的。
    # 从强主线反向扫描至少两个有效产业映射的未涨停成员，只放入0仓位动态池。
    try:
        sector_core_laggards = await _load_sector_core_laggard_plan_candidates(
            db,
            target_date,
            limit=max(24, min(48, limit * 2)),
        )
        if sector_core_laggards:
            candidates.extend(sector_core_laggards)
            candidates = _merge_plan_candidates_by_code(candidates)
            logger.info(
                f"[next_day_plan] 补充强主线核心补涨检测池 {len(sector_core_laggards)} 只"
            )
    except Exception as e:
        logger.warning(f"[next_day_plan] 强主线核心补涨候选加载失败: {e}")

    # 直接公司公告先与“当时已经存在”的技术候选交叉，再单独处理涨停接力。
    # 这样盘后披露利好的低位趋势股可以进入明日确认层，但不会用当日涨停
    # 结果倒灌普通候选，也不会把收盘价直接包装成买点。
    event_catalyst_map: dict[str, dict] = {}
    event_limit_up_context_map: dict[str, dict] = {}
    try:
        event_limit_up_codes = set((await db.execute(
            select(LimitUpPool.code).where(LimitUpPool.trade_date == target_date)
        )).scalars().all())
        technical_candidate_codes = {
            str(item.get("code") or "") for item in candidates if item.get("code")
        }
        event_catalyst_map = await load_direct_stock_catalyst_map(
            db,
            target_date,
            candidate_codes=event_limit_up_codes | technical_candidate_codes,
            limit=max(1200, (len(event_limit_up_codes) + len(technical_candidate_codes)) * 12),
            news_end_time=datetime.now() if target_date == date.today() else None,
        )
        candidates = _enrich_plan_candidates_with_direct_catalysts(
            candidates,
            event_catalyst_map,
        )
        event_candidates, event_limit_up_context_map = await _load_event_limit_up_plan_candidates(
            db,
            target_date,
            event_catalyst_map,
        )
        event_limit_up_context_map = {
            **event_catalyst_map,
            **event_limit_up_context_map,
        }
        if event_candidates:
            candidates.extend(event_candidates)
            candidates = _merge_plan_candidates_by_code(candidates)
            logger.info(f"[next_day_plan] 补充直接事件涨停候选 {len(event_candidates)} 只")
    except Exception as e:
        logger.warning(f"[next_day_plan] 事件涨停候选加载失败: {e}")

    # 不能只从“盈利榜/趋势榜”推预案：全量读取当日主板涨停梯队，按事件、
    # 题材换手、情绪记忆、业绩预期差和断板二波五条高标路线分别归因。
    try:
        high_board_candidates = await _load_high_board_plan_candidates(
            db,
            target_date,
            event_catalyst_map,
            limit=max(60, limit * 4),
        )
        if high_board_candidates:
            candidates.extend(high_board_candidates)
            candidates = _merge_plan_candidates_by_code(candidates)
            logger.info(f"[next_day_plan] 补充五类高标候选 {len(high_board_candidates)} 只")
    except Exception as e:
        logger.warning(f"[next_day_plan] 五类高标候选加载失败: {e}")

    # ST数据只进入独立研究层。即便命中同样的高标形态，也不会借此绕过
    # StockTag、模拟盘或交易风控；行情不新鲜时本加载器直接不产出候选。
    try:
        st_high_board_candidates = await _load_st_high_board_watch_candidates(
            db,
            target_date,
            limit=max(12, min(30, limit * 2)),
        )
        if st_high_board_candidates:
            candidates.extend(st_high_board_candidates)
            candidates = _merge_plan_candidates_by_code(candidates)
            logger.info(f"[next_day_plan] 补充ST高标研究候选 {len(st_high_board_candidates)} 只")
    except Exception as e:
        logger.warning(f"[next_day_plan] ST高标研究候选加载失败: {e}")

    # 晋级预测与牛股雷达共用同一套首板质量闸门，不再只展示“有新闻的涨停”。
    try:
        relay_candidates, relay_context_map = await _load_second_board_relay_plan_candidates(
            db,
            target_date,
        )
        if relay_candidates:
            candidates.extend(relay_candidates)
            candidates = _merge_plan_candidates_by_code(candidates)
            event_limit_up_context_map.update(relay_context_map)
            logger.info(f"[next_day_plan] 补充首板冲二板确认池 {len(relay_candidates)} 只")
    except Exception as e:
        logger.warning(f"[next_day_plan] 冲二板候选加载失败: {e}")

    # 龙头辨识与可交易性分离：A已经确认后，仅把低位、未涨停的B加入盘中二次确认池。
    try:
        dragon_snapshot = await prewarm_dragon_snapshot(
            db,
            target_date,
            # 明日预案刷新不应连带重跑昂贵的全市场龙头扫描；同交易日龙头
            # 快照已有独立刷新节奏，缺失时该函数仍会自行计算。
            force_refresh=False,
        )
        linkage_candidates = await _load_leader_linkage_plan_candidates(
            db,
            target_date,
            list(dragon_snapshot.get("dragons") or []),
        )
        if linkage_candidates:
            candidates.extend(linkage_candidates)
            candidates = _merge_plan_candidates_by_code(candidates)
            logger.info(f"[next_day_plan] 补充看A做B候选 {len(linkage_candidates)} 只")
    except Exception as e:
        logger.warning(f"[next_day_plan] 看A做B候选加载失败: {e}")

    # 明日预案只保留沪深主板；ST只能以独立研究候选进入，名称含“退”继续屏蔽。
    candidates = [_classify_high_board_candidate(item) for item in candidates]
    candidates = [
        item for item in candidates
        if _is_main_board_code(str(item.get("code") or ""))
        and not str(item.get("name") or "").upper().startswith("退")
        and (
            (
                stock_tagger.is_tradeable(str(item.get("code") or ""))
                and not _is_st_name(str(item.get("name") or ""))
            )
            or (
                item.get("research_only")
                and item.get("is_st")
                and item.get("is_high_board_candidate")
            )
        )
    ]
    for item in candidates:
        item["is_capacity_trend"] = _is_capacity_trend_candidate(item)

    trend_pool_candidates = _select_dynamic_trend_pool_candidates(
        [
            item for item in candidates
            if not item.get("research_only") and not item.get("is_capacity_trend")
        ],
        limit=100,
    )
    anomaly_scanner.update_dynamic_trend_pool(trend_pool_candidates)

    if not candidates:
        payload = {
            "plans": [],
            "trend_pool": [],
            "snapshot_time": datetime.now().isoformat(),
            "trade_date": str(target_date),
            "market_breadth": market_breadth,
        }
        await _persist_dashboard_snapshot(db, snapshot_key, target_date, payload)
        _set_cached_payload(_PLAN_CACHE, snapshot_key, target_date, payload)
        return payload

    # 批量获取K线(算MACD/RSI/KDJ, 只算候选池)
    candidate_codes = list(dict.fromkeys(c["code"] for c in candidates if c.get("code")))
    tech_map = {}

    # ===== 4.5补: 批量获取板块共振数据(此时candidate_codes已有) =====
    try:
        resonance_result = await db.execute(
            select(
                StockSectorMapping.code,
                SectorPersistence.sector_code,
                SectorPersistence.change_pct,
                SectorPersistence.fund_flow,
                SectorPersistence.strength_score,
                SectorPersistence.consecutive_days,
                StockSectorMapping.sector_name,
                StockSectorMapping.sector_type,
                StockSectorMapping.source,
            ).join(
                SectorPersistence,
                StockSectorMapping.sector_code == SectorPersistence.sector_code,
            ).where(
                SectorPersistence.trade_date == target_date,
                StockSectorMapping.code.in_(candidate_codes),
            ).order_by(
                StockSectorMapping.code,
                desc(SectorPersistence.strength_score),
            )
        )
        stock_change_map = {
            str(item.get("code") or ""): _safe_float(item.get("change_pct"))
            for item in candidates
        }
        lifecycle_rank = {
            "accelerating": 4,
            "emerging": 3,
            "climax": 0,
            "dormant": -1,
            "diverging": -2,
            "declining": -3,
            "one_day": -3,
        }
        best_by_code: dict[str, tuple[tuple, tuple]] = {}
        for row in resonance_result.all():
            (
                r_code,
                sp_code,
                sp_chg,
                sp_fund,
                sp_score,
                sp_days,
                sp_name,
                mapping_type,
                mapping_source,
            ) = row
            if not _is_causal_trade_driver_sector({
                "sector_name": sp_name,
                "sector_type": mapping_type,
                "source": mapping_source,
            }):
                continue
            lifecycle = sector_lifecycle_detail_map.get(sp_code) or {}
            state = str(lifecycle.get("state") or "")
            selection_rank = (
                lifecycle_rank.get(state, -4),
                1 if _safe_float(sp_fund) > 0 else 0,
                1 if _safe_float(sp_chg) > 0 else 0,
                _safe_float(sp_score),
                _safe_float(sp_fund),
                _safe_float(getattr(row, "weight", 0)),
            )
            current = best_by_code.get(r_code)
            if current is None or selection_rank > current[0]:
                best_by_code[r_code] = (selection_rank, row)

        for r_code, (_rank, row) in best_by_code.items():
            (
                _r_code,
                sp_code,
                sp_chg,
                sp_fund,
                sp_score,
                sp_days,
                sp_name,
                _mapping_type,
                _mapping_source,
            ) = row
            stock_chg = stock_change_map.get(r_code, 0.0)
            lifecycle = sector_lifecycle_detail_map.get(sp_code) or {}
            state = str(lifecycle.get("state") or "")
            positive_lifecycle = state in _NEXT_DAY_ALLOWED_SECTOR_LIFECYCLES
            if positive_lifecycle and _safe_float(sp_score) >= 70 and _safe_float(sp_chg) > 0 and _safe_float(sp_fund) > 0:
                resonance = "强共振"
            elif positive_lifecycle and _safe_float(sp_score) >= 40 and _safe_float(sp_chg) > 0 and _safe_float(sp_fund) > 0:
                resonance = "弱共振"
            elif _safe_float(sp_chg) < -2 and stock_chg > 0:
                resonance = "逆势"
            else:
                resonance = "独立行情"
            code_sector_map[r_code] = sp_code
            sector_resonance_map[r_code] = {
                "resonance": resonance,
                "score": _safe_float(sp_score),
                "name": sp_name or "",
                "change": _safe_float(sp_chg),
                "fund": _safe_float(sp_fund),
                "lifecycle": state,
                "lifecycle_label": _SECTOR_LIFECYCLE_LABELS.get(state, state),
                "lifecycle_score": _safe_float(lifecycle.get("state_score")),
                "active_days": _safe_int(lifecycle.get("active_days")),
                "kline_trend": str(lifecycle.get("kline_trend") or ""),
                "consecutive_days": _safe_int(sp_days),
                "sector_driver_causal": True,
            }
    except Exception as e:
        logger.debug(f"[next_day_plan] 板块共振数据获取失败: {e}")

    if candidate_codes:
        try:
            date_result = await db.execute(
                text("""
                    SELECT DISTINCT trade_date FROM stock_kline
                    WHERE trade_date <= :target_date
                    ORDER BY trade_date DESC
                    LIMIT 120
                """),
                {"target_date": str(target_date)},
            )
            recent_dates = [r[0] for r in date_result.all()]

            if recent_dates:
                from collections import defaultdict
                code_params = {f"code_{i}": c for i, c in enumerate(candidate_codes)}
                code_placeholders = ", ".join([f":code_{i}" for i in range(len(candidate_codes))])
                date_params = {f"date_{i}": str(d) for i, d in enumerate(recent_dates)}
                date_placeholders = ", ".join([f":date_{i}" for i in range(len(recent_dates))])
                all_params = {**code_params, **date_params}

                batch_result = await db.execute(
                    text(f"""
                        SELECT code, close, high, low FROM stock_kline
                        WHERE code IN ({code_placeholders})
                        AND trade_date IN ({date_placeholders})
                        ORDER BY code, trade_date ASC
                    """),
                    all_params,
                )

                code_klines = defaultdict(list)
                for row in batch_result.all():
                    code_klines[row[0]].append((row[1], row[2], row[3]))

                for code, klines in code_klines.items():
                    if len(klines) < 5:
                        continue
                    closes = np.array([k[0] for k in klines if k[0]])
                    highs = np.array([k[1] for k in klines if k[1]])
                    lows = np.array([k[2] for k in klines if k[2]])
                    tech = {}

                    # MA
                    if len(closes) >= 5:  tech["ma5"] = round(float(closes[-5:].mean()), 2)
                    if len(closes) >= 10: tech["ma10"] = round(float(closes[-10:].mean()), 2)
                    if len(closes) >= 20:
                        tech["ma20"] = round(float(closes[-20:].mean()), 2)
                        std20 = float(closes[-20:].std(ddof=0))
                        tech["boll_upper"] = round(tech["ma20"] + 2 * std20, 2)
                        tech["boll_mid"] = tech["ma20"]
                        tech["boll_lower"] = round(tech["ma20"] - 2 * std20, 2)
                    if len(closes) >= 60:  tech["ma60"] = round(float(closes[-60:].mean()), 2)

                    if len(highs) >= 20:  tech["high_20d"] = round(float(highs[-20:].max()), 2)
                    if len(highs) >= 60:  tech["high_60d"] = round(float(highs[-60:].max()), 2)

                    # MACD
                    if len(closes) >= 35:
                        ema12 = _calc_ema(closes, 12)
                        ema26 = _calc_ema(closes, 26)
                        dif = ema12 - ema26
                        valid_start = 25
                        dif_valid = dif[valid_start:]
                        dea_valid = _calc_ema(dif_valid, 9)
                        macd_hist_valid = 2 * (dif_valid - dea_valid)
                        if len(macd_hist_valid) >= 2:
                            hist_vals = macd_hist_valid[~np.isnan(macd_hist_valid)]
                            if len(hist_vals) >= 2:
                                if hist_vals[-1] > 0 and hist_vals[-2] <= 0:
                                    tech["macd_signal"] = "golden_cross"
                                elif hist_vals[-1] < 0 and hist_vals[-2] >= 0:
                                    tech["macd_signal"] = "death_cross"

                    # RSI
                    if len(closes) >= 15:
                        tech["rsi14"] = round(float(_calc_rsi(closes, 14)), 1)

                    # KDJ
                    if len(closes) >= 20 and len(highs) >= 20 and len(lows) >= 20:
                        kdj = _calc_kdj(highs, lows, closes, 9, 3, 3)
                        if kdj:
                            k_vals, d_vals = kdj
                            if len(k_vals) >= 2 and len(d_vals) >= 2:
                                kd_diff = abs(k_vals[-1] - d_vals[-1])
                                if k_vals[-1] > d_vals[-1] and k_vals[-2] <= d_vals[-2] and kd_diff > 5:
                                    tech["kdj_signal"] = "golden_cross"
                                elif k_vals[-1] < d_vals[-1] and k_vals[-2] >= d_vals[-2] and kd_diff > 5:
                                    tech["kdj_signal"] = "death_cross"

                    tech_map[code] = tech
        except Exception as e:
            logger.warning(f"[next_day_plan] 批量K线计算失败: {e}")

    # ===== 6. 批量获取spot详情(补充open/high/low等) =====
    spot_detail_map = {}
    try:
        spot_result = await db.execute(
            select(StockSpot).where(StockSpot.code.in_(candidate_codes))
        )
        for spot in spot_result.scalars().all():
            spot_detail_map[spot.code] = spot
    except Exception:
        pass

    fund_plan_window = await load_main_fund_window(
        db, through_date=target_date, decision_at=fund_decision_at, codes=candidate_codes,
    )
    fund_5d_plan_map = {code: item["total"] for code, item in fund_plan_window["items"].items()}

    limit_up_consecutive_map = {}
    try:
        lu_plan_result = await db.execute(
            select(LimitUpPool.code, LimitUpPool.consecutive_days)
            .where(LimitUpPool.trade_date == target_date)
        )
        limit_up_consecutive_map = {row[0]: _safe_int(row[1]) for row in lu_plan_result.all()}
    except Exception:
        pass

    # ===== 7. 逐只生成预案 =====
    plans = []
    for item in candidates:
        code = item["code"]
        try:
            spot = spot_detail_map.get(code)
            # 旧调度器没有更新ST行情；ST研究候选已对腾讯原始字段[30]做过
            # target_date校验，必须优先用该兜底，不能再被数据库陈旧spot覆盖。
            if item.get("research_only") and item.get("spot_fallback"):
                spot = SimpleNamespace(**dict(item.get("spot_fallback") or {}))
            elif spot is None and item.get("spot_fallback"):
                spot = SimpleNamespace(**dict(item.get("spot_fallback") or {}))
            tech = tech_map.get(code, {})
            sector_code = code_sector_map.get(code, "")
            sector_chg = sector_change_map.get(sector_code, 0)
            sector_lc = sector_lifecycle_map.get(sector_code, "")

            # 5日资金
            fund_5d = fund_5d_plan_map.get(code)

            # 连板
            consecutive_days = limit_up_consecutive_map.get(code, 0)

            is_limit_up = (spot and spot.limit_up and spot.price and spot.price >= spot.limit_up) if spot else False
            is_one_word = is_limit_up and (spot.open and spot.open >= spot.limit_up) if spot else False

            # 调用策略引擎
            plan_result = next_day_plan_engine.generate(
                code=code,
                name=item.get("name", code),
                bull_level=item.get("level", "D"),
                bull_score=item.get("total_score", 0),
                price=spot.price if spot else 0,
                change_pct=item.get("change_pct", 0),
                dimensions=item.get("dimensions", {}),
                top_signals=item.get("top_signals", []),
                risk_warnings=item.get("risk_warnings", []),
                # 技术指标
                ma5=tech.get("ma5", 0),
                ma10=tech.get("ma10", 0),
                ma20=tech.get("ma20", 0),
                ma60=tech.get("ma60", 0),
                boll_upper=tech.get("boll_upper", 0),
                boll_mid=tech.get("boll_mid", 0),
                boll_lower=tech.get("boll_lower", 0),
                high_20d=tech.get("high_20d", 0),
                high_60d=tech.get("high_60d", 0),
                macd_signal=tech.get("macd_signal", ""),
                rsi14=tech.get("rsi14"),
                kdj_signal=tech.get("kdj_signal", ""),
                # V2.2指标
                volume_ratio=item.get("volume_ratio", 1.0),
                volume_ratio_level=item.get("volume_ratio_level", ""),
                turnover=item.get("turnover", 0),
                turnover_level=item.get("turnover_level", ""),
                price_volume_relation=item.get("price_volume_relation", ""),
                # 资金
                main_net_inflow_billion=item.get("main_net_inflow_billion", 0),
                fund_5d_billion=fund_5d / 1e8 if fund_5d is not None else None,
                fund_5d_complete=fund_5d is not None,
                # 连板/涨停
                consecutive_days=consecutive_days,
                is_limit_up=is_limit_up,
                is_one_word=is_one_word,
                # 筹码
                chip_signal=item.get("chip_signal", 0),
                # 板块
                sector_change_pct=sector_chg,
                sector_lifecycle=sector_lc,
                sector_lifecycle_state=sector_lc,
                # 板块共振
                sector_resonance=sector_resonance_map.get(code, {}).get("resonance", ""),
                sector_resonance_score=sector_resonance_map.get(code, {}).get("score", 0),
                sector_resonance_name=sector_resonance_map.get(code, {}).get("name", ""),
                sector_resonance_change=sector_resonance_map.get(code, {}).get("change", 0),
                sector_resonance_fund=sector_resonance_map.get(code, {}).get("fund", 0),
                # 情绪面
                market_environment=market_env,
                sentiment_cycle=sentiment_cycle,
                # 其他
                circ_market_cap=item.get("circ_market_cap_billion", 0),
                pe_ttm=item.get("pe_ttm", 0),
                open_price=spot.open if spot else 0,
                high=spot.high if spot else 0,
                low=spot.low if spot else 0,
            )

            # 转为可序列化dict
            plan_dict = _plan_result_to_dict(plan_result)
            plan_dict.update(fund_window_payload(fund_plan_window, code))
            if fund_5d is None:
                plan_dict["fund_5d_direction"] = "数据不足"
            plan_dict["market_breadth"] = market_breadth
            plan_dict["market_regime"] = market_breadth.get("market_regime", "neutral")
            plan_dict["market_regime_label"] = market_breadth.get("market_regime_label", "中性/震荡")
            plan_dict["market_regime_reason"] = market_breadth.get("market_regime_reason", "")
            resonance_context = sector_resonance_map.get(code, {})
            plan_dict["sector_lifecycle_state"] = resonance_context.get("lifecycle", "")
            plan_dict["sector_lifecycle_label"] = resonance_context.get("lifecycle_label", "")
            plan_dict["sector_lifecycle_score"] = resonance_context.get("lifecycle_score", 0)
            plan_dict["sector_active_days"] = resonance_context.get("active_days", 0)
            plan_dict["sector_kline_trend"] = resonance_context.get("kline_trend", "")
            plan_dict["sector_driver_causal"] = bool(
                resonance_context.get("sector_driver_causal")
                and resonance_context.get("resonance") in {"强共振", "弱共振"}
            )
            if market_breadth.get("direct_buy_ok"):
                plan_dict["top_signals"] = list(dict.fromkeys(
                    ["市场极强窗口"] + list(plan_dict.get("top_signals") or [])
                ))[:8]
            else:
                blockers = list(market_breadth.get("direct_buy_blockers") or [])
                market_note = (
                    f"市场{plan_dict['market_regime_label']}未进入v31极强窗口，"
                    f"{blockers[0] if blockers else '明日直接买点降级'}"
                )
                for key in ("risk_warnings", "notes"):
                    values = list(plan_dict.get(key) or [])
                    values.append(market_note)
                    plan_dict[key] = list(dict.fromkeys(values))
            theme_signals: list[str] = []
            resonance = resonance_context.get("resonance", "")
            if resonance in ("强共振", "弱共振"):
                sector_name = resonance_context.get("name") or "题材"
                theme_signals.append(f"题材合力:{sector_name}{resonance}")
            if _safe_float(resonance_context.get("fund")) > 5:
                theme_signals.append(f"板块资金{_safe_float(resonance_context.get('fund')):.1f}亿")
            if _safe_int(resonance_context.get("score")) >= 70:
                theme_signals.append(f"板块强度{_safe_float(resonance_context.get('score')):.0f}")
            if resonance_context.get("lifecycle_label"):
                theme_signals.append(f"板块{resonance_context.get('lifecycle_label')}")
            if theme_signals:
                plan_dict["top_signals"] = list(dict.fromkeys(theme_signals + list(plan_dict.get("top_signals") or [])))[:8]
            if item.get("candidate_source"):
                plan_dict["candidate_source"] = item.get("candidate_source")
                plan_dict["candidate_source_label"] = item.get("candidate_source_label", "")
                plan_dict["main_wave_score"] = item.get("main_wave_score", 0)
                plan_dict["main_wave_stats"] = item.get("main_wave_stats", {})
                for field in (
                    "high_board_types",
                    "high_board_type_labels",
                    "high_board_primary_type",
                    "high_board_primary_type_label",
                    "high_board_reasons",
                    "is_high_board_candidate",
                    "is_capacity_trend",
                    "is_st",
                    "research_only",
                    "tag",
                ):
                    if field in item:
                        plan_dict[field] = item.get(field)
                # 同股可能同时属于“长期趋势底座”和“当日首阴回踩”。合并候选
                # 保留展示主来源，但独立传递分钟级动态来源，供Top20赛道预留
                # 与飞书扫描使用；不能因主来源得分更高而丢掉短时效买点记忆。
                if item.get("dynamic_pool_source"):
                    plan_dict["dynamic_pool_source"] = item.get("dynamic_pool_source")
                    plan_dict["dynamic_pool_label"] = item.get("dynamic_pool_label", "")
                    plan_dict["dynamic_pool_score"] = item.get("dynamic_pool_score", 0)
                    plan_dict["dynamic_pool_stats"] = item.get("dynamic_pool_stats", {})
                # 冲二板赛道的因果板块已经按“涨停原因→行业/概念语义相关性”
                # 在晋级模块校验过。通用预案的多板块映射可能挑中当日更强但不相干
                # 的泛概念，因此此处只展示已校验的首板驱动板块；在次日盘中重新
                # 取得板块资金/扩散数据前，保持“独立行情”，不虚构强共振。
                if item.get("candidate_source") == "second_board_relay":
                    relay_stats = dict(item.get("main_wave_stats") or {})
                    relay_sector_name = str(relay_stats.get("sector_name") or "").strip()
                    if relay_sector_name:
                        plan_dict["sector_resonance"] = "独立行情"
                        plan_dict["sector_resonance_name"] = relay_sector_name
                        plan_dict["sector_resonance_score"] = _safe_float(
                            relay_stats.get("sector_strength_score")
                        )
                        plan_dict["sector_resonance_change"] = 0
                        plan_dict["sector_resonance_fund"] = 0
                        plan_dict["sector_driver_causal"] = False
            if code in event_limit_up_context_map:
                plan_dict["event_catalyst"] = event_limit_up_context_map[code]
            plans.append(plan_dict)

        except Exception as e:
            logger.warning(f"[next_day_plan] {code} 生成失败: {e}")

    st_research_plans = [
        item for item in plans
        if item.get("research_only") and (item.get("is_st") or _is_st_name(str(item.get("name") or "")))
    ]
    normal_plans = [item for item in plans if item not in st_research_plans]
    filtered = await stock_tagger.filter_signals(db, normal_plans)
    if st_research_plans:
        filtered.extend(await stock_tagger.filter_signals(
            db,
            st_research_plans,
            exclude_blocked=False,
            exclude_suspended=True,
        ))
    filtered = [_mark_st_plan_research_only(item) for item in filtered]
    filtered = [
        _apply_next_day_prediction_quality(
            _apply_next_day_execution_risk_gate(
                _downgrade_observe_only_plan(
                    _apply_event_limit_up_protocol(
                        _apply_high_board_watch_protocol(
                            _apply_sector_core_laggard_protocol(
                                _apply_leader_linkage_protocol(
                                    _apply_repair_followup_entry_protocol(
                                        _apply_ultra_short_entry_protocol(
                                            _apply_trend_driver_entry_protocol(
                                                _downgrade_backtested_low_edge_plan(item)
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
        for item in filtered
    ]
    filtered = [item for item in filtered if not item.get("is_capacity_trend")]
    filtered = _cap_daily_direct_buy_plans(filtered)
    filtered.sort(key=_next_day_plan_sort_key)

    payload = {
        "plans": _diversified_actionable_top(filtered, limit),
        "trend_pool": [
            {
                "code": item.get("code"),
                "name": item.get("name"),
                "score": item.get("main_wave_score"),
                "label": item.get("candidate_source_label"),
                "candidate_source": item.get("candidate_source"),
                "setup_state": (item.get("main_wave_stats") or {}).get("setup_state"),
                "support": (item.get("main_wave_stats") or {}).get("support"),
                "resistance": (item.get("main_wave_stats") or {}).get("resistance"),
                "main_wave_stats": item.get("main_wave_stats") or {},
            }
            for item in trend_pool_candidates[:100]
        ],
        "snapshot_time": datetime.now().isoformat(),
        "trade_date": str(target_date),
        "market_breadth": market_breadth,
    }
    await _persist_dashboard_snapshot(db, snapshot_key, target_date, payload)
    _set_cached_payload(_PLAN_CACHE, snapshot_key, target_date, payload)
    return payload


# =========================================================================
# Tab 3: 龙头追踪
# =========================================================================

@router.get("/dragon")
async def dragon_head_scan(
    sector_code: str = Query("", description="板块代码(空=全部)"),
    force_refresh: bool = Query(False, description="是否强制刷新快照"),
    db: AsyncSession = Depends(get_db),
):
    """龙头股扫描"""
    snapshot = await prewarm_dragon_snapshot(db, None, force_refresh=force_refresh)
    dragons = _filter_main_board_items(list(snapshot.get("dragons") or []))

    if sector_code:
        dragons = [d for d in dragons if d.get("sector_code") == sector_code]

    return {
        "dragons": dragons[:50],
        "snapshot_time": snapshot.get("snapshot_time"),
        "trade_date": snapshot.get("trade_date"),
        "served_from_snapshot": bool(snapshot.get("served_from_snapshot")),
        "snapshot_stale": bool(snapshot.get("snapshot_stale")),
    }


# =========================================================================
# Tab 4: 共振分析
# =========================================================================

@router.get("/resonance")
async def resonance_analysis(
    code: str = Query("", description="股票代码"),
    db: AsyncSession = Depends(get_db),
):
    """个股-板块共振分析"""
    if not code:
        result = await db.execute(
            select(StockSpot.code).where(
                StockSpot.change_pct > 5,
                StockSpot.price > 0,
            ).limit(30)
        )
        codes = stock_tagger.filter_tradeable([r[0] for r in result.all()])
    else:
        codes = [code]

    target_date = await resolve_latest_trade_date(db, SectorPersistence.trade_date)
    results = []
    for c in codes:
        try:
            res = await anomaly_scanner.analyze_resonance(c, db, target_date=target_date)
            results.append(res)
        except Exception as e:
            logger.warning(f"[resonance] {c} 分析失败: {e}")

    return {"resonance": results}


# =========================================================================
# Tab 5: 明日预案
# =========================================================================

@router.get("/next-day-plan")
async def next_day_plan(
    limit: int = Query(20, ge=1, le=500, description="返回数量"),
    force_refresh: bool = Query(False, description="是否强制刷新快照"),
    db: AsyncSession = Depends(get_db),
):
    """明日购买预案"""
    if force_refresh:
        snapshot = await prewarm_next_day_plan_snapshot(
            db,
            None,
            force_refresh=True,
            limit=limit,
        )
        snapshot = {**snapshot, "served_from_snapshot": False, "snapshot_stale": False}
    else:
        target_date = await resolve_latest_trade_date(db, SectorPersistence.trade_date)
        snapshot = await _get_latest_compatible_plan_snapshot(db, target_date, limit)
        if snapshot is None:
            snapshot = await prewarm_next_day_plan_snapshot(
                db,
                target_date,
                force_refresh=False,
                limit=limit,
            )
            snapshot = {**snapshot, "served_from_snapshot": False, "snapshot_stale": False}

    trend_pool = _filter_main_board_items(snapshot.get("trend_pool") or [])
    anomaly_scanner.update_dynamic_trend_pool(trend_pool)
    pattern_pool = await _build_pattern_pool_rows(
        db,
        trend_pool,
        target_date=date.fromisoformat(str(snapshot.get("trade_date")))
        if snapshot.get("trade_date")
        else None,
    )
    snapshot_trade_date = (
        date.fromisoformat(str(snapshot.get("trade_date")))
        if snapshot.get("trade_date")
        else None
    )
    latest_replay = (
        await _build_next_day_plan_replay(
            db,
            snapshot_trade_date,
            limit=limit,
        )
        if snapshot_trade_date is not None
        else {"status": "missing_actual_trade_date", "predicted_count": 0}
    )
    return {
        "plans": _filter_plan_main_board_items(snapshot.get("plans") or []),
        "pattern_pool": _filter_main_board_items(pattern_pool),
        "snapshot_time": snapshot.get("snapshot_time"),
        "trade_date": snapshot.get("trade_date"),
        "market_breadth": snapshot.get("market_breadth") or _empty_market_breadth_context(),
        "latest_replay": latest_replay,
        "served_from_snapshot": bool(snapshot.get("served_from_snapshot")),
        "snapshot_age_seconds": _safe_float(snapshot.get("snapshot_age_seconds")),
        "snapshot_stale": bool(snapshot.get("snapshot_stale")),
    }


# =========================================================================
# 个股评分(详情页用)
# =========================================================================

@router.get("/{code}/score")
async def stock_score(code: str, db: AsyncSession = Depends(get_db)):
    """个股综合评分(短线+十倍潜力)"""
    target_date = await resolve_latest_trade_date(db, SectorPersistence.trade_date)
    try:
        current_fund_snapshot = await prewarm_eastmoney_main_fund_snapshot(db, trade_date=target_date)
    except Exception:
        current_fund_snapshot = {"source": "eastmoney_main_fund_unavailable", "items": {}}
    plan_dates = await _resolve_stock_plan_dates(db, code, target_date)
    technical_trade_date = plan_dates.get("technical_trade_date")
    fund_context = await _load_stock_fund_context(
        db,
        code,
        quote_trade_date=target_date,
        completed_trade_date=technical_trade_date,
        current_snapshot=current_fund_snapshot,
    )

    spot_result = await db.execute(
        select(StockSpot).where(StockSpot.code == code)
    )
    spot = spot_result.scalar_one_or_none()

    name = spot.name if spot else code

    # 近5个完整交易日资金；5日涨幅需六个收盘价才是五个区间。
    fund_5d = _safe_float(fund_context.get("fund_5d_total"))
    change_5d_context = await _load_stock_change_5d(db, code, technical_trade_date)
    change_5d = _safe_float(change_5d_context.get("change_pct"))

    # 技术指标
    tech = {}
    if spot and technical_trade_date is not None:
        tech = await anomaly_scanner._calc_technical_indicators(
            code,
            db,
            target_date=technical_trade_date,
        )

    is_limit_up = (spot and spot.limit_up and spot.price and spot.price >= spot.limit_up)
    is_one_word = is_limit_up and spot and spot.open and spot.open >= spot.limit_up

    consecutive_days = 0
    lu_result = await db.execute(
        select(LimitUpPool).where(
            LimitUpPool.code == code,
            LimitUpPool.trade_date == target_date,
        )
    )
    lu = lu_result.scalar_one_or_none()
    if lu:
        consecutive_days = lu.consecutive_days or 0
    current_fund = fund_context.get("current_item") or {}
    current_main_inflow = _safe_float((current_fund or {}).get("main_net_inflow"))

    # V2.2融合: 筹码分析 — 从K线获取近20日数据
    chip_result = None
    chip_signal = 0.0
    if spot and technical_trade_date is not None:
        try:
            from app.signal.chip_concentration import ChipConcentrationAnalyzer
            _chip_analyzer = ChipConcentrationAnalyzer()

            chip_kline_result = await db.execute(
                select(StockKline.close, StockKline.high, StockKline.low, StockKline.volume, StockKline.turnover, StockKline.change_pct)
                .where(StockKline.code == code)
                .where(StockKline.trade_date <= technical_trade_date)
                .order_by(desc(StockKline.trade_date))
                .limit(20)
            )
            chip_rows = list(reversed(chip_kline_result.all()))
            if len(chip_rows) >= 5:
                chip_result = _chip_analyzer.analyze(
                    code=code, name=name,
                    recent_closes=[r[0] for r in chip_rows if r[0]],
                    recent_highs=[r[1] for r in chip_rows if r[1]],
                    recent_lows=[r[2] for r in chip_rows if r[2]],
                    recent_volumes=[r[3] for r in chip_rows if r[3]],
                    recent_turnovers=[r[4] for r in chip_rows if r[4]],
                    recent_changes=[r[5] for r in chip_rows if r[5]],
                    current_price=spot.price or 0,
                )
                chip_signal = chip_result.signal_strength
        except Exception as e:
            logger.debug(f"[stock_score] {code} 筹码分析失败: {e}")

    bull_result = _bull_model.score_from_spot(
        code=code, name=name,
        change_pct=spot.change_pct if spot else 0,
        volume_ratio=spot.volume_ratio if spot else 1.0,
        turnover=spot.turnover if spot else 0,
        amplitude=spot.amplitude if spot else 0,
        main_net_inflow=current_main_inflow if spot else 0,
        main_net_inflow_pct=_safe_float((current_fund or {}).get("main_net_inflow_pct")) if spot else 0,
        super_net_inflow=_safe_float((current_fund or {}).get("super_net_inflow")) if spot else 0,
        super_net_inflow_pct=_safe_float((current_fund or {}).get("super_net_inflow_pct")) if spot else 0,
        big_net_inflow=_safe_float((current_fund or {}).get("big_net_inflow")) if spot else 0,
        big_net_inflow_pct=_safe_float((current_fund or {}).get("big_net_inflow_pct")) if spot else 0,
        circ_market_cap=spot.circ_market_cap if spot else 0,
        pe_ttm=spot.pe_ttm if spot else 0,
        pb=spot.pb if spot else 0,
        net_profit_growth=spot.net_profit_growth if spot else 0,
        price=spot.price if spot else 0,
        open=spot.open if spot else 0,
        high=spot.high if spot else 0,
        low=spot.low if spot else 0,
        limit_up=spot.limit_up if spot else 0,
        ma5=tech.get("ma5", 0), ma10=tech.get("ma10", 0),
        ma20=tech.get("ma20", 0), ma60=tech.get("ma60", 0),
        boll_upper=tech.get("boll_upper", 0), boll_mid=tech.get("boll_mid", 0),
        boll_lower=tech.get("boll_lower", 0),
        high_20d=tech.get("high_20d", 0), high_60d=tech.get("high_60d", 0),
        macd_signal=tech.get("macd_signal", ""),
        rsi14=tech.get("rsi14"),
        kdj_signal=tech.get("kdj_signal", ""),
        main_net_inflow_5d=fund_5d,
        change_pct_5d=change_5d,
        consecutive_days=consecutive_days,
        is_limit_up=is_limit_up,
        is_one_word=is_one_word,
        chip_signal=chip_signal,
    )

    # 十倍潜力评分
    tenbagger_result = None
    if spot:
        sector_days = 0
        sector_fund = 0
        mapping_result = await db.execute(
            select(StockSectorMapping.sector_code)
            .where(StockSectorMapping.code == code).limit(5)
        )
        sector_codes = [r[0] for r in mapping_result.all()]
        if sector_codes:
            sp_result = await db.execute(
                select(SectorPersistence).where(
                    SectorPersistence.sector_code.in_(sector_codes),
                    SectorPersistence.trade_date == target_date,
                ).order_by(desc(SectorPersistence.strength_score))
            )
            sp = sp_result.first()
            if sp:
                sector_days = sp[0].consecutive_days or 0
                sector_fund = sp[0].fund_flow or 0

        tenbagger_result = tenbagger_model.score(
            code=code, name=name,
            circ_market_cap=spot.circ_market_cap if spot.circ_market_cap is not None else 0,
            net_profit_growth=spot.net_profit_growth if spot.net_profit_growth is not None else 0,
            pe_ttm=spot.pe_ttm if spot.pe_ttm is not None else 0,
            pb=spot.pb if spot.pb is not None else 0,
            sector_consecutive_days=sector_days,
            sector_fund_flow=sector_fund,
            main_net_inflow=current_main_inflow,
            main_net_inflow_pct=_safe_float((current_fund or {}).get("main_net_inflow_pct")),
            super_net_inflow=_safe_float((current_fund or {}).get("super_net_inflow")),
            super_net_inflow_pct=_safe_float((current_fund or {}).get("super_net_inflow_pct")),
            big_net_inflow=_safe_float((current_fund or {}).get("big_net_inflow")),
            big_net_inflow_pct=_safe_float((current_fund or {}).get("big_net_inflow_pct")),
            main_net_inflow_5d=fund_5d,
            change_pct=spot.change_pct if spot.change_pct is not None else 0,
            is_limit_down=bool(spot.limit_down and spot.price and spot.price <= spot.limit_down),
            is_20pct_board=str(code).startswith(("300", "688")),  # 创业板/科创板20%涨跌停
        )

    fund_clock_warning = (
        ["主力资金源时钟未验证或已陈旧，保留评分仅作研究，不代表当前买点"]
        if current_fund.get("is_stale", True) else []
    )
    return {
        "code": code,
        "fund_evidence": {
            **{key: value for key, value in fund_context.items() if key.startswith("fund_5d_")},
            "clock_status": current_fund.get("clock_status", "unknown"),
            "is_stale": bool(current_fund.get("is_stale", True)),
            "source_quote_at": fund_context.get("current_as_of"),
            "historical_unknown_count": int(fund_context.get("fund_5d_clock_unknown_count") or 0),
        },
        "bull": {
            "total_score": bull_result.total_score,
            "level": bull_result.level,
            "dimensions": bull_result.dimensions,
            "top_signals": bull_result.top_signals,
            "risk_warnings": list(bull_result.risk_warnings) + fund_clock_warning,
            "suggestion": bull_result.suggestion,
            # V2.2融合新增
            "volume_ratio_level": bull_result.volume_ratio_level,
            "turnover_level": bull_result.turnover_level,
            "price_volume_relation": bull_result.price_volume_relation,
            "chip_signal": bull_result.chip_signal,
        },
        "tenbagger": {
            "total_score": tenbagger_result.total_score if tenbagger_result else 0,
            "level": tenbagger_result.level if tenbagger_result else "C",
            "dimensions": tenbagger_result.dimensions if tenbagger_result else {},
            "highlights": tenbagger_result.highlights if tenbagger_result else [],
            "risks": tenbagger_result.risks if tenbagger_result else [],
        } if tenbagger_result else None,
        # V2.2融合: 筹码分析详情
        "chip": {
            "score": chip_result.score if chip_result else 0,
            "status": chip_result.status if chip_result else "",
            "description": chip_result.description if chip_result else "",
            "concentration_90": chip_result.concentration_90 if chip_result else 0,
            "concentration_70": chip_result.concentration_70 if chip_result else 0,
            "pattern": chip_result.pattern if chip_result else "",
            "profit_ratio": chip_result.profit_ratio if chip_result else 0,
            "signal_strength": chip_result.signal_strength if chip_result else 0,
            "avg_cost": chip_result.avg_cost if chip_result else 0,
            "support_levels": chip_result.support_levels if chip_result else [],
            "resistance_levels": chip_result.resistance_levels if chip_result else [],
        } if chip_result else None,
    }


# =========================================================================
# 个股共振(详情页用)
# =========================================================================

@router.get("/{code}/resonance")
async def stock_resonance(code: str, db: AsyncSession = Depends(get_db)):
    """个股-板块共振分析"""
    target_date = await resolve_latest_trade_date(db, SectorPersistence.trade_date)
    result = await anomaly_scanner.analyze_resonance(code, db, target_date=target_date)
    return result


# =========================================================================
# 个股明日预案(详情页用) — 复用 stock_score 数据管道
# =========================================================================

@router.get("/{code}/next-day-plan")
async def stock_next_day_plan(code: str, db: AsyncSession = Depends(get_db)):
    """个股明日预案 — 复用 BullScoreModel + NextDayPlanEngine"""
    # 0. 行情日与完整日线严格分离
    target_date = await resolve_latest_trade_date(db, SectorPersistence.trade_date)
    generated_at = datetime.now()
    try:
        current_fund_snapshot = await prewarm_eastmoney_main_fund_snapshot(
            db,
            trade_date=target_date,
        )
    except Exception:
        current_fund_snapshot = {
            "source": "eastmoney_main_fund_unavailable",
            "items": {},
            "snapshot_time": generated_at.isoformat(timespec="seconds"),
        }
    # Freeze the decision after source retrieval, before reading downstream rows.
    generated_at = datetime.now()
    plan_dates = await _resolve_stock_plan_dates(
        db,
        code,
        target_date,
        now=generated_at,
    )
    technical_trade_date = plan_dates.get("technical_trade_date")

    # 1. 获取spot；观察池个股可能没有实时行情，用最新K线做展示/预案兜底
    spot_result = await db.execute(
        select(StockSpot).where(StockSpot.code == code)
    )
    spot = spot_result.scalar_one_or_none()
    if not spot:
        latest_kline_result = await db.execute(
            select(StockKline)
            .where(
                StockKline.code == code,
                StockKline.trade_date <= target_date,
            )
            .order_by(desc(StockKline.trade_date))
            .limit(1)
        )
        latest_kline = latest_kline_result.scalar_one_or_none()
        if not latest_kline:
            return {"code": code, "strategy": "watch", "action": "暂无数据"}

        tag_result = await db.execute(select(StockTag).where(StockTag.code == code))
        tag = tag_result.scalar_one_or_none()
        lu_result = await db.execute(
            select(LimitUpPool).where(
                LimitUpPool.code == code,
                LimitUpPool.trade_date == latest_kline.trade_date,
            )
        )
        latest_limit = lu_result.scalar_one_or_none()
        prev_close = latest_kline.prev_close or latest_kline.close or 0
        amplitude = 0
        if prev_close:
            amplitude = round((float(latest_kline.high or 0) - float(latest_kline.low or 0)) / float(prev_close) * 100, 2)
        spot = SimpleNamespace(
            code=code,
            name=(tag.name if tag else "") or code,
            price=latest_kline.close,
            prev_close=latest_kline.prev_close,
            open=latest_kline.open,
            high=latest_kline.high,
            low=latest_kline.low,
            limit_up=(latest_limit.limit_up_price if latest_limit and latest_limit.limit_up_price else latest_kline.close),
            limit_down=0,
            change_pct=latest_kline.change_pct,
            change_amt=(latest_kline.close or 0) - (latest_kline.prev_close or 0),
            amplitude=amplitude,
            min5_change=0,
            volume=(latest_kline.volume or 0) / 100,
            amount=latest_kline.amount,
            turnover=latest_kline.turnover,
            volume_ratio=1.0,
            main_net_inflow=0,
            avg_price=latest_kline.close,
            bid_ratio=0,
            circ_market_cap=0,
            pe_ttm=0,
            pb=0,
            dividend_yield=0,
            net_profit_growth=0,
            updated_at=datetime.combine(latest_kline.trade_date, datetime.min.time()),
        )

    name = spot.name or code

    # 2. 资金口径: 近5个完整交易日；当日盘中资金单列，绝不混入“5日”。
    fund_context = await _load_stock_fund_context(
        db,
        code,
        quote_trade_date=target_date,
        completed_trade_date=technical_trade_date,
        current_snapshot=current_fund_snapshot,
        as_of_at=generated_at,
    )
    raw_fund_5d = fund_context.get("fund_5d_total")
    fund_5d = _safe_float(raw_fund_5d)

    # 3. 五日涨幅需要6个收盘价，且与技术指标使用相同完整日线截止日。
    change_5d_context = await _load_stock_change_5d(db, code, technical_trade_date)
    change_5d = _safe_float(change_5d_context.get("change_pct"))

    # 4. 技术指标只使用最近完整日线，盘中价仅用于确认相对位置。
    tech = {}
    if technical_trade_date is not None:
        tech = await anomaly_scanner._calc_technical_indicators(
            code,
            db,
            target_date=technical_trade_date,
        )

    # 5. 连板
    consecutive_days = 0
    lu_result = await db.execute(
        select(LimitUpPool.consecutive_days).where(
            LimitUpPool.code == code,
            LimitUpPool.trade_date == target_date,
        )
    )
    lu_row = lu_result.first()
    if lu_row:
        consecutive_days = lu_row[0] or 0
    current_fund = fund_context.get("current_item") or {}
    current_main_inflow = _safe_float((current_fund or {}).get("main_net_inflow"))

    is_limit_up = (spot.limit_up and spot.price and spot.price >= spot.limit_up)
    is_one_word = is_limit_up and spot.open and spot.open >= spot.limit_up

    # 6. BullScore评分
    bull_result = _bull_model.score_from_spot(
        code=code, name=name,
        change_pct=spot.change_pct or 0,
        volume_ratio=spot.volume_ratio or 1.0,
        turnover=spot.turnover or 0,
        amplitude=spot.amplitude or 0,
        main_net_inflow=current_main_inflow,
        main_net_inflow_pct=_safe_float((current_fund or {}).get("main_net_inflow_pct")),
        super_net_inflow=_safe_float((current_fund or {}).get("super_net_inflow")),
        super_net_inflow_pct=_safe_float((current_fund or {}).get("super_net_inflow_pct")),
        big_net_inflow=_safe_float((current_fund or {}).get("big_net_inflow")),
        big_net_inflow_pct=_safe_float((current_fund or {}).get("big_net_inflow_pct")),
        circ_market_cap=spot.circ_market_cap or 0,
        pe_ttm=spot.pe_ttm or 0,
        pb=spot.pb or 0,
        net_profit_growth=spot.net_profit_growth or 0,
        price=spot.price or 0,
        open=spot.open or 0,
        high=spot.high or 0,
        low=spot.low or 0,
        limit_up=spot.limit_up or 0,
        ma5=tech.get("ma5", 0), ma10=tech.get("ma10", 0),
        ma20=tech.get("ma20", 0), ma60=tech.get("ma60", 0),
        boll_upper=tech.get("boll_upper", 0), boll_mid=tech.get("boll_mid", 0),
        boll_lower=tech.get("boll_lower", 0),
        high_20d=tech.get("high_20d", 0), high_60d=tech.get("high_60d", 0),
        macd_signal=tech.get("macd_signal", ""),
        rsi14=tech.get("rsi14"),
        kdj_signal=tech.get("kdj_signal", ""),
        main_net_inflow_5d=fund_5d,
        change_pct_5d=change_5d,
        consecutive_days=consecutive_days,
        is_limit_up=is_limit_up,
        is_one_word=is_one_word,
    )

    # 7. 板块状态只认 SectorLifecycle；主驱动按因果映射和生命周期择优。
    try:
        sector_context = await _load_stock_plan_sector_context(
            db,
            code,
            target_date,
            _safe_float(spot.change_pct),
        )
    except Exception as exc:
        logger.warning(f"[stock_next_day_plan] {code} 板块上下文加载失败: {exc}")
        sector_context = {
            "available": False,
            "sector_change_pct": 0.0,
            "sector_lifecycle_state": "",
            "sector_lifecycle_label": "生命周期缺失",
            "sector_status": "板块数据不足",
            "sector_resonance": "数据不足",
            "sector_resonance_score": 0.0,
            "sector_resonance_name": "",
            "sector_resonance_change": 0.0,
            "sector_resonance_fund": 0.0,
        }

    # 8. 与 Dashboard 2.0 / 情绪熔断器完全共用口径。
    try:
        market_context = await _load_market_regime_context(db, target_date)
    except Exception as exc:
        logger.warning(f"[stock_next_day_plan] {code} 市场环境加载失败: {exc}")
        market_context = {
            "market_environment": "neutral",
            "sentiment_phase": "pending",
            "sentiment_cycle": "待定",
            "sentiment_trade_date": None,
            "index_trade_dates": {},
        }

    # 9. 调用策略引擎
    plan_result = next_day_plan_engine.generate(
        code=code,
        name=name,
        bull_level=bull_result.level,
        bull_score=bull_result.total_score,
        price=spot.price or 0,
        change_pct=spot.change_pct or 0,
        dimensions=bull_result.dimensions,
        top_signals=bull_result.top_signals,
        risk_warnings=bull_result.risk_warnings,
        ma5=tech.get("ma5", 0),
        ma10=tech.get("ma10", 0),
        ma20=tech.get("ma20", 0),
        ma60=tech.get("ma60", 0),
        boll_upper=tech.get("boll_upper", 0),
        boll_mid=tech.get("boll_mid", 0),
        boll_lower=tech.get("boll_lower", 0),
        high_20d=tech.get("high_20d", 0),
        high_60d=tech.get("high_60d", 0),
        macd_signal=tech.get("macd_signal", ""),
        rsi14=tech.get("rsi14"),
        kdj_signal=tech.get("kdj_signal", ""),
        volume_ratio=spot.volume_ratio or 1.0,
        volume_ratio_level=bull_result.volume_ratio_level,
        turnover=spot.turnover or 0,
        turnover_level=bull_result.turnover_level,
        price_volume_relation=bull_result.price_volume_relation,
        main_net_inflow_billion=round(current_main_inflow / 1e8, 2),
        fund_5d_billion=fund_5d / 1e8,
        current_fund_available=(
            current_fund.get("main_net_inflow") is not None
            and not current_fund.get("is_stale", True)
        ),
        fund_5d_complete=bool(fund_context.get("fund_5d_complete")),
        consecutive_days=consecutive_days,
        is_limit_up=is_limit_up,
        is_one_word=is_one_word,
        chip_signal=bull_result.chip_signal,
        sector_change_pct=_safe_float(sector_context.get("sector_change_pct")),
        sector_lifecycle=str(sector_context.get("sector_status") or "板块数据不足"),
        sector_lifecycle_state=str(sector_context.get("sector_lifecycle_state") or ""),
        # 板块共振
        sector_resonance=str(sector_context.get("sector_resonance") or "数据不足"),
        sector_resonance_score=_safe_float(sector_context.get("sector_resonance_score")),
        sector_resonance_name=str(sector_context.get("sector_resonance_name") or ""),
        sector_resonance_change=_safe_float(sector_context.get("sector_resonance_change")),
        sector_resonance_fund=_safe_float(sector_context.get("sector_resonance_fund")),
        # 情绪面
        market_environment=str(market_context.get("market_environment") or "neutral"),
        sentiment_cycle=str(market_context.get("sentiment_cycle") or "待定"),
        circ_market_cap=spot.circ_market_cap or 0,
        pe_ttm=spot.pe_ttm or 0,
        open_price=spot.open or 0,
        high=spot.high or 0,
        low=spot.low or 0,
    )

    quality_warnings: list[str] = []
    degraded = False
    provisional = bool(plan_dates.get("is_intraday_provisional"))
    if provisional:
        quality_warnings.append("盘中行情仅作动态确认，日线技术指标与5日统计截至上一完整交易日")
    if technical_trade_date is None or not tech:
        quality_warnings.append("完整日线不足，技术位与技术信号不可用")
        degraded = True
    if int(fund_context.get("fund_5d_count") or 0) < 5:
        quality_warnings.append(
            f"完整资金数据仅{int(fund_context.get('fund_5d_count') or 0)}个交易日，不足5日"
        )
        degraded = True
    if int(fund_context.get("fund_5d_clock_unknown_count") or 0) > 0:
        quality_warnings.append("历史资金源时钟未验证，保留研究数值但不作完整买入证据")
        degraded = True
    if current_fund.get("is_stale", True):
        quality_warnings.append("当前主力资金源时钟未知、陈旧或晚于决策，不支持直接买入")
        degraded = True
    if fund_context.get("current_source") == "unavailable":
        quality_warnings.append("当日主力资金不可用，资金维度无法确认并禁止直接买入")
        degraded = True
    elif fund_context.get("used_fallback"):
        quality_warnings.append("东方财富当日资金不可用，已回退数据库 FundFlow 口径")
    current_fund_trade_date = fund_context.get("current_trade_date")
    if current_fund_trade_date and str(current_fund_trade_date) != str(target_date):
        quality_warnings.append(f"最新资金日期为{current_fund_trade_date}，落后于行情日{target_date}")
        degraded = True
    if not sector_context.get("available"):
        quality_warnings.append("因果板块或权威生命周期数据缺失")
        degraded = True
    sentiment_trade_date = market_context.get("sentiment_trade_date")
    if sentiment_trade_date is None:
        quality_warnings.append("市场情绪数据缺失，情绪周期待定")
        degraded = True
    elif str(sentiment_trade_date) != str(target_date):
        quality_warnings.append(f"市场情绪数据截至{sentiment_trade_date}")

    payload = _plan_result_to_dict(plan_result)
    if raw_fund_5d is None:
        payload["fund_5d_billion"] = None
        payload["fund_5d_direction"] = "数据不足"

    current_main_raw = current_fund.get("main_net_inflow")
    quote_as_of = (
        getattr(spot, "source_quote_at", None)
        or getattr(spot, "updated_at", None)
        or generated_at
    )
    payload.update({
        "data_version": "stock-next-day-plan-v2",
        "analysis_trade_date": str(target_date),
        "quote_trade_date": str(plan_dates.get("quote_trade_date") or target_date),
        "quote_as_of": quote_as_of.isoformat(timespec="seconds") if isinstance(quote_as_of, datetime) else str(quote_as_of),
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "technical_trade_date": str(technical_trade_date) if technical_trade_date else None,
        "technical_source": tech.get("source") or None,
        "is_intraday_provisional": provisional,
        "trade_session": plan_dates.get("trade_session"),
        "fund_5d_count": int(fund_context.get("fund_5d_count") or 0),
        "fund_5d_start_date": str(fund_context.get("fund_5d_start_date")) if fund_context.get("fund_5d_start_date") else None,
        "fund_5d_end_date": str(fund_context.get("fund_5d_end_date")) if fund_context.get("fund_5d_end_date") else None,
        "fund_5d_label": "近5个已确认收盘会话" if fund_context.get("fund_5d_complete") else "近5会话资金未知",
        "fund_5d_complete": bool(fund_context.get("fund_5d_complete")),
        "fund_5d_window": fund_context.get("fund_5d_window"),
        "fund_5d_status": fund_context.get("fund_5d_status"),
        "current_main_net_inflow_billion": round(_safe_float(current_main_raw) / 1e8, 2) if current_main_raw is not None else None,
        "current_fund_source": fund_context.get("current_source"),
        "current_fund_trade_date": str(current_fund_trade_date) if current_fund_trade_date else None,
        "current_fund_as_of": str(fund_context.get("current_as_of") or "") or None,
        "current_fund_clock_status": current_fund.get("clock_status", "unknown"),
        "current_fund_is_stale": bool(current_fund.get("is_stale", True)),
        "fund_5d_clock_unknown_count": int(fund_context.get("fund_5d_clock_unknown_count") or 0),
        "current_fund_is_partial": bool(provisional and str(current_fund_trade_date) == str(target_date)),
        "sector_code": sector_context.get("sector_code"),
        "sector_lifecycle_state": sector_context.get("sector_lifecycle_state"),
        "sector_lifecycle_label": sector_context.get("sector_lifecycle_label"),
        "sector_lifecycle_score": sector_context.get("sector_lifecycle_score"),
        "sentiment_phase": market_context.get("sentiment_phase"),
        "sentiment_trade_date": str(sentiment_trade_date) if sentiment_trade_date else None,
        "sentiment_score": market_context.get("sentiment_score"),
        "market_index_trade_dates": market_context.get("index_trade_dates") or {},
        "quality_status": "degraded" if degraded else "provisional" if provisional else "ok",
        "quality_warnings": list(dict.fromkeys(quality_warnings)),
    })
    return payload
