"""采集调度器 — 统一管理所有数据源的定时采集+写入DB

每个采集方法现在完整实现：采集 → 字段映射 → 质量校验 → DB写入(upsert) → 质量记录
"""

import asyncio
import math
import sqlite3
import time as _time
from collections import Counter, deque
from datetime import datetime, date, time, timedelta
from typing import Optional

import pandas as pd
from loguru import logger
from sqlalchemy import and_, desc, func, or_, select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import OperationalError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config.settings import settings
from app.core.trade_calendar import trade_calendar
from app.core.data_quality import data_quality_guard
from app.core.process_awake import ProcessAwakeGuard
from app.data.pipeline_runtime import JOB_EVENT_MASK, PipelineRuntimeHealth
from app.core.stock_tagger import stock_tagger
from app.data.sources.akshare_source import AkShareSource
from app.data.sources.eastmoney_source import EastMoneySource
from app.data.fund_flow_clock import evidence_clock, local_clock, verified_fund_clocks
from app.data.sources.pywencai_source import PyWencaiSource
from app.data.sources.sw_source import ShenwanSource
from app.data.sources.sina_source import SinaSource
from app.data.sources.index_source import IndexSource
from app.data.sources.tencent_source import TencentSource
from app.data.sources.ths_kline_source import ThsKlineSource
from app.db.session import async_session
from app.models.news import FinanceNews

# ORM Models
from app.models.stock import (
    SectorInfo, StockSectorMapping, BoardCons,
    LimitUpPool, LimitDownPool, BrokenLimitPool,
    FundFlow, MarketSentiment, SectorPersistence,
    StockTag, StockBlacklist, StockDaily, AuctionData, MarginData,
    StockSpot, StockKline, StockFundamentalDaily, QuoteRound,
)

PROMOTION_NEWS_MAX_STALENESS = timedelta(hours=3)
PROMOTION_NEWS_REFRESH_LIMIT_PER_SOURCE = 120
PROMOTION_NEWS_REFRESH_PROCESS_LIMIT = 48
PROMOTION_NEWS_CRITICAL_SOURCES = ("cninfo", "em", "ths")
from app.promotion.route_contract import (
    EXECUTION_ROUTES as PROMOTION_EXECUTION_ROUTES,
    persisted_contract_status, persisted_route_gate,
)


def _promotion_news_source_health(
    latest_by_source: dict[str, datetime],
    *,
    now: datetime | None = None,
    fetch_health: dict[str, dict] | None = None,
) -> dict:
    """区分内容时效与抓取心跳；没有新公告不能导致预测同步反复补抓。"""
    current = now or datetime.now()
    stale_sources: list[str] = []
    age_minutes: dict[str, float | None] = {}
    refresh_needed_sources: list[str] = []
    checked_without_new_content: list[str] = []
    for source in PROMOTION_NEWS_CRITICAL_SOURCES:
        latest = latest_by_source.get(source)
        age_seconds = (current - latest).total_seconds() if isinstance(latest, datetime) else None
        age_minutes[source] = round(age_seconds / 60.0, 1) if age_seconds is not None else None
        if age_seconds is None or not 0 <= age_seconds <= PROMOTION_NEWS_MAX_STALENESS.total_seconds():
            stale_sources.append(source)
            observation = (fetch_health or {}).get(source) or {}
            observed_at = observation.get("observed_at")
            poll_age = (current - observed_at).total_seconds() if isinstance(observed_at, datetime) else None
            if (
                age_seconds is not None and age_seconds >= 0
                and observation.get("status") == "ok"
                and poll_age is not None
                and 0 <= poll_age <= max(0.0, float(settings.PROMOTION_NEWS_REFRESH_COOLDOWN_SEC))
            ):
                checked_without_new_content.append(source)
            else:
                refresh_needed_sources.append(source)
    return {
        "fresh": not stale_sources,
        "stale_sources": stale_sources,
        "refresh_needed": bool(refresh_needed_sources),
        "refresh_needed_sources": refresh_needed_sources,
        "checked_without_new_content": checked_without_new_content,
        "age_minutes_by_source": age_minutes,
        "latest_by_source": {
            source: latest.isoformat() if isinstance(latest, datetime) else None
            for source, latest in latest_by_source.items()
        },
    }


def _promotion_prediction_snapshot_context(trigger: str, now: datetime | None = None) -> str:
    """给晋级预测调度快照生成稳定上下文名，便于隔离和复盘."""
    normalized = str(trigger or "").strip().lower()
    for suffix in ("0925", "0935", "1000", "1030", "1305", "1510", "2000"):
        if normalized.endswith(suffix):
            return f"promotion_{suffix}"
    if normalized and normalized not in {"schedule", "scheduler", "cron"}:
        return normalized

    current = now or datetime.now()
    if current.hour == 9 and current.minute <= 26:
        return "promotion_0925"
    if current.hour == 9:
        return "promotion_0935"
    if current.hour == 10:
        return "promotion_1000" if current.minute < 25 else "promotion_1030"
    if current.hour == 13 and current.minute <= 20:
        return "promotion_1305"
    if current.hour == 15:
        return "promotion_1510"
    if current.hour >= 20:
        return "promotion_2000"
    return f"promotion_{current:%H%M}"


def _promotion_quality_gate_blocks_all_routes(quality_gate: dict | None) -> bool:
    """全局失败时，仅在 B/C/D 三条生产路线都失败才阻止正式批次。"""
    gate = quality_gate or {}
    if persisted_contract_status(gate) not in {"supported", "legacy_unversioned"}:
        return True
    if gate.get("gate_passed") is True:
        return False
    return not any(
        persisted_route_gate(gate, route)["gate_passed"] is True
        for route in PROMOTION_EXECUTION_ROUTES
    )


def _promotion_startup_catchup_trigger(now: datetime | None = None) -> str:
    """原盘中恢复时段不变；盘后从名义时点起、仅在原正式窗口内恢复。"""
    current = now or datetime.now()
    minutes = current.hour * 60 + current.minute
    if 9 * 60 + 25 <= minutes < 9 * 60 + 35:
        return "promotion_prediction_0925"
    if 9 * 60 + 35 <= minutes < 9 * 60 + 45:
        return "promotion_prediction_0935"
    if 9 * 60 + 55 <= minutes < 10 * 60 + 15:
        return "promotion_prediction_1000"
    if 10 * 60 + 25 <= minutes < 10 * 60 + 45:
        return "promotion_prediction_1030"
    if 13 * 60 <= minutes < 13 * 60 + 20:
        return "promotion_prediction_1305"
    # Do not duplicate or widen producer windows. In particular, a restart at
    # 19:50 must not manufacture a nominal 20:00 snapshot ten minutes early.
    from app.api.v1.promotion import _PROMOTION_OFFICIAL_CONTEXT_WINDOWS
    for suffix in ("1510", "2000"):
        nominal = (int(suffix[:2]), int(suffix[2:]))
        window = _PROMOTION_OFFICIAL_CONTEXT_WINDOWS.get(f"promotion_{suffix}")
        if window and max(nominal, window[0]) <= (current.hour, current.minute) <= window[1]:
            return f"promotion_prediction_{suffix}"
    return ""


def _review_schedule_time(value: str, default: tuple[int, int]) -> tuple[int, int]:
    """Parse HH:MM settings without allowing a bad env value to break startup."""
    try:
        hour_text, minute_text = str(value or "").strip().split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (TypeError, ValueError):
        pass
    logger.warning(f"复盘自动化时间配置无效: {value!r}，回退到 {default[0]:02d}:{default[1]:02d}")
    return default


def _next_sector_active_days(prev_consecutive_days: int, limit_up_count: int, fund_flow: float) -> int:
    """计算板块连续活跃天数。

    只有今日仍存在活跃信号时才延续，否则归零。
    """
    is_active_today = limit_up_count > 0 or (fund_flow or 0) > 0
    if is_active_today:
        return max(prev_consecutive_days or 0, 0) + 1
    return 0


def _resolve_spot_snapshot_trade_date(
    latest_updated_at: datetime | date | None,
    *,
    today: date | None = None,
) -> date | None:
    """根据最新 spot 快照时间推断有效交易日.

    关键约束:
    - 凌晨/未开盘时, stock_spot 仍可能保留上一交易日的最后快照
    - 此时不能直接用 `date.today()` 给 stock_kline 打今天的日期
    """
    if latest_updated_at is None:
        return None

    if isinstance(latest_updated_at, datetime):
        trade_date = latest_updated_at.date()
    elif isinstance(latest_updated_at, date):
        trade_date = latest_updated_at
    else:
        return None

    current_day = today or date.today()
    if trade_date > current_day:
        return None
    return trade_date


def _should_run_startup_kline_compensation(
    *,
    session_name: str,
    latest_spot_trade_date: date | None,
    today: date | None = None,
) -> bool:
    current_day = today or date.today()
    return (
        session_name in {"pre_auction", "morning", "afternoon", "after_hours"}
        and latest_spot_trade_date == current_day
    )


def _calculate_spot_advance_decline_ratio(
    rows: list[tuple],
    *,
    target_date: date,
) -> tuple[float, int, int, int, int]:
    """按同日有效实时行情计算上涨/下跌家数比，避免误用涨停/跌停比。"""
    advances = 0
    declines = 0
    flats = 0
    for price, volume, change_pct, updated_at in rows:
        if isinstance(updated_at, datetime):
            snapshot_date = updated_at.date()
        elif isinstance(updated_at, date):
            snapshot_date = updated_at
        else:
            continue
        numeric_price = _safe_float(price) or 0.0
        numeric_volume = _safe_float(volume) or 0.0
        numeric_change = _safe_float(change_pct)
        if (
            snapshot_date != target_date
            or numeric_price <= 0
            or numeric_volume <= 0
            or numeric_change is None
        ):
            continue
        if numeric_change > 0.005:
            advances += 1
        elif numeric_change < -0.005:
            declines += 1
        else:
            flats += 1
    sample_count = advances + declines + flats
    if advances + declines <= 0:
        return 1.0, advances, declines, flats, sample_count
    ratio = advances / declines if declines > 0 else float(advances)
    return round(ratio, 2), advances, declines, flats, sample_count


SENTIMENT_CALCULATION_VERSION = "breadth_index_quality_v4_nullable_funds"


def _current_index_snapshot_records(records: list[dict], trade_day: date) -> list[dict]:
    """日线回退的旧日期不冒充当日完整指数快照；同指数只计一次。"""
    import math
    valid = {}
    for row in records:
        code = str(row.get("code") or "")
        close = _safe_float(row.get("close"))
        change = _safe_float(row.get("change_pct"))
        if (code in {"000001", "399001", "399006"} and row.get("trade_date") == trade_day
                and close is not None and math.isfinite(close) and close > 0
                and change is not None and math.isfinite(change)):
            valid[code] = row
    return list(valid.values())


def _calculate_market_sentiment_state(
    *,
    limit_up_count: int,
    limit_down_count: int,
    broken_limit_count: int,
    seal_rate: float,
    board_height: int,
    main_net_inflow: float | None,
    advance_decline_ratio: float,
    breadth_sample_count: int,
    breadth_coverage: float,
    index_avg_change_pct: float | None,
    index_sample_count: int,
    turnover_total: float,
    fund_flow_coverage: float,
) -> dict:
    """统一计算情绪与质量；数据不完整时绝不把缺失值解释为“修复”."""
    from numbers import Real

    numeric_inputs = {
        "limit_up_count": limit_up_count, "limit_down_count": limit_down_count,
        "broken_limit_count": broken_limit_count, "seal_rate": seal_rate,
        "board_height": board_height, "main_net_inflow": main_net_inflow,
        "advance_decline_ratio": advance_decline_ratio,
        "breadth_sample_count": breadth_sample_count, "breadth_coverage": breadth_coverage,
        "index_avg_change_pct": index_avg_change_pct, "index_sample_count": index_sample_count,
        "turnover_total": turnover_total, "fund_flow_coverage": fund_flow_coverage,
    }
    count_fields = {"limit_up_count", "limit_down_count", "broken_limit_count",
                    "board_height", "breadth_sample_count", "index_sample_count"}
    invalid_fields = []
    for field, value in numeric_inputs.items():
        # An explicitly unavailable index already has an established degraded path.
        if field == "index_avg_change_pct" and value is None:
            continue
        try:
            valid = (isinstance(value, Real) and not pd.api.types.is_bool(value)
                     and math.isfinite(value))
            if valid and field in count_fields:
                valid = value >= 0 and value == int(value)
            elif valid and field in {"breadth_coverage", "fund_flow_coverage"}:
                valid = 0 <= value <= 1
            elif valid and field == "seal_rate":
                valid = 0 <= value <= 100
            elif valid and field in {"advance_decline_ratio", "turnover_total"}:
                valid = value >= 0
        except (ValueError, TypeError, OverflowError):
            valid = False
        if not valid:
            invalid_fields.append(field)
    if invalid_fields:
        # Non-tradable sentinel, not a neutral/strong-market observation. Preserve
        # raw unknown values separately; do not change entry or exit thresholds.
        return {
            "score": 0.0, "cycle_points": 0, "cycle": "divergence",
            "quality_status": "degraded",
            "quality_reason": "情绪数值无效: " + ", ".join(invalid_fields),
            "quality_completeness": 0.0, "broad_weakness": False,
        }
    # cycle_points 只用于离散周期判定；对外 sentiment_score 必须统一为
    # 0~100，避免旧实现把 -4~+4 的周期分值误当成百分制评分持久化。
    cycle_points = 0
    if limit_up_count >= 80:
        cycle_points += 2
    elif limit_up_count >= 50:
        cycle_points += 1
    elif limit_up_count < 20:
        cycle_points -= 2
    elif limit_up_count < 30:
        cycle_points -= 1

    if seal_rate >= 80:
        cycle_points += 2
    elif seal_rate >= 65:
        cycle_points += 1
    elif seal_rate < 45:
        cycle_points -= 2
    elif seal_rate < 60:
        cycle_points -= 1

    if board_height >= 5:
        cycle_points += 2
    elif board_height >= 3:
        cycle_points += 1
    elif board_height <= 1:
        cycle_points -= 1

    if limit_down_count >= 20:
        cycle_points -= 2
    elif limit_down_count >= 10:
        cycle_points -= 1
    if broken_limit_count >= limit_up_count and broken_limit_count >= 20:
        cycle_points -= 2
    elif broken_limit_count >= max(10, limit_up_count * 0.5):
        cycle_points -= 1
    if main_net_inflow >= 80:
        cycle_points += 1
    elif main_net_inflow <= -80:
        cycle_points -= 1

    # 市场宽度与指数方向是全市场状态，不能被局部涨停池覆盖。
    if advance_decline_ratio >= 1.5:
        cycle_points += 2
    elif advance_decline_ratio >= 1.0:
        cycle_points += 1
    elif advance_decline_ratio <= 0.5:
        cycle_points -= 2
    elif advance_decline_ratio <= 0.8:
        cycle_points -= 1
    if index_avg_change_pct is not None:
        if index_avg_change_pct >= 1.0:
            cycle_points += 2
        elif index_avg_change_pct >= 0.3:
            cycle_points += 1
        elif index_avg_change_pct <= -1.5:
            cycle_points -= 2
        elif index_avg_change_pct <= -0.5:
            cycle_points -= 1

    quality_reasons: list[str] = []
    min_coverage = max(float(settings.PAPER_MARKET_QUALITY_MIN_BREADTH_COVERAGE), 0.0)
    if breadth_sample_count < 500 or breadth_coverage < min_coverage:
        quality_reasons.append(
            f"涨跌家数覆盖{breadth_coverage:.1%}({breadth_sample_count}只)低于{min_coverage:.0%}"
        )
    if index_sample_count < 2 or index_avg_change_pct is None:
        quality_reasons.append(f"指数有效样本仅{index_sample_count}个")
    if turnover_total <= 0:
        quality_reasons.append("沪深成交额缺失或为0")
    if fund_flow_coverage < min_coverage:
        quality_reasons.append(f"主力资金覆盖{fund_flow_coverage:.1%}低于{min_coverage:.0%}")
    quality_status = "ok" if not quality_reasons else "degraded"
    coverage_denominator = min_coverage if min_coverage > 0 else 1.0
    quality_completeness = (
        min(max(breadth_coverage / coverage_denominator, 0.0), 1.0)
        + min(max(index_sample_count / 2.0, 0.0), 1.0)
        + (1.0 if turnover_total > 0 else 0.0)
        + min(max(fund_flow_coverage / coverage_denominator, 0.0), 1.0)
    ) / 4.0

    if cycle_points >= 4:
        cycle = "climax"
    elif cycle_points >= 1:
        cycle = "recovery"
    elif cycle_points <= -4:
        cycle = "freezing"
    else:
        cycle = "divergence"

    broad_weakness = (
        advance_decline_ratio < float(settings.PAPER_MARKET_WEAK_BREADTH_RATIO)
        or (
            index_avg_change_pct is not None
            and index_avg_change_pct <= float(settings.PAPER_MARKET_WEAK_INDEX_AVG_CHANGE_PCT)
        )
    )
    # 降级数据或指数/宽度共振走弱时，周期最多只能是分歧；极弱仍保留冰点。
    if (quality_status != "ok" or broad_weakness) and cycle in {"recovery", "climax"}:
        cycle = "divergence"

    sentiment_score = max(0.0, min(100.0, 50.0 + cycle_points * 5.0))
    return {
        "score": round(sentiment_score, 1),
        "cycle_points": cycle_points,
        "cycle": cycle,
        "quality_status": quality_status,
        "quality_reason": "；".join(quality_reasons),
        "quality_completeness": round(quality_completeness, 6),
        "broad_weakness": broad_weakness,
    }


class DataScheduler:
    """数据采集调度器 — 采集+写入一体化"""

    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self._jobs_initialized = False
        self._pipeline_runtime_health = PipelineRuntimeHealth()
        self._runtime_listener_registered = False
        self._pipeline_health_task: asyncio.Task | None = None
        self._anomaly_snapshot_refreshing = False
        self._anomaly_snapshot_pending = False
        self._news_raw_refreshing = False
        self._news_ai_refreshing = False
        self._news_weekend_refreshing = False
        self._promotion_prediction_refreshing = False
        self._promotion_prediction_epoch = 0
        self._promotion_prediction_pending: dict | None = None
        self._promotion_prediction_active_context: str | None = None
        self._promotion_prediction_task: asyncio.Task | None = None
        self._promotion_prediction_last_attempt: dict[tuple[date, str], float] = {}
        self._promotion_news_refresh_task: asyncio.Task | None = None
        self._concept_fund_flow_task: asyncio.Task | None = None
        self._index_history_task: asyncio.Task | None = None
        self._index_history_refreshing = False
        self._index_history_runtime: dict = {}
        self._kline_price_chain_health: dict = {"status": "not_observed"}
        self._kline_observation_health: dict = {"status": "not_observed"}
        self._sentiment_fund_health: dict = {"status": "not_observed"}
        self._promotion_news_last_attempt: float | None = None
        self._promotion_news_last_result: dict = {}
        self._promotion_runtime_audit: list[dict] = []
        self._promotion_active_phase: str | None = None
        self._promotion_phase_tick: float = 0.0
        self._promotion_phase_timings: dict[str, float] = {}
        self._promotion_learning_reviewing = False
        self._review_automation_running: set[str] = set()
        self._promotion_startup_catchup_task: asyncio.Task | None = None
        self._promotion_recovery_health: dict = {"status": "not_observed", "protocol_version": "formal_window_recovery_v1"}
        self._promotion_snapshot_completed_contexts: set[tuple[date, str]] = set()
        self._auction_collect_refreshing = False
        self._paper_auto_trading = False
        self._paper_research_reporting = False
        self._paper_auto_trading_started_at: datetime | None = None
        self._paper_auto_trading_last_run_at: datetime | None = None
        self._paper_auto_loop_task: asyncio.Task | None = None
        self._anomaly_push_loop_task: asyncio.Task | None = None
        self._paper_buy_point_push_task: asyncio.Task | None = None
        self._chase_review_refreshing = False
        self._anomaly_scan_event = asyncio.Event()
        self._anomaly_scan_trigger = "startup"
        # 交易保留最新轮次；A2纯行情路径另存有界前向帧，不能随交易合并丢失。
        self._process_awake_guard = ProcessAwakeGuard(enabled=settings.SCHEDULER_PREVENT_IDLE_SLEEP)
        self._quote_round_event = asyncio.Event()
        self._quote_round_payload: dict | None = None
        self._quote_round_loop_task: asyncio.Task | None = None
        self._momentum_quote_inbox: deque[dict] = deque()
        self._momentum_quote_inflight: dict | None = None
        self._momentum_last_published_at: datetime | None = None
        # 行情事件消费者与60秒watchdog共用，禁止同一批账户重叠执行。
        self._quote_dispatch_lock = asyncio.Lock()
        self._quote_archive_tasks: set[asyncio.Task] = set()
        self._last_healthy_quote_round_at: datetime | None = None
        self._last_quote_round_processed_at: datetime | None = None
        self._last_quote_round_processed_id: str | None = None
        self._paper_position_risk_health: dict = {"status": "not_run", "accounts": []}
        self._anomaly_scan_requested_at: float | None = None
        self._last_anomaly_scan_started_at: float | None = None
        self._last_intraday_limit_up_codes: set[str] | None = None
        self._sources = {
            "akshare": AkShareSource(),
            "eastmoney": EastMoneySource(),
            "pywencai": PyWencaiSource(),
            "shenwan": ShenwanSource(),
            "sina": SinaSource(),
            "index": IndexSource(),
            "tencent": TencentSource(),
            "ths_kline": ThsKlineSource(),
        }
        # 腾讯实时行情: 缓存可交易股票代码列表(每日盘前刷新)
        self._tradeable_codes: list[str] = []

    def setup_jobs(self):
        """设置定时任务 — 优化版(v2.0: 秒级延时)"""
        if self._jobs_initialized:
            return

        # === 盘前 (8:25) 股票状态更新 ===
        self.scheduler.add_job(
            self._update_stock_status, CronTrigger(hour=8, minute=25, day_of_week="mon-fri"),
            id="update_stock_status", name="股票状态更新(ST/停牌/退市)",
        )

        # === 盘前 (8:30) 板块列表+申万 ===
        self.scheduler.add_job(
            self._pre_market, CronTrigger(hour=8, minute=30, day_of_week="mon-fri"),
            id="pre_market", name="盘前数据准备",
        )

        # === 竞价采集 9:15-9:25 每30秒 ===
        self.scheduler.add_job(
            self._auction_collect, IntervalTrigger(seconds=30),
            id="auction_collect", name="竞价数据采集",
        )
        self.scheduler.add_job(
            self._auction_collect_force_0920,
            CronTrigger(hour=9, minute=20, second=6, day_of_week="mon-fri"),
            id="auction_collect_0920", name="竞价不可撤单阶段证据(09:20:06)",
        )
        self.scheduler.add_job(
            self._auction_collect_force_0924, CronTrigger(hour=9, minute=24, day_of_week="mon-fri"),
            id="auction_collect_0924", name="竞价最终快照(09:24)",
        )
        self.scheduler.add_job(
            self._auction_collect_force_0925,
            CronTrigger(hour=9, minute=25, second=6, day_of_week="mon-fri"),
            id="auction_collect_0925", name="竞价最终快照(09:25:06)",
        )

        # === 盘中快频 — 每10秒(涨停/跌停/炸板) ===
        self.scheduler.add_job(
            self._intraday_fast, IntervalTrigger(seconds=10),
            id="intraday_fast", name="盘中快频采集(10s)",
        )

        # === 盘中慢频 — 每30秒(资金流) ===
        self.scheduler.add_job(
            self._intraday_slow, IntervalTrigger(seconds=30),
            id="intraday_slow", name="腾讯资金滚动采集(30s)",
            coalesce=True, max_instances=1, misfire_grace_time=20,
        )

        # === 指数快照 — 每60秒(三大指数+情绪) ===
        self.scheduler.add_job(
            self._intraday_indices_and_sentiment, IntervalTrigger(seconds=60),
            id="intraday_indices_and_sentiment", name="指数与情绪快照(60s)",
        )

        # 指数65日窗口只在盘外刷新，失败半小时重试；不进入买入或60秒行情链。
        self.scheduler.add_job(
            self._refresh_index_history, CronTrigger(minute="0,30"),
            id="index_history_window", name="盘外指数历史完整窗口",
            coalesce=True, max_instances=1, misfire_grace_time=120,
        )

        # === 盘中新浪 — 每5分钟(概念板块行情) ===
        self.scheduler.add_job(
            self._intraday_sina, IntervalTrigger(minutes=5),
            id="intraday_sina", name="盘中新浪概念行情",
        )

        # === 收盘固化 (15:05)：先冻结全市场终值，再允许盘后预测 ===
        self.scheduler.add_job(
            self._finalize_close_snapshot,
            CronTrigger(hour=15, minute=5, day_of_week="mon-fri"),
            id="close_snapshot_finalize",
            name="收盘行情固化与质量验收",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
        )

        # === 盘后映射补全 (15:20)，避免与收盘固化/15:10预测争抢外部源 ===
        self.scheduler.add_job(
            self._after_market, CronTrigger(hour=15, minute=20, day_of_week="mon-fri"),
            id="after_market", name="盘后数据补全",
        )

        # === 深度复盘 (20:00) ===
        self.scheduler.add_job(
            self._deep_review, CronTrigger(hour=20, minute=0, day_of_week="mon-fri"),
            id="deep_review", name="盘后深度数据",
        )

        # === 每日基本面截面快照 (16:05 盘后，前向积累供历史复盘) ===
        self.scheduler.add_job(
            self._snapshot_daily_fundamentals,
            CronTrigger(hour=16, minute=5, day_of_week="mon-fri"),
            id="fundamental_daily_snapshot",
            name="每日基本面截面快照(16:05)",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
        )

        # === 晋级预测快照: 盘后、晚间、次日竞价/开盘后 ===
        for hour, minute, job_id, name in (
            (15, 10, "promotion_prediction_1510", "晋级预测快照(15:10)"),
            (20, 0, "promotion_prediction_2000", "晋级预测快照(20:00)"),
            (9, 25, "promotion_prediction_0925", "晋级预测快照(09:25)"),
            (9, 35, "promotion_prediction_0935", "晋级预测快照(09:35)"),
            (10, 0, "promotion_prediction_1000", "主线扩散刷新快照(10:00)"),
            (10, 30, "promotion_prediction_1030", "主线扩散刷新快照(10:30)"),
            (13, 5, "promotion_prediction_1305", "主线扩散刷新快照(13:05)"),
        ):
            self.scheduler.add_job(
                self._prewarm_promotion_candidates,
                CronTrigger(hour=hour, minute=minute, day_of_week="mon-fri"),
                id=job_id,
                name=name,
                kwargs={"trigger": job_id},
                coalesce=True,
                max_instances=1,
                misfire_grace_time=180,
            )

        self.scheduler.add_job(
            self._promotion_daily_learning_review,
            CronTrigger(hour=20, minute=20, day_of_week="mon-fri"),
            id="promotion_daily_learning_review",
            name="晋级预测逐日复盘学习(20:20)",
            coalesce=True,
            max_instances=1,
        )

        # [v3.2] 每日盘后复盘报告
        self.scheduler.add_job(
            self._daily_review,
            CronTrigger(hour=20, minute=25, day_of_week="mon-fri"),
            id="daily_review",
            name="每日盘后复盘报告(20:25)",
            coalesce=True,
            max_instances=1,
        )

        # === 时点化三阶段复盘与市场风格快照（不改参、不晋级） ===
        if settings.REVIEW_AUTOMATION_ENABLED:
            for phase, configured_time, fallback, job_id, label in (
                ("premarket", settings.REVIEW_PREMARKET_TIME, (8, 45), "review_snapshot_premarket", "盘前复盘快照"),
                ("intraday", settings.REVIEW_INTRADAY_TIME, (11, 35), "review_snapshot_intraday", "盘中复盘快照"),
                ("postmarket", settings.REVIEW_POSTMARKET_TIME, (20, 35), "review_snapshot_postmarket", "盘后复盘快照"),
            ):
                review_hour, review_minute = _review_schedule_time(configured_time, fallback)
                self.scheduler.add_job(
                    self._review_snapshot_automation,
                    CronTrigger(hour=review_hour, minute=review_minute, day_of_week="mon-fri"),
                    id=job_id,
                    name=f"{label}({review_hour:02d}:{review_minute:02d})",
                    kwargs={"phase": phase},
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=600,
                )
            regime_hour, regime_minute = _review_schedule_time(
                settings.REVIEW_REGIME_TIME, (20, 10)
            )
            self.scheduler.add_job(
                self._market_regime_automation,
                CronTrigger(hour=regime_hour, minute=regime_minute, day_of_week="mon-fri"),
                id="market_regime_snapshot",
                name=f"市场风格快照({regime_hour:02d}:{regime_minute:02d})",
                coalesce=True,
                max_instances=1,
                misfire_grace_time=600,
            )

        # === GPT 复盘报告: 盘后 20:40（默认关闭，需 REVIEW_GPT_ENABLED + REVIEW_GPT_CLI） ===
        gpt_hour, gpt_minute = _review_schedule_time("20:40", (20, 40))
        self.scheduler.add_job(
            self._gpt_review_report,
            CronTrigger(hour=gpt_hour, minute=gpt_minute, day_of_week="mon-fri"),
            id="gpt_review_report",
            name=f"GPT复盘报告({gpt_hour:02d}:{gpt_minute:02d})",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=600,
        )

        # === 派生计算: 板块持续性+强弱+生命周期 每2分钟 ===
        self.scheduler.add_job(
            self._sector_derive, IntervalTrigger(minutes=2),
            id="sector_derive", name="板块派生计算(持续性+强弱+生命周期)",
        )

        # === overview-v2 快照: 每60秒 ===
        self.scheduler.add_job(
            self._dashboard2_snapshot, IntervalTrigger(seconds=60),
            id="dashboard2_snapshot", name="overview-v2 快照刷新(60s)",
        )

        # === 新闻消息面: 盘前/盘中/盘后分批采集，保证晚间和周末催化能进入首板路线 ===
        self.scheduler.add_job(
            self._news_raw_refresh,
            CronTrigger(hour="7-11,13-14,16,19,20", minute=30, day_of_week="mon-fri"),
            id="news_raw_refresh",
            name="新闻原始流刷新(盘前+盘中+盘后)",
        )
        self.scheduler.add_job(
            self._news_ai_refresh,
            CronTrigger(hour="7-11,13-14,16,19,20", minute=35, day_of_week="mon-fri"),
            id="news_ai_refresh",
            name="新闻AI精洗(盘前+盘中+盘后)",
        )
        self.scheduler.add_job(
            self._news_weekend_refresh,
            CronTrigger(hour="9,13,20", minute=40, day_of_week="sat,sun"),
            id="news_weekend_refresh",
            name="周末新闻补爬+精洗",
        )
        self.scheduler.add_job(
            self._news_weekend_refresh,
            CronTrigger(hour=7, minute=10, day_of_week="mon"),
            id="news_monday_weekend_backfill",
            name="周末新闻周一盘前兜底",
        )

        # === 每周末复校异动追高过滤阈值 (周六 21:00) ===
        self.scheduler.add_job(
            self._weekly_chase_review,
            CronTrigger(hour=21, minute=0, day_of_week="sat"),
            id="weekly_chase_review",
            name="异动追高过滤周度复校(周六21:00)",
        )

        # === 数据质量检查: 每5分钟 ===
        self.scheduler.add_job(
            self._quality_check, IntervalTrigger(minutes=5),
            id="quality_check", name="数据质量检查",
            )

        # === 腾讯实时行情: 盘中30秒/轮 ===
        self.scheduler.add_job(
            self._tencent_spot_collect, IntervalTrigger(seconds=30),
            id="tencent_spot", name="腾讯实时行情(30s)",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=10,
        )

        # === 同花顺日K线: 盘后采集 + spot→kline盘中/盘后补全 ===
        # 同花顺CDN收盘后延迟更新(T+1~T+2)，spot有完整当日行情
        # 策略：15:10同花顺首轮 → 盘中+盘后spot每15分钟补kline → 21:00最终兜底
        self.scheduler.add_job(
            self._ths_kline_daily, CronTrigger(hour=15, minute=10, day_of_week="mon-fri"),
            id="ths_kline_daily", name="同花顺日K线(盘后首轮)",
        )
        self.scheduler.add_job(
            self._ths_kline_recent_repair, CronTrigger(hour=21, minute=20, day_of_week="mon-fri"),
            id="ths_kline_recent_repair", name="同花顺日K线(近期异常回补)",
        )
        self.scheduler.add_job(
            self._spot_to_kline_fill, CronTrigger(hour="9-15", minute="*/15", day_of_week="mon-fri"),
            id="spot_to_kline_intraday", name="spot→kline盘中补全(15min)",
        )
        self.scheduler.add_job(
            self._spot_to_kline_fill, CronTrigger(hour=19, minute=0, day_of_week="mon-fri"),
            id="spot_to_kline_19", name="spot→kline盘后补全(19:00)",
            kwargs={"finalize_close": True},
        )
        self.scheduler.add_job(
            self._spot_to_kline_fill, CronTrigger(hour=21, minute=0, day_of_week="mon-fri"),
            id="spot_to_kline_21", name="spot→kline盘后兜底(21:00)",
            kwargs={"finalize_close": True},
        )

        # === 模拟盘60秒 watchdog：健康行情轮次事件才是主触发器 ===
        self.scheduler.add_job(
            self._paper_intraday_auto_trade,
            IntervalTrigger(seconds=settings.PAPER_INTRADAY_AUTO_INTERVAL_SEC),
            id="paper_auto_trade_intraday",
            name=f"模拟盘行情事件故障兜底({settings.PAPER_INTRADAY_AUTO_INTERVAL_SEC}s)",
            coalesce=True,
            max_instances=1,
        )

        # 独立只读研究：不调用15:45业务终态写入，不占交易锁、不补采集。
        for hour, minute, suffix in ((15, 50, "1550"), (20, 45, "2045")):
            self.scheduler.add_job(
                self._publish_paper_research,
                CronTrigger(hour=hour, minute=minute, day_of_week="mon-fri"),
                id=f"paper_research_{suffix}", name=f"模拟盘只读研究归档({hour:02}:{minute:02})",
                coalesce=True, max_instances=1, misfire_grace_time=900,
            )

        # === 模拟盘收盘复盘: 只做演练记录，不再收盘后真实买入 ===
        self.scheduler.add_job(
            self._paper_auto_trade,
            CronTrigger(hour=15, minute=45, day_of_week="mon-fri"),
            id="paper_auto_trade_close",
            name="模拟盘收盘复盘(不下单)",
        )
        self.scheduler.add_job(
            self._settle_momentum_retest_shadow,
            CronTrigger(hour=20, minute=30, day_of_week="mon-fri"),
            id="momentum_retest_shadow_settle",
            name="强势股首次回踩影子结算(20:30)",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=900,
        )
        self.scheduler.add_job(
            self._settle_strategy_iteration_shadow,
            CronTrigger(hour=20, minute=35, day_of_week="mon-fri"),
            id="strategy_iteration_shadow_settle",
            name="ABC3DF形态挑战者影子结算(20:35)",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=900,
        )

        logger.info(
            "采集调度器任务设置完成("
            f"v3.7: 快频10s/慢频30s/新闻盘前+盘中+盘后+周末补爬/晋级预测15:10+20:00+09:25+09:35/"
            f"模拟盘盘中{settings.PAPER_INTRADAY_AUTO_INTERVAL_SEC}s+15:45复盘/"
            "三阶段复盘快照+市场风格台账/新浪5min/派生2min/腾讯30s/竞价兜底/日K+spot→kline盘中15min补全)"
        )
        self._jobs_initialized = True

    async def _run_paper_accounts_isolated(
        self,
        *,
        execute: bool,
        trigger: str,
        execution_mode: str,
        log_all: bool = False,
        quote_payload: dict | None = None,
        include_position_risk: bool = True,
    ) -> None:
        """账户并发读取、独立提交；慢账户不再把后续账户拖到下一行情轮次。"""
        from app.api.v1 import paper

        async def run_one(account_name: str) -> None:
            token = paper._QUOTE_ROUND_CONTEXT.set(quote_payload or {})
            try:
                async with async_session() as session:
                    try:
                        result = await paper.run_paper_auto_trade(
                            session,
                            execute=execute,
                            trigger=trigger,
                            max_candidates=20,
                            execution_mode=execution_mode,
                            account_name=account_name,
                            include_position_risk=include_position_risk,
                        )
                    except Exception:
                        await session.rollback()
                        raise
                summary = result.get("summary") or {}
                if log_all or summary.get("executed") or summary.get("blocked"):
                    logger.info(
                        "模拟盘账户执行完成: "
                        f"run_id={result.get('run_id')}, account={account_name}, summary={summary}"
                    )
            except Exception:
                logger.exception(
                    "模拟盘账户执行失败，其他账户继续: "
                    f"account={account_name}, trigger={trigger}, mode={execution_mode}"
                )
            finally:
                paper._QUOTE_ROUND_CONTEXT.reset(token)

        await asyncio.gather(*(run_one(name) for name in paper.PAPER_SCAN_ACCOUNTS))

    async def _latest_healthy_quote_payload(self) -> dict | None:
        """watchdog复用公共不可变轮次加载器，避免API/调度器出现两套口径。"""
        from app.data.quote_round import load_latest_healthy_quote_payload

        async with async_session() as session:
            payload = await load_latest_healthy_quote_payload(
                session,
                trade_day=date.today(),
                now=datetime.now(),
            )
        if payload:
            payload["watchdog_replay"] = True
        return payload

    async def _expire_pending_paper_buys(self, now: datetime) -> None:
        """Quote outage housekeeping; never enter a fill/entry/position-sell path."""
        from app.api.v1 import paper

        token = paper._QUOTE_ROUND_CONTEXT.set({})
        try:
            # Sequential short transactions avoid twelve concurrent SQLite writers.
            for account_name in (*paper.PAPER_ALL_ACCOUNTS, *paper.PAPER_CHALLENGER_ACCOUNTS):
                async with async_session() as session:
                    await paper.expire_pending_paper_buys(
                        session, account_name=account_name, now=now)
        finally:
            paper._QUOTE_ROUND_CONTEXT.reset(token)

    async def _paper_intraday_auto_trade(self):
        """60秒故障兜底；健康 QuoteRound 正常流动时绝不重复扫描/下单。"""
        logger.debug("模拟盘行情事件 watchdog 触发")
        now = datetime.now()
        if self._last_quote_round_processed_at:
            event_age = (now - self._last_quote_round_processed_at).total_seconds()
            if event_age <= max(60, int(settings.PAPER_INTRADAY_AUTO_INTERVAL_SEC) + 5):
                logger.debug(f"模拟盘watchdog跳过: 行情轮次消费者{event_age:.0f}s前正常完成")
                return
        if self._quote_dispatch_lock.locked():
            logger.debug("模拟盘watchdog跳过: 行情轮次消费者仍在处理")
            return
        if self._paper_auto_trading_last_run_at:
            elapsed_since_last = (datetime.now() - self._paper_auto_trading_last_run_at).total_seconds()
            if elapsed_since_last < max(10, settings.PAPER_INTRADAY_AUTO_INTERVAL_SEC - 1):
                logger.debug(f"模拟盘盘中自动执行跳过: 距上次完成仅{elapsed_since_last:.0f}s")
                return
        if self._paper_auto_trading:
            elapsed = None
            if self._paper_auto_trading_started_at:
                elapsed = (datetime.now() - self._paper_auto_trading_started_at).total_seconds()
            stale_after = max(settings.PAPER_INTRADAY_AUTO_INTERVAL_SEC * 3, 180)
            if elapsed is None or elapsed < stale_after:
                logger.debug("模拟盘盘中自动执行跳过: 上一轮仍在运行")
                return
            logger.warning(
                "模拟盘盘中自动执行重入锁超时，自动释放: "
                f"elapsed={elapsed:.0f}s, stale_after={stale_after}s"
            )
            self._paper_auto_trading = False
            self._paper_auto_trading_started_at = None
        session_name = trade_calendar.get_trade_session()
        if session_name not in {"morning", "afternoon"}:
            logger.debug(f"模拟盘盘中自动执行跳过: 当前交易时段={session_name}")
            return
        self._paper_auto_trading = True
        self._paper_auto_trading_started_at = datetime.now()
        try:
            async with self._quote_dispatch_lock:
                if self._last_quote_round_processed_at:
                    locked_age = (
                        datetime.now() - self._last_quote_round_processed_at
                    ).total_seconds()
                    if locked_age <= max(
                        60,
                        int(settings.PAPER_INTRADAY_AUTO_INTERVAL_SEC) + 5,
                    ):
                        logger.debug(
                            "模拟盘watchdog在取得锁后发现事件消费者已完成，取消重复执行"
                        )
                        return
                quote_payload = await self._latest_healthy_quote_payload()
                if not quote_payload:
                    logger.warning("模拟盘watchdog未找到仍新鲜的完整QuoteRound，仅执行买单过期清理")
                    await self._expire_pending_paper_buys(datetime.now())
                    return
                await self._run_quote_round_position_risk(quote_payload)
                await self._run_paper_accounts_isolated(
                    execute=True,
                    trigger="schedule-intraday-watchdog",
                    execution_mode="intraday",
                    quote_payload=quote_payload,
                    include_position_risk=False,
                )
                completed_at = datetime.now()
                self._paper_auto_trading_last_run_at = completed_at
                self._last_quote_round_processed_at = completed_at
                self._last_quote_round_processed_id = str(
                    quote_payload.get("round_id") or ""
                ) or None
        except Exception as e:
            logger.error(f"模拟盘盘中自动执行失败: {e}")
        finally:
            self._paper_auto_trading = False
            self._paper_auto_trading_started_at = None

    async def _paper_auto_trade(self):
        """收盘后运行模拟盘复盘演练，并写入执行/跳过原因."""
        if self._paper_auto_trading:
            logger.debug("模拟盘自动执行跳过: 上一轮仍在运行")
            return
        self._paper_auto_trading = True
        try:
            await self._run_paper_accounts_isolated(
                execute=False,
                trigger="schedule-close-review",
                execution_mode="close",
                log_all=True,
            )
            from app.api.v1.paper import finalize_paper_daily_outcomes

            async with async_session() as session:
                outcome = await finalize_paper_daily_outcomes(
                    session,
                    trade_date=date.today(),
                    observed_at=datetime.now(),
                )
            logger.info(f"模拟盘每日运行终态已固化: {outcome}")
        except Exception as e:
            logger.error(f"模拟盘自动执行失败: {e}")
        finally:
            self._paper_auto_trading = False

    async def _publish_paper_research(self):
        """Readonly research has its own reentry flag; failures never block trading."""
        if self._paper_research_reporting:
            return {"status": "already_running"}
        self._paper_research_reporting = True
        try:
            from app.paper.research_reports import publish_daily_paper_research
            result = await publish_daily_paper_research()
            logger.info("模拟盘只读研究归档: status={}, sections={}",
                        result.get("status"), result.get("sections"))
            return result
        except Exception as exc:
            logger.warning("模拟盘只读研究归档失败: {}", type(exc).__name__)
            return {"status": "failed", "error_type": type(exc).__name__}
        finally:
            self._paper_research_reporting = False

    async def _settle_momentum_retest_shadow(self):
        """追加式结算影子确认信号；结算结果不反向触发交易。"""
        try:
            from app.paper.momentum_retest_shadow import settle_momentum_retest_shadow

            async with async_session() as session:
                result = await settle_momentum_retest_shadow(session, date.today())
            if result.get("evaluations_added"):
                logger.info(f"强势股首次回踩影子结算完成: {result}")
        except Exception:
            logger.exception("强势股首次回踩影子结算失败")

    async def _settle_strategy_iteration_shadow(self):
        """结算B/C/C3/D/F形态挑战者；结果只进入证据台账。"""
        try:
            from app.paper.strategy_iteration_shadow import (
                finalize_first_board_shadow_sessions,
                settle_strategy_iteration_shadow,
            )

            async with async_session() as session:
                finalization = await finalize_first_board_shadow_sessions(
                    session,
                    date.today(),
                )
                result = await settle_strategy_iteration_shadow(session, date.today())
            if (
                finalization.get("controls_added")
                or finalization.get("outcomes_added")
                or finalization.get("quality_blocks_added")
            ):
                logger.info(f"C3首板候选分母收盘归档完成: {finalization}")
            if result.get("evaluations_added"):
                logger.info(f"ABC3DF形态挑战者影子结算完成: {result}")
        except Exception:
            logger.exception("ABC3DF形态挑战者影子结算失败")

    async def _news_raw_refresh(self):
        """新闻原始流高频入库：覆盖个股、公告、宏观、全球财经和商品快讯."""
        if self._news_raw_refreshing:
            logger.debug("新闻原始流刷新跳过: 上一轮仍在运行")
            return
        self._news_raw_refreshing = True
        try:
            from app.news.engine import news_engine

            items = await news_engine.fetch_all(limit_per_source=100)
            if not items:
                logger.warning("新闻原始流刷新: 未获取到新闻")
                return
            items = sorted(
                items,
                key=lambda item: item.publish_time or datetime.min,
                reverse=True,
            )
            async with async_session() as session:
                saved = await news_engine.cache_raw_items(session, items, reset_dedup=True)
            logger.info(f"新闻原始流刷新完成: fetched={len(items)} saved={saved}")
        except Exception as exc:
            logger.error(f"新闻原始流刷新失败: {exc}")
        finally:
            self._news_raw_refreshing = False

    async def _news_ai_refresh(self):
        """新闻AI精洗：覆盖盘前、盘中和盘后较新样本，生成利好利空、事件和影响映射."""
        if self._news_ai_refreshing:
            logger.debug("新闻AI精洗跳过: 上一轮仍在运行")
            return
        self._news_ai_refreshing = True
        try:
            from app.news.engine import news_engine

            items = await news_engine.fetch_all(limit_per_source=100)
            items = sorted(
                items,
                key=lambda item: item.publish_time or datetime.min,
                reverse=True,
            )[:32]
            if not items:
                logger.warning("新闻AI精洗: 未获取到可处理新闻")
                return
            async with async_session() as session:
                await news_engine.process_and_store(items, db_session=session, reset_dedup=True, concurrency=5)
            logger.info(f"新闻AI精洗完成: processed={len(items)}")
        except Exception as exc:
            logger.error(f"新闻AI精洗失败: {exc}")
        finally:
            self._news_ai_refreshing = False

    async def _news_weekend_refresh(self):
        """周末新闻补爬和精洗，给周一消息催化首板留出提前量."""
        if self._news_weekend_refreshing:
            logger.debug("周末新闻补爬跳过: 上一轮仍在运行")
            return
        self._news_weekend_refreshing = True
        try:
            from app.news.engine import news_engine

            items = await news_engine.fetch_all(limit_per_source=160)
            items = sorted(
                items,
                key=lambda item: item.publish_time or datetime.min,
                reverse=True,
            )
            if not items:
                logger.warning("周末新闻补爬: 未获取到新闻")
                return

            async with async_session() as session:
                saved = await news_engine.cache_raw_items(session, items, reset_dedup=True)

            process_items = items[:80]
            async with async_session() as session:
                processed = await news_engine.process_and_store(
                    process_items,
                    db_session=session,
                    reset_dedup=True,
                    concurrency=5,
                )
            logger.info(
                f"周末新闻补爬完成: fetched={len(items)} saved={saved} analyzed={len(processed)}"
            )
        except Exception as exc:
            logger.error(f"周末新闻补爬失败: {exc}")
        finally:
            self._news_weekend_refreshing = False

    def _record_promotion_runtime_audit(self, kind: str, status: str, **details) -> dict:
        """Bounded runtime view plus durable structured log; never a trading signal."""
        event = {
            "kind": kind, "status": status,
            "observed_at": datetime.now().isoformat(), **details,
        }
        self._promotion_runtime_audit.append(event)
        del self._promotion_runtime_audit[:-100]
        logger.info(f"盘中预测运行审计: {event}")
        return event

    def _set_promotion_phase(self, phase: str | None) -> None:
        now_tick = _time.monotonic()
        if self._promotion_active_phase is not None:
            previous = self._promotion_active_phase
            self._promotion_phase_timings[previous] = round(
                self._promotion_phase_timings.get(previous, 0.0)
                + (now_tick - self._promotion_phase_tick) * 1000, 1,
            )
        self._promotion_active_phase = phase
        self._promotion_phase_tick = now_tick

    async def _pipeline_health_loop(self) -> None:
        """Independent observation only: no DB, calendar fetch, replay or orders."""
        previous_key = None
        while self.scheduler.running:
            try:
                health = self._pipeline_runtime_health.snapshot()
                quote = health["quote"]
                key = (health["observed_at"][:10], health["status"], quote["status"])
                if key != previous_key:
                    observation = {
                        "observed_at": health["observed_at"],
                        "status": health["status"], "quote_status": quote["status"],
                        "active_tail_sec": quote["active_tail_sec"],
                        "max_gap_sec": quote["max_gap_sec"],
                        "scope": health["scope"],
                        "historical_day_completeness": health["historical_day_completeness"],
                    }
                    log = logger.warning if health["status"] == "degraded" else logger.info
                    log("行情/调度观测状态变化: {}", observation)
                    previous_key = key
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("行情/调度健康观察失败；不改变交易状态")
            await asyncio.sleep(15)

    def _on_scheduler_job_event(self, event) -> None:
        observation = self._pipeline_runtime_health.observe_job(event)
        if observation["status"] in {"error", "missed", "max_instances", "business_degraded"}:
            logger.warning("调度运行异常（不等同业务批次完成）: {}", observation)

    def get_pipeline_runtime_status(self) -> dict:
        from app.news.engine import news_engine

        return {
            "contract_version": "intraday_pipeline_v2_nonblocking_news",
            "observed_at": datetime.now().isoformat(),
            "prediction_running": self._promotion_prediction_refreshing,
            "formal_window_recovery": dict(self._promotion_recovery_health),
            "active_context": self._promotion_prediction_active_context,
            "active_phase": self._promotion_active_phase,
            "active_phase_elapsed_ms": (
                round((_time.monotonic() - self._promotion_phase_tick) * 1000, 1)
                if self._promotion_active_phase is not None else None
            ),
            "phase_timings_ms": dict(self._promotion_phase_timings),
            "pending": dict(self._promotion_prediction_pending or {}),
            "news_refresh_running": bool(
                self._promotion_news_refresh_task
                and not self._promotion_news_refresh_task.done()
            ),
            "news_last_result": dict(self._promotion_news_last_result),
            "news_source_checks": {
                code: {
                    **observation,
                    "observed_at": (
                        observation["observed_at"].isoformat()
                        if isinstance(observation.get("observed_at"), datetime)
                        else None
                    ),
                }
                for code, observation in news_engine.get_fetch_health().items()
            },
            "recent_events": [dict(event) for event in self._promotion_runtime_audit[-30:]],
            "confirmation_windows_changed": False,
            "process_awake": self._process_awake_guard.status(),
            "operational_health": self._pipeline_runtime_health.snapshot(),
            "sentiment_funds": {
                **self._sentiment_fund_health,
                "status_counts": dict(self._sentiment_fund_health.get("status_counts", {})),
            },
            "index_history": dict(self._index_history_runtime),
            "kline_observations": {
                **self._kline_observation_health,
                "dispositions": dict(self._kline_observation_health.get("dispositions", {})),
                "quality_issues": dict(self._kline_observation_health.get("quality_issues", {})),
                "examples": [{**row, "issues": list(row.get("issues", []))}
                             for row in self._kline_observation_health.get("examples", [])],
            },
            "kline_price_chain": {
                **self._kline_price_chain_health,
                "examples": [dict(row) for row in self._kline_price_chain_health.get("examples", [])],
            },
            "paper_position_risk": {
                **self._paper_position_risk_health,
                "accounts": [dict(row) for row in self._paper_position_risk_health["accounts"]],
            },
            "concept_fund_flow_pending": bool(
                self._concept_fund_flow_task and not self._concept_fund_flow_task.done()
            ),
        }

    async def _read_promotion_news_health(self) -> dict:
        from app.news.engine import news_engine

        async with async_session() as session:
            source_rows = (
                await session.execute(
                    select(FinanceNews.source, func.max(FinanceNews.publish_time))
                    .where(FinanceNews.source.in_(PROMOTION_NEWS_CRITICAL_SOURCES))
                    .group_by(FinanceNews.source)
                )
            ).all()
        latest_by_source = {
            str(source): published_at
            for source, published_at in source_rows
            if source and isinstance(published_at, datetime)
        }
        return _promotion_news_source_health(
            latest_by_source,
            now=datetime.now(),
            fetch_health=news_engine.get_fetch_health(),
        )

    async def _ensure_fresh_promotion_news(self, *, trigger: str = "schedule") -> dict:
        """Only inspect cached evidence here; fetching/AI never blocks a prediction.

        Returning early does NOT make news executable. Existing per-route quality
        gates and the official snapshot's immutable news cutoff remain mandatory.
        """
        if (
            self._news_raw_refreshing or self._news_ai_refreshing
            or (self._promotion_news_refresh_task and not self._promotion_news_refresh_task.done())
        ):
            return {"status": "refreshing", "fresh": False}
        try:
            health = await asyncio.wait_for(
                self._read_promotion_news_health(),
                timeout=max(0.01, float(settings.PROMOTION_NEWS_CHECK_TIMEOUT_SEC)),
            )
        except Exception as exc:
            self._record_promotion_runtime_audit("news_check", "check_failed", trigger=trigger)
            logger.warning(f"晋级预测新闻新鲜度检查失败: {exc}")
            return {"status": "check_failed", "fresh": False}
        if health["fresh"]:
            return {"status": "fresh", **health}
        if not health["refresh_needed"]:
            return {"status": "checked_no_new_content", **health}
        # Another requester may have scheduled work while the DB read awaited.
        if (
            self._news_raw_refreshing or self._news_ai_refreshing
            or (self._promotion_news_refresh_task and not self._promotion_news_refresh_task.done())
        ):
            return {"status": "refreshing", **health}

        now_tick = _time.monotonic()
        cooldown = max(0.0, float(settings.PROMOTION_NEWS_REFRESH_COOLDOWN_SEC))
        if (
            self._promotion_news_last_attempt is not None
            and now_tick - self._promotion_news_last_attempt < cooldown
        ):
            return {"status": "refresh_cooldown", **health}
        self._promotion_news_last_attempt = now_tick
        self._promotion_news_refresh_task = asyncio.create_task(
            self._run_promotion_news_refresh(trigger=trigger)
        )
        self._record_promotion_runtime_audit(
            "news_refresh", "scheduled", trigger=trigger,
            stale_sources=health["stale_sources"],
        )
        return {"status": "refresh_scheduled", **health}

    async def _run_promotion_news_refresh(self, *, trigger: str) -> dict:
        """Own the background task, its time budget and cleanup separately."""
        started = _time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._refresh_promotion_news_cache(),
                timeout=max(0.01, float(settings.PROMOTION_NEWS_REFRESH_TIMEOUT_SEC)),
            )
        except asyncio.TimeoutError:
            result = {"status": "timeout"}
        except asyncio.CancelledError:
            self._promotion_news_last_result = {"status": "cancelled"}
            raise
        except Exception as exc:
            logger.error(f"晋级预测新闻后台刷新失败: {exc}")
            result = {"status": "refresh_failed"}
        self._promotion_news_last_result = {
            **result, "completed_at": datetime.now().isoformat(),
            "elapsed_ms": round((_time.monotonic() - started) * 1000, 1),
        }
        self._record_promotion_runtime_audit(
            "news_refresh", result["status"], trigger=trigger,
            elapsed_ms=self._promotion_news_last_result["elapsed_ms"],
        )
        return dict(self._promotion_news_last_result)

    async def _refresh_promotion_news_cache(self) -> dict:
        from app.news.engine import news_engine

        if self._news_raw_refreshing or self._news_ai_refreshing:
            return {"status": "refreshing"}
        self._news_raw_refreshing = True
        try:
            items = await news_engine.fetch_all(limit_per_source=PROMOTION_NEWS_REFRESH_LIMIT_PER_SOURCE)
            items = sorted(items, key=lambda item: item.publish_time or datetime.min, reverse=True)
            if not items:
                return {"status": "no_data"}
            async with async_session() as session:
                saved = await news_engine.cache_raw_items(session, items, reset_dedup=True)
        finally:
            self._news_raw_refreshing = False

        # Raw ingestion can resume while AI enriches the already persisted data.
        if self._news_ai_refreshing:
            return {"status": "raw_refreshed", "fetched": len(items), "saved": saved}
        self._news_ai_refreshing = True
        try:
            async with async_session() as session:
                processed = await news_engine.process_and_store(
                    items[:PROMOTION_NEWS_REFRESH_PROCESS_LIMIT],
                    db_session=session, reset_dedup=True, concurrency=5,
                )
            return {
                "status": "refreshed", "fetched": len(items),
                "saved": saved, "analyzed": len(processed),
            }
        finally:
            self._news_ai_refreshing = False

    # =========================================================================
    # 通用写入辅助
    # =========================================================================

    @staticmethod
    async def _batch_upsert(session: AsyncSession, table_class, records: list[dict],
                             unique_cols: list[str], update_cols: list[str] = None):
        """批量 Upsert — INSERT ON CONFLICT DO UPDATE (单条SQL，极速)

        Args:
            table_class: ORM类
            records: 记录列表
            unique_cols: 冲突检测列(ON CONFLICT)
            update_cols: 冲突时更新列(空=更新全部非主键列)

        性能: 5190条FundFlow: 逐条upsert≈5s, 批量INSERT≈50ms (100x提速)
        """
        if not records:
            return
        if table_class is StockKline:
            raise ValueError("StockKline requires append-only observations; generic historical upsert is disabled")
        if table_class is StockDaily and any(row.get("trade_date") != date.today() for row in records):
            # Recheck at the write boundary as network awaits can cross midnight.
            # Historical index responses belong in observed evidence, not in the
            # original daily projection (including absent or invalid old rows).
            raise ValueError("StockDaily projection writes require the actual current date")

        from sqlalchemy import text as sa_text

        table = table_class.__table__
        table_name = table.name
        columns = [c.name for c in table.columns if c.name != "id"]

        # 构建列名列表
        col_str = ", ".join(columns)
        # 参数占位符(每条记录一组)
        param_str = ", ".join([f":{c}" for c in columns])
        # ON CONFLICT 列
        conflict_str = ", ".join(unique_cols)
        # DO UPDATE SET 列
        if update_cols is None:
            update_cols = [c for c in columns if c not in unique_cols]
        update_str = ", ".join([f"{c} = EXCLUDED.{c}" for c in update_cols])

        sql = (
            f"INSERT INTO {table_name} ({col_str}) "
            f"VALUES ({param_str}) "
            f"ON CONFLICT ({conflict_str}) DO UPDATE SET {update_str}"
        )

        # 只保留columns中存在的字段，清理数据
        clean_records = []
        for r in records:
            clean = {}
            for c in columns:
                if c in r:
                    val = r[c]
                    # datetime 是 date 的子类；与 ORM DateTime 存储保持空格及微秒口径。
                    if isinstance(val, datetime):
                        val = val.isoformat(sep=" ", timespec="microseconds")
                    elif isinstance(val, date):
                        val = val.isoformat()
                    clean[c] = val
            clean_records.append(clean)

        # 逐条执行(SQLite不支持VALUES多行，但aiosqlite的execute可批量传参)
        stmt = sa_text(sql)
        for clean in clean_records:
            await session.execute(stmt, clean)

    @staticmethod
    async def _upsert_sector_info(session: AsyncSession, records: list[dict]):
        """Upsert 板块信息 → SectorInfo 表

        records 字段要求:
          sector_code: str  (唯一键)
          sector_name: str
          sector_type: str  (industry/concept/sw_l1/sw_l2/sw_l3)
          source: str       (akshare/eastmoney/sw/sina)
          parent_code: Optional[str]
          stock_count: Optional[int]
          avg_pe: Optional[float]
          avg_pb: Optional[float]
          avg_dividend: Optional[float]
        """
        for r in records:
            existing = await session.execute(
                select(SectorInfo).where(SectorInfo.sector_code == r["sector_code"])
            )
            row = existing.scalar_one_or_none()
            if row:
                for k, v in r.items():
                    setattr(row, k, v)
            else:
                session.add(SectorInfo(**r))
        await session.flush()

    @staticmethod
    async def _upsert_stock_sector_mapping(session: AsyncSession, records: list[dict]):
        """Upsert 个股→板块映射 → StockSectorMapping 表

        records 字段要求:
          code: str
          sector_code: str
          sector_name: Optional[str]
          sector_type: Optional[str]
          source: str
          source_version: str
          observed_at: datetime
          weight: Optional[float]
        """
        for r in records:
            existing = await session.execute(
                select(StockSectorMapping).where(
                    StockSectorMapping.code == r["code"],
                    StockSectorMapping.sector_code == r["sector_code"],
                    StockSectorMapping.source == r["source"],
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                for k, v in r.items():
                    setattr(row, k, v)
            else:
                session.add(StockSectorMapping(**r))
        await session.flush()

    @staticmethod
    async def _upsert_limit_up(session: AsyncSession, records: list[dict]):
        """Upsert 涨停池 → LimitUpPool 表

        records 字段要求:
          code: str, name: Optional[str], trade_date: date
          limit_up_time: Optional[str], limit_up_price: Optional[float]
          seal_amount: Optional[float], break_count: Optional[int]
          consecutive_days: Optional[int], limit_up_reason: Optional[str]
          turnover: Optional[float], source: str
        """
        for r in records:
            existing = await session.execute(
                select(LimitUpPool).where(
                    LimitUpPool.code == r["code"],
                    LimitUpPool.trade_date == r["trade_date"],
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                for k, v in r.items():
                    setattr(row, k, v)
            else:
                session.add(LimitUpPool(**r))
        await session.flush()

    @staticmethod
    async def _upsert_limit_down(session: AsyncSession, records: list[dict]):
        """Upsert 跌停池 → LimitDownPool 表"""
        for r in records:
            existing = await session.execute(
                select(LimitDownPool).where(
                    LimitDownPool.code == r["code"],
                    LimitDownPool.trade_date == r["trade_date"],
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                for k, v in r.items():
                    setattr(row, k, v)
            else:
                session.add(LimitDownPool(**r))
        await session.flush()

    @staticmethod
    async def _upsert_broken_limit(session: AsyncSession, records: list[dict]):
        """Upsert 炸板池 → BrokenLimitPool 表"""
        for r in records:
            existing = await session.execute(
                select(BrokenLimitPool).where(
                    BrokenLimitPool.code == r["code"],
                    BrokenLimitPool.trade_date == r["trade_date"],
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                for k, v in r.items():
                    setattr(row, k, v)
            else:
                session.add(BrokenLimitPool(**r))
        await session.flush()

    @staticmethod
    async def _upsert_fund_flow(session: AsyncSession, records: list[dict]):
        """Upsert 资金流 → FundFlow 表

        records 字段要求:
          code: str, name: Optional[str], trade_date: date
          main_net_inflow: Optional[float], main_net_inflow_pct: Optional[float]
          big_net_inflow: Optional[float], mid_net_inflow: Optional[float]
          small_net_inflow: Optional[float]
        """
        for r in records:
            existing = await session.execute(
                select(FundFlow).where(
                    FundFlow.code == r["code"],
                    FundFlow.trade_date == r["trade_date"],
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                for k, v in r.items():
                    setattr(row, k, v)
            else:
                session.add(FundFlow(**r))
        await session.flush()

    # =========================================================================
    # 采集方法
    # =========================================================================

    async def _update_stock_status(self):
        """只合并问财正向风险；列表缺席不是摘帽/复牌/退市风险解除证据。"""
        if await trade_calendar.is_trade_day() is not True:
            return {"status": "blocked", "reason": "交易日未明确确认"}

        started_at = datetime.now()
        today = started_at.date()
        t_fetch = _time.monotonic()
        loop = asyncio.get_event_loop()
        queries = ("ST股", "停牌", "*ST股", "北交所ST股")
        import pywencai as _pw
        # 问财自 2026-08 下旬起要求登录会话；cookie 从 settings（`.env`）读取。
        # 仅在非空时才传：库的 headers() 会把 None 原样当字符串写进 cookie 头。
        _pw_extra = {}
        _pw_cookie = str(getattr(settings, "PYWENCAI_COOKIE", "") or "").strip()
        if _pw_cookie:
            _pw_extra["cookie"] = _pw_cookie
        try:
            frames = await asyncio.gather(*[
                loop.run_in_executor(
                    None, lambda q=q: _pw.get(query=q, loop=True, **_pw_extra)
                )
                for q in queries
            ])
            if any(not isinstance(frame, pd.DataFrame) for frame in frames):
                raise TypeError("pywencai股票状态返回类型异常")
            if all(frame.empty for frame in frames):
                raise ValueError("pywencai股票状态四个查询均为空，禁止清空既有风控标签")
            maps = []
            seen_names = {}
            for query, frame in zip(queries, frames):
                parsed = {}
                if not frame.empty:
                    if not {"股票代码", "股票简称"} <= set(frame.columns):
                        raise ValueError(f"{query}: 缺少股票代码/股票简称列")
                    for _, row in frame.iterrows():
                        raw_code, raw_name = row["股票代码"], row["股票简称"]
                        # 不把float/NaN/短码/错误交易所前后缀猜成证券身份。
                        code = _normalize_code(raw_code) if isinstance(raw_code, str) else ""
                        board = stock_tagger.get_board_type(code)
                        exchange = "sh" if board in {"main_sh", "star"} else "bj" if board == "bse" else "sz"
                        valid_formats = {code, exchange + code, code + "." + exchange}
                        name = stock_tagger.clean_name(raw_name)
                        if board == "unknown" or raw_code.strip().lower() not in valid_formats or not name:
                            raise ValueError(f"{query}: 无效证券身份，整批禁止替换")
                        if code in seen_names and seen_names[code] != name:
                            raise ValueError(f"{query}: 同代码名称冲突，整批禁止替换")
                        seen_names[code] = name
                        parsed[code] = name
                maps.append(parsed)
            st_map, suspended_map, delisting_risk_map, bse_st_map = maps
            # *ST 是退市风险，不是已退市事实；同时必须保留ST风险。
            st_map = {**st_map, **delisting_risk_map, **bse_st_map}
            all_codes = set(st_map) | set(suspended_map)
            if not all_codes or datetime.now().date() != today:
                raise ValueError("未解析出有效风险标的或查询已跨日")
        except Exception as exc:
            async with async_session() as session:
                await data_quality_guard.record_failure(
                    session, "pywencai", "stock_status", str(exc),
                    latency_ms=int((_time.monotonic() - t_fetch) * 1000),
                )
            return {"status": "failed", "reason": str(exc)}

        async with async_session() as session:
            try:
                tags = {t.code: t for t in (await session.scalars(
                    select(StockTag).where(StockTag.code.in_(all_codes))
                )).all()}
                blacklists = {b.code: b for b in (await session.scalars(
                    select(StockBlacklist).where(StockBlacklist.code.in_(all_codes))
                )).all()}
                for code in sorted(all_codes):
                    tag = tags.get(code)
                    if tag is None:
                        tag = StockTag(code=code, board_type=stock_tagger.get_board_type(code),
                                       board_tag=stock_tagger.get_board_tag(stock_tagger.get_board_type(code)))
                        session.add(tag)
                    old_st, old_delisting = stock_tagger.name_risks(tag.name)
                    tag.name = seen_names[code]
                    name_st, name_delisting = stock_tagger.name_risks(tag.name)
                    tag.is_st = bool(tag.is_st or code in st_map or old_st or name_st)
                    tag.is_suspended = bool(tag.is_suspended or code in suspended_map)
                    tag.is_delisting = bool(tag.is_delisting or code in delisting_risk_map or old_delisting or name_delisting)
                    tag.board_type = stock_tagger.get_board_type(code)
                    tag.board_tag = "suspended" if tag.is_suspended or tag.board_tag == "suspended" else "blocked"
                    tag.updated_at = datetime.now()
                    # 保留原名单所有字段(人工/起始日/到期日/原因)，不删除或重开旧行。
                    # 新风险仍由StockTag拦截；单行PK不是完整风险事件历史。
                    if code not in blacklists:
                        session.add(StockBlacklist(
                            code=code,
                            reason="delisting" if tag.is_delisting else "suspended" if tag.is_suspended else "st",
                            start_date=today, end_date=None, auto_expire=False, source="auto",
                        ))
                await session.commit()
                empty_queries = [q for q, f in zip(queries, frames) if f.empty]
                result = {
                    "status": "degraded", "positive_merge_status": "ok",
                    "record_count": len(all_codes), "empty_queries": empty_queries,
                    "removal_policy": "positive_only_no_automatic_clear",
                    "coverage_verified": False, "automatic_clear_count": 0,
                    "delisting_evidence": "risk_warning_not_delisted_fact",
                }
                # 即使四个列表非空，也没有来源总量/逐代码解除的证据。
                # record_success默认完整率=1，不能把正向合并冒充全量状态健康。
                await data_quality_guard.record_failure(
                    session, "pywencai", "stock_status",
                    ("部分查询为空；" if empty_queries else "")
                    + f"已正向合并{len(all_codes)}只风险标的；全量覆盖和风险解除未核验，禁止自动清除",
                    latency_ms=int((_time.monotonic() - t_fetch) * 1000),
                )
                logger.info(f"股票状态正向合并: {result}")
                return result
            except Exception as exc:
                await session.rollback()
                await data_quality_guard.record_failure(session, "pywencai", "stock_status", str(exc))
                return {"status": "failed", "reason": str(exc)}


    @staticmethod
    async def _upsert_blacklist(session: AsyncSession, code: str, reason: str,
                                 start_date: date, source: str = "auto"):
        """Upsert 股票黑名单 → StockBlacklist 表

        reason: st / delisting / suspended / ipo_recent
        """
        bl = await session.get(StockBlacklist, code)
        if bl:
            # 已存在且reason相同 → 更新start_date
            if bl.reason == reason:
                bl.start_date = start_date
                bl.end_date = None  # 重置结束日期
                bl.source = source
            else:
                # reason不同 → 更新为新reason
                bl.reason = reason
                bl.start_date = start_date
                bl.end_date = None
                bl.source = source
        else:
            bl = StockBlacklist(
                code=code, reason=reason,
                start_date=start_date, end_date=None,
                auto_expire=True, source=source,
            )
            session.add(bl)
        await session.commit()

    async def _pre_market(self):
        """盘前准备 — pywencai板块列表→SectorInfo + akshare板块列表补充 + 申万行业→SectorInfo
        
        板块口径(统一用pywencai):
        - 行业板块: pywencai全量5197只个股→去重257个三级行业/31个一级行业
        - 概念板块: pywencai全量5197只个股→去重389个概念
        - akshare/申万/新浪作为补充源, 不在板块营地页面展示申万
        """
        # 清空可交易代码缓存, 盘前刷新
        self._tradeable_codes = []
        if not await trade_calendar.is_trade_day():
            return
        logger.info("📊 盘前数据准备开始...")
        t0 = _time.monotonic()
        async with async_session() as session:
            pw = self._sources["pywencai"]
            ak_src = self._sources["akshare"]
            sw = self._sources["shenwan"]

            # ---- 1. pywencai 个股映射→提取行业+概念板块列表 → SectorInfo ----
            try:
                t1 = _time.monotonic()
                df_mapping = await pw.get_stock_industry_mapping()
                if df_mapping is not None and len(df_mapping) > 0:
                    # 1a. 提取行业板块(三级全名"医药生物-中药-中药Ⅲ", 与collect_pywencai_sectors.py一致)
                    industries = set()
                    industry_counts = {}  # 行业→成分股数
                    for _, row in df_mapping.iterrows():
                        ind = str(row.get("所属同花顺行业", ""))
                        if ind and ind != "nan":
                            ind = ind.strip()
                            industries.add(ind)
                            industry_counts[ind] = industry_counts.get(ind, 0) + 1
                    
                    records = []
                    for ind_name in sorted(industries):
                        records.append({
                            "sector_code": f"pw_industry_{ind_name}",
                            "sector_name": ind_name,
                            "sector_type": "industry",
                            "source": "pywencai",
                            "stock_count": industry_counts.get(ind_name, 0),
                        })
                    await self._upsert_sector_info(session, records)
                    logger.info(f"pywencai行业板块写入: {len(records)}个(一级)")
                    
                    # 1b. 提取概念板块(分号分隔)
                    concepts = set()
                    concept_counts = {}
                    for _, row in df_mapping.iterrows():
                        con_str = str(row.get("所属概念", ""))
                        if con_str and con_str != "nan":
                            for concept in con_str.split(";"):
                                concept = concept.strip()
                                if concept:
                                    concepts.add(concept)
                                    concept_counts[concept] = concept_counts.get(concept, 0) + 1
                    
                    records = []
                    for con_name in sorted(concepts):
                        records.append({
                            "sector_code": f"pw_concept_{con_name}",
                            "sector_name": con_name,
                            "sector_type": "concept",
                            "source": "pywencai",
                            "stock_count": concept_counts.get(con_name, 0),
                        })
                    await self._upsert_sector_info(session, records)
                    logger.info(f"pywencai概念板块写入: {len(records)}个")
                    
                    await data_quality_guard.record_success(
                        session, "pywencai", "sector_list",
                        latency_ms=int((_time.monotonic() - t1) * 1000),
                        record_count=len(industries) + len(concepts),
                    )
                else:
                    logger.warning("pywencai映射返回空, 跳过板块列表")
            except Exception as e:
                logger.error(f"盘前pywencai板块列表失败: {e}")
                await data_quality_guard.record_failure(
                    session, "pywencai", "sector_list", str(e),
                )

            # ---- 2. 同花顺概念板块列表(补充akshare源) → SectorInfo ----
            try:
                t2 = _time.monotonic()
                df_concept = await ak_src.get_sector_list("concept")
                records = []
                for _, row in df_concept.iterrows():
                    records.append({
                        "sector_code": f"ak_concept_{row['code']}",
                        "sector_name": str(row["name"]),
                        "sector_type": "concept",
                        "source": "akshare",
                        "stock_count": None,
                    })
                await self._upsert_sector_info(session, records)
                await data_quality_guard.record_success(
                    session, "akshare", "concept_list",
                    latency_ms=int((_time.monotonic() - t2) * 1000),
                    record_count=len(records),
                )
                logger.info(f"akshare概念板块写入: {len(records)}条")
            except Exception as e:
                logger.error(f"盘前akshare概念板块失败: {e}")
                await data_quality_guard.record_failure(
                    session, "akshare", "concept_list", str(e),
                )

            # ---- 3. 同花顺行业板块列表(补充akshare源) → SectorInfo ----
            try:
                t3 = _time.monotonic()
                df_industry = await ak_src.get_sector_list("industry")
                records = []
                for _, row in df_industry.iterrows():
                    records.append({
                        "sector_code": f"ak_industry_{row['code']}",
                        "sector_name": str(row["name"]),
                        "sector_type": "industry",
                        "source": "akshare",
                        "stock_count": None,
                    })
                await self._upsert_sector_info(session, records)
                await data_quality_guard.record_success(
                    session, "akshare", "industry_list",
                    latency_ms=int((_time.monotonic() - t3) * 1000),
                    record_count=len(records),
                )
                logger.info(f"akshare行业板块写入: {len(records)}条")
            except Exception as e:
                logger.error(f"盘前akshare行业板块失败: {e}")
                await data_quality_guard.record_failure(
                    session, "akshare", "industry_list", str(e),
                )

            # ---- 4. 申万一级行业 → SectorInfo (不在板块营地展示, 但保留采集) ----
            try:
                t2 = _time.monotonic()
                df_sw1 = await sw.get_sw_index_list(1)
                # 返回: 行业代码, 行业名称, 成份个数, 静态市盈率, TTM(滚动)市盈率, 市净率, 静态股息率
                records = []
                for _, row in df_sw1.iterrows():
                    records.append({
                        "sector_code": str(row["行业代码"]),
                        "sector_name": str(row["行业名称"]),
                        "sector_type": "sw_l1",
                        "source": "shenwan",
                        "parent_code": None,
                        "stock_count": _safe_int(row.get("成份个数")),
                        "avg_pe": _safe_float(row.get("静态市盈率")),
                        "avg_pb": _safe_float(row.get("市净率")),
                        "avg_dividend": _safe_float(row.get("静态股息率")),
                    })
                await self._upsert_sector_info(session, records)
                await data_quality_guard.record_success(
                    session, "shenwan", "sw_l1_list",
                    latency_ms=int((_time.monotonic() - t2) * 1000),
                    record_count=len(records),
                )
                logger.info(f"申万一级写入: {len(records)}条")
            except Exception as e:
                logger.error(f"盘前申万一级失败: {e}")
                await data_quality_guard.record_failure(
                    session, "shenwan", "sw_l1_list", str(e),
                )

            # ---- 4. 申万二级行业 → SectorInfo ----
            try:
                t3 = _time.monotonic()
                df_sw2 = await sw.get_sw_index_list(2)
                # 返回: 行业代码, 行业名称, 上级行业, 成份个数, 静态市盈率, TTM市盈率, 市净率, 静态股息率
                records = []
                for _, row in df_sw2.iterrows():
                    # 上级行业是名称，需要反查code
                    parent_name = str(row.get("上级行业", ""))
                    parent_code = await self._find_sector_code_by_name(
                        session, parent_name, "sw_l1"
                    )
                    records.append({
                        "sector_code": str(row["行业代码"]),
                        "sector_name": str(row["行业名称"]),
                        "sector_type": "sw_l2",
                        "source": "shenwan",
                        "parent_code": parent_code,
                        "stock_count": _safe_int(row.get("成份个数")),
                        "avg_pe": _safe_float(row.get("静态市盈率")),
                        "avg_pb": _safe_float(row.get("市净率")),
                        "avg_dividend": _safe_float(row.get("静态股息率")),
                    })
                await self._upsert_sector_info(session, records)
                await data_quality_guard.record_success(
                    session, "shenwan", "sw_l2_list",
                    latency_ms=int((_time.monotonic() - t3) * 1000),
                    record_count=len(records),
                )
                logger.info(f"申万二级写入: {len(records)}条")
            except Exception as e:
                logger.error(f"盘前申万二级失败: {e}")
                await data_quality_guard.record_failure(
                    session, "shenwan", "sw_l2_list", str(e),
                )

            # ---- 5. 申万三级行业 → SectorInfo ----
            try:
                t4 = _time.monotonic()
                df_sw3 = await sw.get_sw_index_list(3)
                records = []
                for _, row in df_sw3.iterrows():
                    parent_name = str(row.get("上级行业", ""))
                    parent_code = await self._find_sector_code_by_name(
                        session, parent_name, "sw_l2"
                    )
                    records.append({
                        "sector_code": str(row["行业代码"]),
                        "sector_name": str(row["行业名称"]),
                        "sector_type": "sw_l3",
                        "source": "shenwan",
                        "parent_code": parent_code,
                        "stock_count": _safe_int(row.get("成份个数")),
                        "avg_pe": _safe_float(row.get("静态市盈率")),
                        "avg_pb": _safe_float(row.get("市净率")),
                        "avg_dividend": _safe_float(row.get("静态股息率")),
                    })
                await self._upsert_sector_info(session, records)
                await data_quality_guard.record_success(
                    session, "shenwan", "sw_l3_list",
                    latency_ms=int((_time.monotonic() - t4) * 1000),
                    record_count=len(records),
                )
                logger.info(f"申万三级写入: {len(records)}条")
            except Exception as e:
                logger.error(f"盘前申万三级失败: {e}")
                await data_quality_guard.record_failure(
                    session, "shenwan", "sw_l3_list", str(e),
                )

            # ---- 6. 新浪概念板块列表 → SectorInfo ----
            try:
                t5 = _time.monotonic()
                sina = self._sources["sina"]
                df_sina_concept = await sina.get_sector_list()
                # stock_sector_spot 返回: label, 板块, 公司家数, ...
                records = []
                for _, row in df_sina_concept.iterrows():
                    label = str(row.get("label", ""))
                    if not label.startswith("gn_"):
                        continue  # 只取概念板块
                    records.append({
                        "sector_code": f"sina_{label}",
                        "sector_name": str(row["板块"]),
                        "sector_type": "concept",
                        "source": "sina",
                        "stock_count": _safe_int(row.get("公司家数")),
                    })
                await self._upsert_sector_info(session, records)
                await data_quality_guard.record_success(
                    session, "sina", "concept_list",
                    latency_ms=int((_time.monotonic() - t5) * 1000),
                    record_count=len(records),
                )
                logger.info(f"新浪概念写入: {len(records)}条")
            except Exception as e:
                logger.error(f"盘前新浪概念失败: {e}")
                await data_quality_guard.record_failure(
                    session, "sina", "concept_list", str(e),
                )

            await session.commit()
            logger.info(f"📊 盘前数据准备完成, 耗时{_time.monotonic()-t0:.1f}s")

    async def _intraday_fast(self):
        """盘中快频 — 东财涨停/跌停/炸板 并行采集+批量写入

        优化: 3个API asyncio.gather并行(从串行6s→并行2s) + 批量upsert
        频率: 10秒/次, 端到端延时≈3秒
        """
        if not await trade_calendar.is_trading_hours():
            return

        t_total = _time.monotonic()
        em = self._sources["eastmoney"]
        today = date.today()
        today_str = today.strftime("%Y%m%d")

        # ===== 并行采集3个池 =====
        df_limit_up = pd.DataFrame()
        df_limit_down = pd.DataFrame()
        df_broken = pd.DataFrame()
        limit_structure_changed = False
        current_limit_up_codes: set[str] | None = None

        try:
            df_limit_up, df_limit_down, df_broken = await asyncio.gather(
                em.get_limit_up_pool(today_str),
                em.get_limit_down_pool(today_str),
                em.get_broken_limit_pool(today_str),
                return_exceptions=True,
            )
        except Exception as e:
            logger.error(f"盘中快频并行采集异常: {e}")
            return

        fetch_ms = int((_time.monotonic() - t_total) * 1000)

        async with async_session() as session:
            try:
                # ---- 1. 涨停池 批量写入 ----
                # 有效的空 DataFrame 也是一份“当前为空”的完整快照，必须清退旧记录；
                # 只有采集异常（return_exceptions 返回的 Exception）才保留上一帧。
                if isinstance(df_limit_up, pd.DataFrame):
                    limit_up_records = self._parse_limit_up_df(
                        df_limit_up, today, source="eastmoney"
                    )
                    if limit_up_records:
                        await self._batch_upsert(
                            session, LimitUpPool, limit_up_records,
                            unique_cols=["code", "trade_date"],
                            update_cols=["name", "limit_up_time", "limit_up_price",
                                         "seal_amount", "break_count", "consecutive_days",
                                         "limit_up_reason", "turnover", "source"],
                        )

                    # 东财涨停池是“当前仍封板”快照：凡不在本帧中的东财旧记录均清退。
                    alive_codes = {r["code"] for r in limit_up_records}
                    if (
                        self._last_intraday_limit_up_codes is None
                        or alive_codes != self._last_intraday_limit_up_codes
                    ):
                        limit_structure_changed = True
                    current_limit_up_codes = alive_codes
                    stale_result = await session.execute(
                        select(LimitUpPool.id, LimitUpPool.code).where(
                            LimitUpPool.trade_date == today,
                            LimitUpPool.source == "eastmoney",
                        )
                    )
                    deleted_codes = []
                    for sid, scode in stale_result.all():
                        if scode not in alive_codes:
                            await session.execute(
                                delete(LimitUpPool).where(LimitUpPool.id == sid)
                            )
                            deleted_codes.append(scode)
                    if deleted_codes:
                        logger.info(f"清退非当前封板股票: {deleted_codes}")

                    await data_quality_guard.record_success(
                        session, "eastmoney", "limit_up",
                        latency_ms=fetch_ms, record_count=len(limit_up_records),
                    )

                # ---- 2. 跌停池 批量写入 ----
                if isinstance(df_limit_down, pd.DataFrame) and len(df_limit_down) > 0:
                    records = self._parse_limit_down_df(df_limit_down, today, source="eastmoney")
                    if records:
                        await self._batch_upsert(
                            session, LimitDownPool, records,
                            unique_cols=["code", "trade_date"],
                            update_cols=["name", "limit_down_time", "break_count",
                                         "consecutive_days", "reason", "source"],
                        )
                    await data_quality_guard.record_success(
                        session, "eastmoney", "limit_down",
                        latency_ms=fetch_ms, record_count=len(records),
                    )

                # ---- 3. 炸板池 批量写入 ----
                if isinstance(df_broken, pd.DataFrame):
                    broken_records = self._parse_broken_limit_df(
                        df_broken, today, source="eastmoney"
                    )

                    # 最终状态口径：当前仍在涨停池的股票不能同时出现在炸板池。
                    # 若本轮涨停池采集失败，则不凭不完整信息清理交集。
                    if current_limit_up_codes is not None:
                        broken_records = [
                            row for row in broken_records
                            if row["code"] not in current_limit_up_codes
                        ]
                        if current_limit_up_codes:
                            await session.execute(
                                delete(BrokenLimitPool).where(
                                    BrokenLimitPool.trade_date == today,
                                    BrokenLimitPool.code.in_(current_limit_up_codes),
                                )
                            )

                    if broken_records:
                        await self._batch_upsert(
                            session, BrokenLimitPool, broken_records,
                            unique_cols=["code", "trade_date"],
                            update_cols=["name", "limit_up_time", "break_time",
                                         "seal_duration", "seal_amount", "source",
                                         "limit_up_price", "close_price",
                                         "close_at_limit", "final_state"],
                        )

                    # 炸板池同样是完整当前快照：清退不在本帧内的东财旧记录。
                    current_broken_codes = {row["code"] for row in broken_records}
                    stale_broken_result = await session.execute(
                        select(BrokenLimitPool.id, BrokenLimitPool.code).where(
                            BrokenLimitPool.trade_date == today,
                            BrokenLimitPool.source == "eastmoney",
                        )
                    )
                    for broken_id, broken_code in stale_broken_result.all():
                        if broken_code not in current_broken_codes:
                            await session.execute(
                                delete(BrokenLimitPool).where(
                                    BrokenLimitPool.id == broken_id
                                )
                            )

                    await data_quality_guard.record_success(
                        session, "eastmoney", "broken_limit",
                        latency_ms=fetch_ms, record_count=len(broken_records),
                    )

                await session.commit()
                total_ms = int((_time.monotonic() - t_total) * 1000)
                logger.debug(f"盘中快频完成: 采集{fetch_ms}ms+写入{total_ms-fetch_ms}ms={total_ms}ms")
                if current_limit_up_codes is not None:
                    self._last_intraday_limit_up_codes = current_limit_up_codes
                if limit_structure_changed:
                    self._request_anomaly_scan("eastmoney_limit_structure")

            except Exception as e:
                logger.error(f"盘中快频写入失败: {e}")
                await session.rollback()

    async def _collect_concept_fund_flow_bounded(self):
        """Keep one legacy AkShare call alive without holding up individual funds.

        Shielding preserves the global AkShare/V8 serialization lock while the
        underlying blocking thread runs; a scheduler retry never spawns another.
        """
        task = self._concept_fund_flow_task
        if task is None:
            task = asyncio.create_task(self._sources["akshare"].get_sector_fund_flow("概念"))
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
            self._concept_fund_flow_task = task
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=max(0.01, float(settings.FUND_FLOW_CONCEPT_WAIT_TIMEOUT_SEC)),
            )
        except asyncio.TimeoutError:
            raise TimeoutError("概念资金元数据等待超时，保留单一在途请求；不阻塞个股资金入库") from None
        finally:
            if task.done() and self._concept_fund_flow_task is task:
                self._concept_fund_flow_task = None

    async def _intraday_slow(self):
        """30秒慢频：个股资金与概念元数据并行，概念慢源不得无限阻塞入库。"""
        if not await trade_calendar.is_trading_hours():
            return

        t_total = _time.monotonic()
        tencent_src = self._sources["tencent"]
        today = date.today()
        # Reuse the existing quote universe, not per-account requests. End this
        # read transaction before HTTP; the provider handles bounded fair rotation.
        async with async_session() as universe_session:
            universe_rows = (await universe_session.execute(
                select(StockSpot.code, StockSpot.name)
            )).all()
        names = {str(code): name for code, name in universe_rows}
        codes = sorted(set(self._tradeable_codes) | set(names))
        individual_records: list[dict] = []
        individual_provider = individual_source = "tencent"
        individual_source_version = "tencent_hsfundtab_v1"
        primary_error = ""

        # Old individual EastMoney/AkShare/THS fallbacks have been removed from
        # the live route. Other EastMoney datasets and historical evidence remain.
        df_individual, df_concept = await asyncio.gather(
            tencent_src.get_individual_fund_flow(codes),
            self._collect_concept_fund_flow_bounded(),
            return_exceptions=True,
        )
        if isinstance(df_individual, Exception):
            primary_error = f"腾讯资金采集失败: {type(df_individual).__name__}"
        elif isinstance(df_individual, pd.DataFrame) and not df_individual.empty:
            if (df_individual.attrs.get("fund_flow_source") != individual_source
                    or df_individual.attrs.get("fund_flow_source_version") != individual_source_version):
                primary_error = "腾讯资金帧合同缺失或不匹配"
            else:
                individual_records = self._parse_individual_fund_flow_df(
                    df_individual, today, source=individual_source,
                    source_version=individual_source_version,
                )
                requested = set(codes)
                individual_records = [row for row in individual_records if row["code"] in requested]
                for row in individual_records:
                    row["name"] = names.get(row["code"]) or row["name"]
                if not individual_records:
                    primary_error = "腾讯资金帧无有效主力字段、代码或同日新鲜源时钟"
        else:
            primary_error = "腾讯资金本轮无有效数据（空帧、退避或缺失），不以旧源/零填补"
        if isinstance(df_individual, pd.DataFrame):
            diagnostics = df_individual.attrs.get("fund_flow_errors") or {}
            if diagnostics:
                logger.warning(f"[tencent] 资金有界采集诊断: {diagnostics}")
                if primary_error:
                    primary_error += f"; reasons={diagnostics}"

        fetch_ms = int((_time.monotonic() - t_total) * 1000)

        async with async_session() as session:
            try:
                # ---- 1. 个股资金流 批量写入 ----
                individual_expected_count = (
                    max(len(df_individual), int(df_individual.attrs.get("fund_flow_expected_count", len(df_individual))))
                    if isinstance(df_individual, pd.DataFrame) else 0
                )
                if primary_error:
                    await data_quality_guard.record_failure(
                        session, "tencent", "individual_fund_flow", primary_error,
                        latency_ms=fetch_ms,
                    )
                if individual_records:
                    # A regressing provider cache must not replace a newer daily
                    # watermark, even when the regressed point is <10 minutes old.
                    previous = (await session.execute(
                        select(FundFlow.code, FundFlow.source_quote_at, FundFlow.received_at).where(
                            FundFlow.trade_date == today,
                            FundFlow.code.in_([row["code"] for row in individual_records]),
                        )
                    )).all()
                    previous_clocks = {code: (evidence_clock(source_at), evidence_clock(receipt))
                                       for code, source_at, receipt in previous}
                    individual_records = [
                        row for row in individual_records
                        if row["code"] not in previous_clocks or (
                            (previous_clocks[row["code"]][0] is None
                             or row["source_quote_at"] >= previous_clocks[row["code"]][0])
                            and (previous_clocks[row["code"]][1] is None
                                 or row["received_at"] >= previous_clocks[row["code"]][1])
                        )
                    ]
                if individual_records:
                    await self._batch_upsert(
                        session, FundFlow, individual_records,
                        unique_cols=["code", "trade_date"],
                        update_cols=["name", "main_net_inflow", "main_net_inflow_pct",
                                     "super_net_inflow", "super_net_inflow_pct",
                                     "big_net_inflow", "big_net_inflow_pct",
                                     "mid_net_inflow", "mid_net_inflow_pct",
                                     "small_net_inflow", "small_net_inflow_pct",
                                     "source", "source_version", "observed_at",
                                     "source_quote_at", "received_at"],
                    )
                    await data_quality_guard.record_success(
                        session,
                        individual_provider,
                        "individual_fund_flow_round",
                        latency_ms=fetch_ms,
                        record_count=len(individual_records),
                        expected_count=max(individual_expected_count, len(individual_records)),
                    )
                    # A successful slice is NOT full-market coverage. Report a
                    # separate freshness-gated rolling-universe health row.
                    from app.data.main_fund import load_current_main_fund_map
                    fresh = await load_current_main_fund_map(
                        session, trade_date=today, decision_at=datetime.now(), codes=codes,
                    )
                    await data_quality_guard.record_success(
                        session, "tencent", "individual_fund_flow",
                        record_count=sum(row["source"] == "tencent" for row in fresh.values()),
                        expected_count=len(codes), latency_ms=fetch_ms,
                    )

                # ---- 2. 概念板块资金流 → 更新SectorInfo.stock_count ----
                if isinstance(df_concept, Exception):
                    await data_quality_guard.record_failure(
                        session,
                        "akshare",
                        "concept_fund_flow",
                        str(df_concept),
                        latency_ms=fetch_ms,
                    )
                elif isinstance(df_concept, pd.DataFrame) and len(df_concept) > 0:
                    valid_concept_count = 0
                    for _, row in df_concept.iterrows():
                        sector_name = str(row.get("行业", ""))
                        stock_count = _safe_int(row.get("公司家数"))
                        if sector_name and stock_count:
                            existing = await session.execute(
                                select(SectorInfo).where(
                                    SectorInfo.sector_name == sector_name,
                                    SectorInfo.source == "akshare",
                                )
                            )
                            for si in existing.scalars().all():
                                si.stock_count = stock_count
                            valid_concept_count += 1
                    await session.flush()
                    if valid_concept_count:
                        await data_quality_guard.record_success(
                            session,
                            "akshare",
                            "concept_fund_flow",
                            latency_ms=fetch_ms,
                            record_count=valid_concept_count,
                            expected_count=len(df_concept),
                        )
                    else:
                        await data_quality_guard.record_failure(
                            session,
                            "akshare",
                            "concept_fund_flow",
                            "接口有返回但没有有效板块名称/公司家数",
                            latency_ms=fetch_ms,
                        )
                else:
                    await data_quality_guard.record_failure(
                        session,
                        "akshare",
                        "concept_fund_flow",
                        "接口返回空数据或非法类型",
                        latency_ms=fetch_ms,
                    )

                await session.commit()
                total_ms = int((_time.monotonic() - t_total) * 1000)
                logger.debug(f"盘中慢频完成: 采集{fetch_ms}ms+写入{total_ms-fetch_ms}ms={total_ms}ms")

            except Exception as e:
                logger.error(f"盘中慢频写入失败: {e}")
                await session.rollback()

    async def _refresh_index_history(self):
        """独立盘外采集，真实失败保持unknown；不关闭任何账户，不回写旧订单。"""
        now = datetime.now()
        if self._index_history_refreshing:
            return {"status": "busy"}
        if time(9, 0) <= now.time() < time(15, 30):
            return {"status": "outside_history_window"}
        self._index_history_refreshing = True
        self._index_history_runtime = {"last_attempt_at": now.isoformat(), "status": "running"}
        try:
            from app.paper.experiment_regime import previous_known_trade_day
            from app.data.index_history_window import refresh_verified_history
            target_clock = (datetime.combine(now.date() + timedelta(days=1), time.min)
                            if now.time() >= time(15, 30) else now)
            async with async_session() as db:
                through = await previous_known_trade_day(db, at=target_clock)
            if through is None:
                result = {"status": "calendar_missing", "complete": False}
            else:
                result = await asyncio.wait_for(refresh_verified_history(
                    source=self._sources["index"], through_date=through,
                    session_factory=async_session), timeout=60)
            self._index_history_runtime.update(result, completed_at=datetime.now().isoformat())
            logger.info("盘外指数历史窗口验收: {}", result)
            return result
        except Exception as exc:
            result = {"status": "error", "complete": False, "error_type": type(exc).__name__}
            self._index_history_runtime.update(result, completed_at=datetime.now().isoformat())
            logger.warning("盘外指数历史窗口失败: {}", type(exc).__name__)
            return result
        finally:
            self._index_history_refreshing = False

    async def _intraday_indices_and_sentiment(self):
        """指数与市场情绪快照 — 每60秒更新一次真实数据"""
        if not await trade_calendar.is_trade_day():
            return

        session_type = trade_calendar.get_trade_session()
        if session_type not in ("pre_auction", "morning", "afternoon", "after_hours"):
            return

        today = date.today()
        self._sentiment_fund_health = {
            "status": "refreshing", "snapshot_persisted": False,
            "attempt_started_at": datetime.now().isoformat(),
        }
        index_src = self._sources["index"]

        async with async_session() as session:
            try:
                # ---- 1. 三大指数(优先实时快照, 失败回退日线) ----
                index_records = []
                spot_df = None
                try:
                    spot_df = await index_src.get_index_spot()
                except Exception as e:
                    logger.warning(f"实时指数快照获取失败, 回退日线: {e}")

                spot_map = {}
                if isinstance(spot_df, pd.DataFrame) and len(spot_df) > 0:
                    for _, row in spot_df.iterrows():
                        raw_code = str(row.get("代码", ""))
                        pure_code = raw_code[-6:] if len(raw_code) >= 6 else raw_code
                        spot_map[pure_code] = row

                for code in ("000001", "399001", "399006"):
                    if code in spot_map:
                        r = spot_map[code]
                        close = _safe_float(r.get("最新价"))
                        open_ = _safe_float(r.get("今开"))
                        high = _safe_float(r.get("最高"))
                        low = _safe_float(r.get("最低"))
                        volume = _safe_float(r.get("成交量"))
                        amount = _safe_float(r.get("成交额"))
                        prev_close = _safe_float(r.get("昨收"))
                        change_pct = _safe_float(r.get("涨跌幅"))
                        amplitude = round((high - low) / prev_close * 100, 2) if prev_close and high and low else 0
                        index_records.append({
                            "code": code,
                            "trade_date": today,
                            "open": open_,
                            "high": high,
                            "low": low,
                            "close": close,
                            "volume": int(volume or 0),
                            "amount": amount,
                            "turnover": 0,
                            "amplitude": amplitude,
                            "change_pct": change_pct,
                            "prev_close": prev_close,
                        })
                        continue

                    try:
                        df = await index_src.get_index_daily(code)
                    except Exception as e:
                        logger.warning(f"指数获取失败 {code}: {e}")
                        continue

                    if df is None or len(df) == 0:
                        continue

                    df2 = df.copy()
                    if "date" not in df2.columns:
                        continue
                    df2["date"] = pd.to_datetime(df2["date"]).dt.date
                    row = df2[df2["date"] <= today].tail(1)
                    if len(row) == 0:
                        continue
                    r = row.iloc[-1]
                    trade_date = r["date"]
                    if trade_date != today:
                        logger.warning("指数日线回退非当日，保留历史投影: {} {}", code, trade_date)
                        continue
                    close = _safe_float(r.get("close"))
                    open_ = _safe_float(r.get("open"))
                    high = _safe_float(r.get("high"))
                    low = _safe_float(r.get("low"))
                    volume = _safe_float(r.get("volume"))
                    amount = _safe_float(r.get("amount"))

                    prev_close = None
                    hist = df2[df2["date"] < trade_date].tail(1)
                    if len(hist) > 0:
                        prev_close = _safe_float(hist.iloc[-1].get("close"))
                    if not prev_close and open_:
                        prev_close = open_

                    change_pct = 0.0
                    if prev_close:
                        change_pct = round((close - prev_close) / prev_close * 100, 2)

                    index_records.append({
                        "code": code,
                        "trade_date": trade_date,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": int(volume or 0),
                        "amount": amount,
                        "turnover": 0,
                        "amplitude": round((high - low) / prev_close * 100, 2) if prev_close and high and low else 0,
                        "change_pct": change_pct,
                        "prev_close": prev_close,
                    })

                index_records = _current_index_snapshot_records(index_records, today)
                if index_records:
                    await self._batch_upsert(
                        session, StockDaily, index_records,
                        unique_cols=["code", "trade_date"],
                        update_cols=["open", "high", "low", "close", "volume", "amount",
                                     "turnover", "amplitude", "change_pct", "prev_close"],
                    )
                valid_index_count = len(index_records)
                if valid_index_count == 3:
                    await data_quality_guard.record_success(
                        session, "index", "daily_snapshot", latency_ms=0,
                        record_count=valid_index_count, expected_count=3,
                    )
                else:
                    # Zero current rows must also replace a stale healthy status.
                    await data_quality_guard.record_failure(
                        session, "index", "daily_snapshot",
                        f"当日有效指数仅{valid_index_count}/3，历史日线回退不视为当前完整快照",
                        completeness=valid_index_count / 3,
                    )

                # ---- 2. 市场情绪 ----
                lu_result = await session.execute(
                    select(LimitUpPool).where(LimitUpPool.trade_date == today)
                )
                ld_result = await session.execute(
                    select(LimitDownPool).where(LimitDownPool.trade_date == today)
                )
                bl_result = await session.execute(
                    select(BrokenLimitPool).where(BrokenLimitPool.trade_date == today)
                )
                tradeable_filters = (
                    StockTag.board_tag == "tradeable",
                    func.coalesce(StockTag.is_st, False).is_(False),
                    func.coalesce(StockTag.is_suspended, False).is_(False),
                    func.coalesce(StockTag.is_delisting, False).is_(False),
                )
                tradeable_universe_count = int(
                    await session.scalar(
                        select(func.count()).select_from(StockTag).where(*tradeable_filters)
                    )
                    or 0
                )
                ff_result = await session.execute(
                    select(FundFlow)
                    .join(StockTag, StockTag.code == FundFlow.code)
                    .where(FundFlow.trade_date == today, *tradeable_filters)
                )
                breadth_result = await session.execute(
                    select(
                        StockSpot.price,
                        StockSpot.volume,
                        StockSpot.change_pct,
                        StockSpot.updated_at,
                    )
                    .join(StockTag, StockTag.code == StockSpot.code)
                    .where(*tradeable_filters)
                )

                limit_ups = lu_result.scalars().all()
                limit_downs = ld_result.scalars().all()
                limit_up_codes = {row.code for row in limit_ups}
                # 防御性保持最终状态互斥：即便历史脏数据尚未被快频任务清掉，
                # 情绪封板率也不能把仍封板股票重复计入炸板分母。
                broken_limits = [
                    row for row in bl_result.scalars().all()
                    if row.code not in limit_up_codes
                ]
                # Coverage numerator and sum must use the same audited source,
                # finite pair and fresh clock contract as API/B1 consumption.
                from app.data.main_fund import current_main_fund_evidence, current_main_fund_status
                fund_decision_at = datetime.now()
                observed_funds = ff_result.scalars().all()
                fund_status_counts = Counter(current_main_fund_status(
                    row, trade_date=today, decision_at=fund_decision_at,
                ) for row in observed_funds)
                qualified_funds = [
                    evidence for row in observed_funds
                    if (evidence := current_main_fund_evidence(
                        row, trade_date=today, decision_at=fund_decision_at,
                    )) is not None
                ]
                main_flows = [row["main_net_inflow"] for row in qualified_funds]
                (
                    advance_decline_ratio,
                    advance_count,
                    decline_count,
                    flat_count,
                    breadth_sample_count,
                ) = _calculate_spot_advance_decline_ratio(
                    breadth_result.all(),
                    target_date=today,
                )

                lu_count = len(limit_ups)
                ld_count = len(limit_downs)
                bl_count = len(broken_limits)
                seal_rate = round(lu_count / (lu_count + bl_count) * 100, 1) if (lu_count + bl_count) > 0 else 0
                board_height = max([lu.consecutive_days or 1 for lu in limit_ups], default=0)
                # No qualified rows means unknown, not a measured market net zero.
                main_net_inflow = round(sum(main_flows) / 1e8, 2) if main_flows else None
                if breadth_sample_count >= 500:
                    logger.debug(
                        f"市场宽度: 上涨{advance_count} 下跌{decline_count} "
                        f"平盘{flat_count} 涨跌比{advance_decline_ratio:.2f}"
                    )
                else:
                    logger.warning(
                        f"市场涨跌家数样本不足({breadth_sample_count})，情绪质量降级而非回填中性"
                    )

                breadth_coverage = min(
                    breadth_sample_count / tradeable_universe_count,
                    1.0,
                ) if tradeable_universe_count else 0.0
                fund_flow_coverage = min(
                    len(main_flows) / tradeable_universe_count,
                    1.0,
                ) if tradeable_universe_count else 0.0
                current_index_records = _current_index_snapshot_records(index_records, today)
                index_changes = [
                    float(row["change_pct"]) for row in current_index_records
                ]
                index_avg_change_pct = (
                    round(sum(index_changes) / len(index_changes), 4)
                    if index_changes
                    else None
                )
                # 上证+深证成交额已覆盖沪深市场；创业板属于深市，不能重复相加。
                turnover_total = round(
                    sum(
                        float(row.get("amount") or 0)
                        for row in current_index_records
                        if row.get("code") in {"000001", "399001"}
                    ) / 1e12,
                    4,
                )
                # Finite individual observations can still overflow on aggregation.
                # Persist unknown (NULL), never infinity or an invented zero; the
                # quality function must then block this snapshot from buy evidence.
                if main_net_inflow is not None and not math.isfinite(main_net_inflow):
                    main_net_inflow = None
                if not math.isfinite(turnover_total):
                    turnover_total = None
                if index_avg_change_pct is not None and not math.isfinite(index_avg_change_pct):
                    index_avg_change_pct = None
                self._sentiment_fund_health = {
                    "status": "known_aggregate" if main_net_inflow is not None else "unavailable",
                    "decision_at": fund_decision_at.isoformat(),
                    "trade_date": today.isoformat(),
                    "scope": "qualified_tradeable_rows_only",
                    "main_net_inflow_billion": main_net_inflow,
                    "observed_row_count": len(observed_funds),
                    "qualified_count": len(qualified_funds),
                    "expected_count": tradeable_universe_count,
                    "missing_row_count": max(0, tradeable_universe_count - len(observed_funds)),
                    "fund_flow_coverage": fund_flow_coverage,
                    "status_counts": dict(fund_status_counts),
                    "freshness_threshold_changed": False,
                    "snapshot_persisted": False,
                }
                sentiment_state = _calculate_market_sentiment_state(
                    limit_up_count=lu_count,
                    limit_down_count=ld_count,
                    broken_limit_count=bl_count,
                    seal_rate=seal_rate,
                    board_height=board_height,
                    main_net_inflow=main_net_inflow,
                    advance_decline_ratio=advance_decline_ratio,
                    breadth_sample_count=breadth_sample_count,
                    breadth_coverage=breadth_coverage,
                    index_avg_change_pct=index_avg_change_pct,
                    index_sample_count=len(index_changes),
                    turnover_total=turnover_total,
                    fund_flow_coverage=fund_flow_coverage,
                )
                sentiment_cycle = str(sentiment_state["cycle"])

                sentiment_record = {
                    "trade_date": today,
                    "sentiment_cycle": sentiment_cycle,
                    "limit_up_count": lu_count,
                    "limit_down_count": ld_count,
                    "broken_limit_count": bl_count,
                    "seal_rate": seal_rate,
                    "board_height": board_height,
                    "advance_decline_ratio": advance_decline_ratio,
                    "turnover_total": turnover_total,
                    "main_net_inflow": main_net_inflow,
                    "sentiment_score": float(sentiment_state["score"]),
                    "quality_status": str(sentiment_state["quality_status"]),
                    "quality_reason": str(sentiment_state["quality_reason"]),
                    "breadth_sample_count": breadth_sample_count,
                    "breadth_coverage": round(breadth_coverage, 6),
                    "index_avg_change_pct": index_avg_change_pct,
                    "calculation_version": SENTIMENT_CALCULATION_VERSION,
                    "observed_at": datetime.now(),
                }
                await self._batch_upsert(
                    session, MarketSentiment, [sentiment_record],
                    unique_cols=["trade_date"],
                    update_cols=["sentiment_cycle", "limit_up_count", "limit_down_count",
                                 "broken_limit_count", "seal_rate", "board_height",
                                 "advance_decline_ratio", "turnover_total", "main_net_inflow",
                                 "sentiment_score", "quality_status", "quality_reason",
                                 "breadth_sample_count", "breadth_coverage",
                                 "index_avg_change_pct", "calculation_version", "observed_at"],
                )
                if sentiment_state["quality_status"] == "ok":
                    await data_quality_guard.record_success(
                        session,
                        "market_sentiment",
                        "snapshot",
                        latency_ms=0,
                        record_count=1,
                        expected_count=1,
                    )
                else:
                    await data_quality_guard.record_failure(
                        session,
                        "market_sentiment",
                        "snapshot",
                        str(sentiment_state["quality_reason"]) or "情绪关键字段不完整",
                        completeness=float(sentiment_state["quality_completeness"]),
                    )

                await session.commit()
                self._sentiment_fund_health["snapshot_persisted"] = True
                logger.debug(f"指数与情绪快照更新完成: 指数{len(index_records)}条, 情绪1条")
                return {"status": sentiment_state["quality_status"], "snapshot_persisted": True}

            except Exception as e:
                logger.error(f"指数与情绪快照失败: {e}")
                self._sentiment_fund_health.update(status="failed", error_type=type(e).__name__)
                await session.rollback()
                return {"status": "failed", "error_type": type(e).__name__}

    async def _dashboard2_snapshot(self):
        """overview-v2 快照刷新"""
        from app.dashboard2.jobs import refresh_dashboard2_snapshot
        await refresh_dashboard2_snapshot()

    async def _intraday_sina(self):
        """盘中新浪 — 概念板块实时行情(5分钟/次)

        更新SectorInfo的stock_count + SectorPersistence的fund_flow
        """
        if not await trade_calendar.is_trading_hours():
            return

        async with async_session() as session:
            sina = self._sources["sina"]
            try:
                t0 = _time.monotonic()
                df = await sina.get_sector_list()
                if df is not None and len(df) > 0:
                    for _, row in df.iterrows():
                        label = str(row.get("label", ""))
                        if not label.startswith("gn_"):
                            continue
                        sector_name = str(row.get("板块", ""))
                        stock_count = _safe_int(row.get("公司家数"))
                        if sector_name and stock_count:
                            existing = await session.execute(
                                select(SectorInfo).where(
                                    SectorInfo.sector_name == sector_name,
                                    SectorInfo.source == "sina",
                                )
                            )
                            for si in existing.scalars().all():
                                si.stock_count = stock_count

                    await session.commit()
                    await data_quality_guard.record_success(
                        session, "sina", "concept_spot",
                        latency_ms=int((_time.monotonic() - t0) * 1000),
                        record_count=len(df),
                    )
                    logger.debug(f"新浪概念行情写入: {len(df)}条")
            except Exception as e:
                logger.error(f"盘中新浪概念采集失败: {e}")

    async def _auction_collect(self, force: bool = False, auction_time: str | None = None):
        """竞价采集 — 9:15-9:25每30秒采集一次

        使用东财全A快照，失败时切换新浪全A快照，提取竞价信息
        """
        if self._auction_collect_refreshing:
            logger.debug("竞价采集跳过: 上一轮仍在运行")
            return

        session_type = trade_calendar.get_trade_session()
        if not force and session_type != "pre_auction":
            return

        if not await trade_calendar.is_trade_day():
            return

        self._auction_collect_refreshing = True
        from app.strategy.auction import auction_scheduler
        async with async_session() as session:
            try:
                result = await auction_scheduler.run_auction_phase(session, date.today(), auction_time=auction_time)
                if result.get("status") == "no_data":
                    logger.warning("竞价采集无数据: auction_data 未写入，竞价强攻路线本轮无输入")
                logger.debug(
                    f"竞价采集: {result.get('status')}, "
                    f"{result.get('saved', 0)}条, {result.get('total_signals', 0)}异动"
                )
            except Exception as e:
                logger.error(f"竞价采集失败: {e}")
            finally:
                self._auction_collect_refreshing = False

    async def _auction_collect_force(self):
        """竞价最终快照兜底，避免 09:25 会话边界漏采."""
        await self._auction_collect(force=True)

    async def _auction_collect_force_0920(self):
        """09:20:06 触发采样；落库时钟由采集器按真实响应时刻确定."""
        await self._auction_collect(force=True)

    async def _auction_collect_force_0924(self):
        """09:24 触发终场采样；不得预写未来的09:24:50标签."""
        await self._auction_collect(force=True)

    async def _auction_collect_force_0925(self):
        """09:25:06 触发终场采样；延迟响应不得倒填为09:25."""
        await self._auction_collect(force=True)

    async def _after_market(self):
        """盘后补全 — pywencai个股行业映射 + 涨停池补充"""
        if not await trade_calendar.is_trade_day():
            return
        logger.info("📊 盘后数据补全...")
        async with async_session() as session:
            pw = self._sources["pywencai"]
            today = date.today()

            # ---- 1. pywencai 个股→行业+概念映射 ----
            try:
                t0 = _time.monotonic()
                df = await pw.get_stock_industry_mapping()
                # pywencai.get("全部A股 所属同花顺行业 所属概念") 返回列:
                #   股票代码, 股票简称, 所属同花顺行业, 所属概念(多个逗号分隔)
                if df is not None and len(df) > 0:
                    mapping_records = []
                    for _, row in df.iterrows():
                        code = _normalize_code(str(row.get("股票代码", "")))
                        name = str(row.get("股票简称", ""))
                        industry = str(row.get("所属同花顺行业", ""))
                        concepts = str(row.get("所属概念", ""))

                        if not code:
                            continue

                        # 行业映射(pywencai三级格式"医药生物-中药-中药Ⅲ", 用三级全名与SectorInfo一致)
                        if industry and industry != "nan":
                            industry = industry.strip()
                            mapping_records.append({
                                "code": code,
                                "sector_code": f"pw_industry_{industry}",
                                "sector_name": industry,
                                "sector_type": "industry",
                                "source": "pywencai",
                                "source_version": "mapping_query_v2",
                                "observed_at": datetime.now(),
                                "weight": None,
                            })

                        # 概念映射(多值分号分隔, pywencai用分号";")
                        if concepts and concepts != "nan":
                            for concept in concepts.split(";"):
                                concept = concept.strip()
                                if concept:
                                    mapping_records.append({
                                        "code": code,
                                        "sector_code": f"pw_concept_{concept}",
                                        "sector_name": concept,
                                        "sector_type": "concept",
                                        "source": "pywencai",
                                        "source_version": "mapping_query_v2",
                                        "observed_at": datetime.now(),
                                        "weight": None,
                                    })

                    # 分批写入(每100条)
                    batch_size = 100
                    for i in range(0, len(mapping_records), batch_size):
                        await self._upsert_stock_sector_mapping(
                            session, mapping_records[i:i+batch_size]
                        )
                    mapped_codes = {item["code"] for item in mapping_records}
                    if mapped_codes:
                        await data_quality_guard.record_success(
                            session,
                            "pywencai",
                            "stock_mapping",
                            latency_ms=int((_time.monotonic() - t0) * 1000),
                            record_count=len(mapped_codes),
                            expected_count=len(df),
                        )
                    else:
                        await data_quality_guard.record_failure(
                            session,
                            "pywencai",
                            "stock_mapping",
                            "接口有返回但未解析出有效行业/概念映射",
                            latency_ms=int((_time.monotonic() - t0) * 1000),
                        )
                    logger.info(f"个股映射写入: {len(mapping_records)}条")

                    # 同时打股票标记
                    stocks_for_tag = []
                    for _, row in df.iterrows():
                        code = _normalize_code(str(row.get("股票代码", "")))
                        name = str(row.get("股票简称", ""))
                        if code:
                            is_st = "ST" in name or "*ST" in name
                            stocks_for_tag.append({
                                "code": code,
                                "name": name,
                                "is_st": is_st,
                            })
                    await stock_tagger.batch_tag(session, stocks_for_tag)
                    logger.info(f"股票标记写入: {len(stocks_for_tag)}只")
                else:
                    logger.warning("pywencai映射返回空DataFrame")
                    await data_quality_guard.record_failure(
                        session,
                        "pywencai",
                        "stock_mapping",
                        "接口返回空DataFrame",
                        latency_ms=int((_time.monotonic() - t0) * 1000),
                    )
            except Exception as e:
                logger.error(f"盘后映射补全失败: {e}")
                await data_quality_guard.record_failure(
                    session, "pywencai", "stock_mapping", str(e),
                )

            # ---- 2. pywencai 涨停池补充 ----
            # 东财盘中封板时间、连板数优先；问财盘后只补真实涨停原因。
            # 不能因东财已有记录就丢掉问财原因，否则“所属行业”会被误当涨停逻辑。
            try:
                t1 = _time.monotonic()
                df = await pw.get_limit_up_pool()
                if df is not None and len(df) > 0:
                    records = self._parse_limit_up_df(df, today, source="pywencai")
                    existing_em = await session.execute(
                        select(LimitUpPool).where(
                            LimitUpPool.trade_date == today,
                            LimitUpPool.source == "eastmoney",
                        )
                    )
                    em_by_code = {item.code: item for item in existing_em.scalars().all()}
                    reason_updates = 0
                    supplemental_records = []
                    for record in records:
                        existing = em_by_code.get(record["code"])
                        reason = str(record.get("limit_up_reason") or "").strip()
                        if existing:
                            if reason and reason.lower() != "nan":
                                existing.limit_up_reason = reason
                                reason_updates += 1
                            continue
                        supplemental_records.append(record)
                    if supplemental_records:
                        await self._upsert_limit_up(session, supplemental_records)
                    await data_quality_guard.record_success(
                        session, "pywencai", "limit_up",
                        latency_ms=int((_time.monotonic() - t1) * 1000),
                        record_count=len(supplemental_records) + reason_updates,
                    )
                    logger.info(
                        f"pywencai涨停补全: 新增{len(supplemental_records)}条，"
                        f"更新东财涨停原因{reason_updates}条"
                    )
            except Exception as e:
                logger.error(f"盘后pywencai涨停失败: {e}")

            await session.commit()

    async def _deep_review(self):
        """盘后深度 — 申万个股行业映射 → StockSectorMapping"""
        logger.info("📊 盘后深度数据采集...")
        
        async with async_session() as session:
            sw = self._sources["shenwan"]

            # ---- 1. 申万个股行业映射 ----
            try:
                t0 = _time.monotonic()
                df = await sw.get_stock_industry_clf()
                # ak.stock_industry_clf_hist_sw 返回列:
                #   symbol(股票代码), start_date, industry_code(申万行业代码), update_time
                # 注意: 同一只股票可能有多条记录(行业调整)，取最新的
                if df is not None and len(df) > 0:
                    # 按symbol分组，取最新一条
                    df_sorted = df.sort_values("update_time", ascending=False)
                    latest = df_sorted.drop_duplicates(subset=["symbol"], keep="first")

                    # 先加载所有申万行业信息，用于反查sector_name
                    sw_sectors = await session.execute(select(SectorInfo).where(
                        SectorInfo.source == "shenwan"
                    ))
                    sector_map = {s.sector_code: s.sector_name for s in sw_sectors.scalars().all()}

                    mapping_records = []
                    for _, row in latest.iterrows():
                        code = _normalize_code(str(row.get("symbol", "")))
                        industry_code = str(row.get("industry_code", ""))
                        if not code or not industry_code:
                            continue

                        # 从行业代码推断层级
                        if industry_code.endswith("01") and len(industry_code) == 6:
                            sector_type = "sw_l1"
                        elif len(industry_code) == 6:
                            sector_type = "sw_l2"
                        elif len(industry_code) == 8:
                            sector_type = "sw_l3"
                        else:
                            sector_type = "sw_l1"

                        sector_name = sector_map.get(industry_code, "")

                        mapping_records.append({
                            "code": code,
                            "sector_code": industry_code,
                            "sector_name": sector_name,
                            "sector_type": sector_type,
                            "source": "shenwan",
                            "source_version": "industry_clf_latest_v1",
                            "observed_at": datetime.now(),
                            "weight": None,
                        })

                    # 分批写入
                    batch_size = 100
                    for i in range(0, len(mapping_records), batch_size):
                        await self._upsert_stock_sector_mapping(
                            session, mapping_records[i:i+batch_size]
                        )
                    mapped_codes = {item["code"] for item in mapping_records}
                    if mapped_codes:
                        await data_quality_guard.record_success(
                            session,
                            "shenwan",
                            "stock_mapping",
                            latency_ms=int((_time.monotonic() - t0) * 1000),
                            record_count=len(mapped_codes),
                            expected_count=len(latest),
                        )
                    else:
                        await data_quality_guard.record_failure(
                            session,
                            "shenwan",
                            "stock_mapping",
                            "接口有返回但未解析出有效行业映射",
                            latency_ms=int((_time.monotonic() - t0) * 1000),
                        )
                    logger.info(f"申万映射写入: {len(mapping_records)}条")
                else:
                    await data_quality_guard.record_failure(
                        session,
                        "shenwan",
                        "stock_mapping",
                        "接口返回空DataFrame",
                        latency_ms=int((_time.monotonic() - t0) * 1000),
                    )
            except Exception as e:
                logger.error(f"深度申万映射失败: {e}")
                await data_quality_guard.record_failure(
                    session, "shenwan", "stock_mapping", str(e),
                )

            await session.commit()

    async def _quality_check(self):
        """数据质量检查"""
        async with async_session() as session:
            for name, source in self._sources.items():
                try:
                    endpoint_check = getattr(source, "health_check_endpoints", None)
                    if callable(endpoint_check):
                        endpoint_results = await endpoint_check(session)
                        for api_name, is_ok in endpoint_results.items():
                            if not is_ok:
                                logger.warning(f"⚠️ 数据端点 {name}/{api_name} 健康检查失败")
                        continue
                    is_ok = await source.health_check(session)
                    if not is_ok:
                        logger.warning(f"⚠️ 数据源 {name} 健康检查失败")
                except Exception as e:
                    logger.error(f"数据源 {name} 健康检查异常: {e}")

    # =========================================================================
    # DataFrame 解析器 (AkShare列名 → ORM字段映射)
    # =========================================================================

    @staticmethod
    def _parse_limit_up_df(df: pd.DataFrame, trade_date: date,
                            source: str = "eastmoney") -> list[dict]:
        """解析涨停池 DataFrame → LimitUpPool 记录列表

        东财 ak.stock_zt_pool_em 标准列(2026-04验证):
          序号, 代码, 名称, 涨跌幅, 最新价, 成交额, 流通市值, 总市值,
          换手率, 封板资金, 首次封板时间, 最后封板时间,
          炸板次数, 涨停统计, 连板数, 所属行业

          - 连板数: 当前连板天数(1=首板, 3=3连板)
          - 涨停统计: 格式"N/M"(N天内M个涨停)
          - 所属行业: 东财行业分类(用作涨停原因归类)

        pywencai 涨停池列(动态日期后缀):
          股票代码, 股票简称, 连续涨停天数[YYYYMMDD], 涨停原因类别[YYYYMMDD],
          涨停封单额[YYYYMMDD], 涨停开板次数[YYYYMMDD], 几天几板[YYYYMMDD], ...
        """
        # 预解析pywencai动态列名(带日期后缀)
        pywencai_col_map = {}
        if source == "pywencai":
            for col in df.columns:
                if "连续涨停天数" in col:
                    pywencai_col_map["consecutive_days"] = col
                elif "涨停原因类别" in col:
                    pywencai_col_map["limit_up_reason"] = col
                elif "涨停封单额" in col:
                    pywencai_col_map["seal_amount"] = col
                elif "涨停开板次数" in col:
                    pywencai_col_map["break_count"] = col
                elif "首次涨停时间" in col:
                    pywencai_col_map["limit_up_time"] = col
                elif "几天几板" in col:
                    pywencai_col_map["board_stats"] = col

        records = []
        for _, row in df.iterrows():
            # 兼容两种来源的列名
            code = _normalize_code(
                str(row.get("代码", row.get("股票代码", "")))
            )
            name = str(row.get("名称", row.get("股票简称", "")))
            limit_price = _safe_float(row.get("涨停价"))
            # 东财实时涨停池没有“涨停价”列；仍封板时“最新价”就是涨停价。
            # 过去只读不存在的列会把全池写成 0，进而使策略价格证据失真。
            if not limit_price or limit_price <= 0:
                limit_price = _safe_float(row.get("最新价"))
            seal_amt = _safe_float(
                row.get(pywencai_col_map.get("seal_amount", ""), 
                        row.get("封板资金"))
            )
            break_cnt = _safe_int(
                row.get(pywencai_col_map.get("break_count", ""), 
                        row.get("炸板次数", 0))
            )
            consec = _safe_int(
                row.get(pywencai_col_map.get("consecutive_days", ""), 
                        row.get("连板数", 1))
            )
            turnover = _safe_float(row.get("换手率"))

            # 涨停时间: 取首次封板时间
            limit_time = str(
                row.get(pywencai_col_map.get("limit_up_time", ""),
                        row.get("首次封板时间", row.get("涨停时间", "")))
            )
            if limit_time == "nan":
                limit_time = ""

            # 涨停原因: 
            # 东财: "所属行业"列用作原因归类
            # pywencai: "涨停原因类别[日期]"列
            if source == "pywencai" and "limit_up_reason" in pywencai_col_map:
                reason = str(row.get(pywencai_col_map["limit_up_reason"], ""))
            else:
                reason = str(row.get("所属行业", row.get("涨停原因", "")))
            if reason == "nan" or not reason:
                stat = str(row.get("涨停统计", ""))
                if stat and stat != "nan":
                    reason = stat
                else:
                    reason = ""

            if not code:
                continue

            records.append({
                "code": code,
                "name": name,
                "trade_date": trade_date,
                "limit_up_time": limit_time,
                "limit_up_price": limit_price,
                "seal_amount": seal_amt,
                "break_count": break_cnt,
                "consecutive_days": consec,
                "limit_up_reason": reason if reason else None,
                "turnover": turnover,
                "source": source,
                # _batch_upsert 会显式绑定 LimitUpPool 的全部列；新增非空列后
                # 必须在记录中给值，不能依赖 server_default，否则整批实时涨停池回滚。
                "quarantined": False,
            })
        return records

    @staticmethod
    def _parse_limit_down_df(df: pd.DataFrame, trade_date: date,
                              source: str = "eastmoney") -> list[dict]:
        """解析跌停池 DataFrame → LimitDownPool 记录列表

        东财 ak.stock_zt_pool_dtgc_em 标准列:
          代码, 名称, 跌停价, 最新价, 涨跌幅, 成交额, ...
        """
        records = []
        for _, row in df.iterrows():
            code = _normalize_code(
                str(row.get("代码", row.get("股票代码", "")))
            )
            name = str(row.get("名称", row.get("股票简称", "")))
            limit_time = str(row.get("跌停时间", ""))
            if limit_time == "nan":
                limit_time = ""
            break_cnt = _safe_int(row.get("炸板次数", 0))
            consec = _safe_int(row.get("连续跌停", 1))
            reason = str(row.get("跌停原因", ""))

            if not code:
                continue

            records.append({
                "code": code,
                "name": name,
                "trade_date": trade_date,
                "limit_down_time": limit_time if limit_time else None,
                "break_count": break_cnt,
                "consecutive_days": consec,
                "reason": reason if reason != "nan" else None,
                "source": source,
            })
        return records

    @staticmethod
    def _parse_broken_limit_df(df: pd.DataFrame, trade_date: date,
                                source: str = "eastmoney") -> list[dict]:
        """解析炸板池 DataFrame → BrokenLimitPool 记录列表

        东财 ak.stock_zt_pool_zbgc_em 标准列:
          代码, 名称, 涨停价, 最新价, 涨跌幅, 成交额,
          首次封板时间, 最后封板时间, 炸板次数, 封板资金, ...
        """
        records = []
        for _, row in df.iterrows():
            code = _normalize_code(
                str(row.get("代码", row.get("股票代码", "")))
            )
            name = str(row.get("名称", row.get("股票简称", "")))
            limit_time = str(row.get("首次封板时间", ""))
            break_time = str(row.get("最后封板时间", ""))
            if limit_time == "nan":
                limit_time = ""
            if break_time == "nan":
                break_time = ""
            seal_amt = _safe_float(row.get("封板资金"))

            if not code:
                continue

            records.append({
                "code": code,
                "name": name,
                "trade_date": trade_date,
                "limit_up_time": limit_time if limit_time else None,
                "break_time": break_time if break_time else None,
                "seal_duration": None,
                "seal_amount": seal_amt,
                "source": source,
                "limit_up_price": _safe_float(row.get("涨停价")),
                "close_price": _safe_float(row.get("最新价")),
                "close_at_limit": (
                    bool(
                        _safe_float(row.get("涨停价"))
                        and _safe_float(row.get("最新价"))
                        and round(_safe_float(row.get("最新价")), 2)
                        >= round(_safe_float(row.get("涨停价")), 2)
                    )
                ),
                "final_state": (
                    "reclosed"
                    if _safe_float(row.get("涨停价"))
                    and _safe_float(row.get("最新价"))
                    and round(_safe_float(row.get("最新价")), 2)
                    >= round(_safe_float(row.get("涨停价")), 2)
                    else "broken"
                    if _safe_float(row.get("最新价"))
                    else "unknown"
                ),
            })
        return records

    @staticmethod
    def _parse_individual_fund_flow_df(
        df: pd.DataFrame,
        trade_date: date,
        *,
        source: str = "eastmoney",
        source_version: str = "individual_fund_flow_v3_f124",
        observed_at: datetime | None = None,
    ) -> list[dict]:
        """解析个股资金流 DataFrame → FundFlow 记录列表

        兼容两类 AkShare 返回:

        1. 旧列名:
          代码, 名称, 最新价, 涨跌幅, 换手率,
          主力净流入-净额, 主力净流入-净占比,
          超大单净流入-净额, 超大单净流入-净占比,
          大单净流入-净额, 大单净流入-净占比,
          中单净流入-净额, 中单净流入-净占比,
          小单净流入-净额, 小单净流入-净占比

        2. 同花顺总流入/流出列名:
          股票代码, 股票简称, 最新价, 涨跌幅, 换手率,
          流入资金, 流出资金, 净额, 成交额

        第二类的“净额”是总流入减总流出，不是主力净额；FundFlow 模型的
        指标均为主力/订单大小口径，因此这种帧必须拒绝，不能用 0 补缺失细分。
        """
        # Share the exact source gate with current-fund readers and sentiment.
        from app.data.main_fund import main_fund_source_supported
        if not main_fund_source_supported(source, source_version):
            logger.warning(f"拒绝未知主力资金合同: source={source}, version={source_version}")
            return []

        main_amount_columns = {"主力净流入-净额", "今日主力净流入-净额"}
        main_pct_columns = {"主力净流入-净占比", "今日主力净流入-净占比"}
        if not (main_amount_columns & set(df.columns)):
            logger.warning(
                "个股资金帧不含可验证主力净额，拒绝写入主力指标: "
                f"source={source}, version={source_version}, columns={list(df.columns)}"
            )
            return []
        if not (main_pct_columns & set(df.columns)):
            logger.warning(
                "个股资金帧不含可验证主力净占比，拒绝写入主力指标: "
                f"source={source}, version={source_version}"
            )
            return []

        frame_observed_at = local_clock(observed_at if observed_at is not None else datetime.now())
        if (frame_observed_at is None or frame_observed_at.date() != trade_date
                or frame_observed_at > datetime.now()):
            logger.warning("资金帧观测时钟无效/跨日/超前，拒绝重贴当前时刻")
            return []
        if not {"source_quote_at", "received_at"}.issubset(df.columns):
            logger.warning(f"资金帧缺少个股源时钟/接收时钟，拒绝以接收时间替代: source={source}")
            return []

        def fund_number(value):
            # Keep this guard local: shared _safe_float serves unrelated datasets.
            # Python/numpy booleans are not measured money; preserve real zero and
            # the existing explicit 万/亿/% string conversion below.
            try:
                return None if pd.api.types.is_bool(value) else _safe_float(value)
            except OverflowError:
                # Reject this cell only; one huge supplier integer must not abort
                # other stocks or turn a missing optional component into zero.
                return None

        records = []
        for values in df.itertuples(index=False, name=None):
            # iterrows can overflow while inferring a Series dtype, before the
            # per-cell guards run. Preserve supplier values without inference.
            row = pd.Series(values, index=df.columns, dtype=object)
            try:
                code = _normalize_code(str(row.get("代码", row.get("股票代码", ""))))
            except (ValueError, TypeError, OverflowError):
                # Even an oversized integer identifier must not abort other rows.
                continue
            # Shared normalization is permissive for unrelated datasets. Funds
            # require an actual six-digit ASCII identity, not NaN, a rounded
            # decimal, Unicode digits or an overlength code counted as coverage.
            if len(code) != 6 or not code.isascii() or not code.isdigit():
                continue
            name = str(row.get("名称", row.get("股票简称", "")))

            clocks = verified_fund_clocks(
                row.get("source_quote_at"), row.get("received_at"),
                frame_observed_at, trade_date, settings.FUND_FLOW_SOURCE_MAX_AGE_SEC,
            )
            if clocks is None:
                continue
            source_at, received_at, _ = clocks
            main_inflow = fund_number(
                row.get("主力净流入-净额", row.get("今日主力净流入-净额"))
            )
            main_pct = fund_number(
                row.get("主力净流入-净占比", row.get("今日主力净流入-净占比"))
            )
            # 覆盖率按同一行真实可用的主力净额+占比计数；仅有列名、整列
            # None/NaN 或单边缺值都不能进入分子。0 是合法观测值，不应丢弃。
            if (main_inflow is None or main_pct is None
                or not math.isfinite(main_inflow) or not math.isfinite(main_pct)):
                continue
            # Persist the same response row, not estimates or a second cache.
            # Optional missing/invalid breakdowns stay NULL; measured zero/sign
            # and the provider percentage-point unit are preserved unchanged.
            breakdown = {}
            for prefix, label in (("super", "超大单"), ("big", "大单"),
                                  ("mid", "中单"), ("small", "小单")):
                for suffix, unit in (("", "净额"), ("_pct", "净占比")):
                    column = f"{label}净流入-{unit}"
                    value = fund_number(row.get(column, row.get(f"今日{column}")))
                    breakdown[f"{prefix}_net_inflow{suffix}"] = (
                        value if value is not None and math.isfinite(value) else None
                    )

            records.append({
                "code": code,
                "name": name,
                "trade_date": trade_date,
                "main_net_inflow": main_inflow,
                "main_net_inflow_pct": main_pct,
                **breakdown,
                "source": source,
                "source_version": source_version,
                "source_quote_at": source_at,
                "received_at": received_at,
                "observed_at": frame_observed_at,
            })
        # 排名型分页会在采集期间移动页边界。同一代码只保留后取到的
        # 最新行，避免一批UPSERT包含重复唯一键并虚增覆盖率。
        deduplicated = {record["code"]: record for record in records}
        if len(deduplicated) != len(records):
            logger.warning(
                f"个股资金帧去重: source={source}, rows={len(records)}, "
                f"unique={len(deduplicated)}"
            )
        return list(deduplicated.values())

    # =========================================================================
    # 辅助方法
    # =========================================================================

    @staticmethod
    async def _find_sector_code_by_name(session: AsyncSession,
                                         sector_name: str,
                                         sector_type: str) -> Optional[str]:
        """根据板块名称和类型反查 sector_code"""
        if not sector_name or sector_name == "nan":
            return None
        result = await session.execute(
            select(SectorInfo).where(
                SectorInfo.sector_name == sector_name,
                SectorInfo.sector_type == sector_type,
            ).limit(1)
        )
        row = result.scalar_one_or_none()
        return row.sector_code if row else None

    def start(self):
        """启动调度器"""
        if self.scheduler.running:
            logger.info("🦅 采集调度器已在运行，跳过重复启动")
            return
        
        try:
            self.setup_jobs()
            if not self._runtime_listener_registered:
                self.scheduler.add_listener(self._on_scheduler_job_event, JOB_EVENT_MASK)
                self._runtime_listener_registered = True
            self.scheduler.start()
            
            # 验证启动成功
            if not self.scheduler.running:
                logger.error("❌ 调度器启动失败: scheduler.running=False after start()")
                return
            
            self._process_awake_guard.start()
            job_count = len(self.scheduler.get_jobs())
            if job_count == 0:
                logger.warning("⚠️ 调度器已启动但无任务，可能setup_jobs()失败")
            else:
                logger.info(f"✅ 调度器已启动，任务数: {job_count}")
            
            try:
                loop = asyncio.get_running_loop()
                if self._pipeline_health_task is None or self._pipeline_health_task.done():
                    self._pipeline_health_task = loop.create_task(self._pipeline_health_loop())
                loop.create_task(self._startup_prewarm())
                self._index_history_task = loop.create_task(self._refresh_index_history())
                self._start_promotion_snapshot_catchup(loop)
                self._start_paper_auto_loop(loop)
                self._start_anomaly_push_loop(loop)
                self._start_paper_buy_point_push_loop(loop)
                self._start_quote_round_loop(loop)
                loop.create_task(self._restore_intraday_quote_state())
            except RuntimeError:
                logger.debug("启动预热跳过: 当前无运行中的事件循环")
            
            logger.info("🦅 采集调度器已启动")
            
        except Exception as e:
            logger.error(f"❌ 调度器启动异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
            raise

    def stop(self):
        """停止调度器"""
        self._process_awake_guard.stop()
        if self._runtime_listener_registered:
            self.scheduler.remove_listener(self._on_scheduler_job_event)
            self._runtime_listener_registered = False
        for task in (
            self._pipeline_health_task,
            self._promotion_news_refresh_task, self._promotion_prediction_task,
            self._promotion_startup_catchup_task,
            self._concept_fund_flow_task, self._index_history_task,
        ):
            if task is not None and not task.done():
                task.cancel()
        self._promotion_startup_catchup_task = None
        self._promotion_prediction_pending = None
        if self._paper_buy_point_push_task and not self._paper_buy_point_push_task.done():
            self._paper_buy_point_push_task.cancel()
        self._paper_buy_point_push_task = None
        from app.news.engine import news_engine
        news_engine.cancel_pending_fetches()
        if not self.scheduler.running:
            logger.info("采集调度器未运行，跳过停止")
            return
        if self._paper_auto_loop_task and not self._paper_auto_loop_task.done():
            self._paper_auto_loop_task.cancel()
            self._paper_auto_loop_task = None
        if self._anomaly_push_loop_task and not self._anomaly_push_loop_task.done():
            self._anomaly_push_loop_task.cancel()
            self._anomaly_push_loop_task = None
        if self._quote_round_loop_task and not self._quote_round_loop_task.done():
            self._quote_round_loop_task.cancel()
            self._quote_round_loop_task = None
        for task in tuple(self._quote_archive_tasks):
            if not task.done():
                task.cancel()
        self._quote_archive_tasks.clear()
        self.scheduler.shutdown()
        logger.info("采集调度器已停止")

    def _start_paper_auto_loop(self, loop: asyncio.AbstractEventLoop):
        """给模拟盘自动执行增加独立心跳，避免 APScheduler 拥挤时漏跑。"""
        if self._paper_auto_loop_task and not self._paper_auto_loop_task.done():
            return
        self._paper_auto_loop_task = loop.create_task(self._paper_intraday_auto_loop())

    def _start_promotion_snapshot_catchup(self, loop: asyncio.AbstractEventLoop):
        if self._promotion_startup_catchup_task and not self._promotion_startup_catchup_task.done():
            return
        self._promotion_startup_catchup_task = loop.create_task(self._startup_promotion_snapshot_catchup())

    async def _read_promotion_recovery_probe(self, context: str, checked_at: datetime) -> dict:
        """Read only; persisted completion suppresses duplicates, never authorizes trades."""
        is_trade_day = await trade_calendar.is_trade_day(checked_at.date())
        if is_trade_day is not True:
            return {"status": "not_trade_day" if is_trade_day is False else "calendar_unknown"}
        if context not in {"promotion_1510", "promotion_2000"}:
            return {"status": "existing_intraday_recovery"}
        from app.api.v1.promotion import (
            PROMOTION_MODEL_IDENTITY, PROMOTION_CANONICAL_CLOSE_CONTEXTS,
            _PROMOTION_OFFICIAL_CONTEXT_WINDOWS,
        )
        from app.core.prediction_data_quality import PREDICTION_ROUTE_REQUIRED_DATASETS
        from app.promotion.batch_diagnostics import load_batch_diagnostics

        identity = PROMOTION_MODEL_IDENTITY
        async with async_session() as db:
            diagnostic = await load_batch_diagnostics(
                db, trade_date=checked_at.date(), checked_at=checked_at,
                expected_identity={
                    "model_version": identity.active_model_version,
                    "feature_version": identity.feature_version,
                    "data_version": identity.data_version,
                    "runtime_mode": identity.runtime_mode.value,
                },
                context_windows=_PROMOTION_OFFICIAL_CONTEXT_WINDOWS,
                required_routes=tuple(PREDICTION_ROUTE_REQUIRED_DATASETS),
                canonical_close_contexts=PROMOTION_CANONICAL_CLOSE_CONTEXTS,
            )
        if not diagnostic["evidence_available"]:
            return {"status": "probe_unavailable"}
        window = next(row for row in diagnostic["contexts"] if row["snapshot_context"] == context)
        latest = window["latest_attempt"]
        state = window["status"]
        if state in {"calendar_unknown", "not_expected_closed_session", "invalid_persisted_attempt"}:
            return {"status": "probe_attention", "diagnostic_status": state}
        # A published batch and all-routes-pass are different facts. Preserve
        # partial/blocked route evidence instead of rebuilding a favorable batch.
        if latest and latest["status"] == "completed" and state in {
            "completed", "completed_route_blocked", "completed_batch_gate_not_passed",
            "completed_route_gate_unknown",
        }:
            return {"status": "persisted_completed", "run_id": latest["id"],
                    "diagnostic_status": state}
        return {"status": "no_completed_batch", "diagnostic_status": state}

    async def _promotion_recovery_tick(self) -> dict:
        checked_at = datetime.now()
        trigger = _promotion_startup_catchup_trigger(checked_at)
        context = _promotion_prediction_snapshot_context(trigger, checked_at) if trigger else None
        state = {
            "protocol_version": "formal_window_recovery_v1",
            "checked_at": checked_at.isoformat(), "context": context,
            "scope": "current_process_window_recovery_not_day_completeness",
            "historical_backfill": False, "execution_authorization": False,
        }

        def finish(status, **detail):
            result = {**state, "status": status, **detail}
            self._promotion_recovery_health = result
            return result

        if not trigger:
            return finish("outside_recovery_window")
        key = (checked_at.date(), context)
        self._promotion_snapshot_completed_contexts = {
            item for item in self._promotion_snapshot_completed_contexts if item[0] == checked_at.date()
        }
        if key in self._promotion_snapshot_completed_contexts:
            return finish("already_completed")
        # Existing single-owner/coalescing logic owns a running build.
        if not self._promotion_prediction_refreshing:
            probe_epoch = self._promotion_prediction_epoch
            try:
                probe = await asyncio.wait_for(
                    self._read_promotion_recovery_probe(context, checked_at),
                    timeout=float(settings.PROMOTION_RECOVERY_PROBE_TIMEOUT_SEC),
                )
            except asyncio.TimeoutError:
                return finish("probe_timeout")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return finish("probe_failed", error_type=type(exc).__name__)
            # Expiration/date may have changed during the SELECT/calendar await.
            now = datetime.now()
            if now.date() != checked_at.date() or _promotion_startup_catchup_trigger(now) != trigger:
                return finish("expired_during_probe")
            if self._promotion_prediction_epoch != probe_epoch:
                # Cron may have started AND failed while SELECT was pending.
                # Never let an earlier read resurrect old completion over it.
                return finish("probe_obsoleted_by_live_attempt")
            if probe["status"] == "persisted_completed":
                self._promotion_snapshot_completed_contexts.add(key)
                return finish("persisted_completed", run_id=probe["run_id"],
                              diagnostic_status=probe["diagnostic_status"])
            if probe["status"] not in {"no_completed_batch", "existing_intraday_recovery"}:
                return finish(probe["status"], diagnostic_status=probe.get("diagnostic_status"))
        self._promotion_recovery_health = {**state, "status": "attempt_requested"}
        # No custom recorded_at/caller DB: the original builder rechecks date,
        # window, commits the generation barrier and applies all original gates.
        payload = await self._prewarm_promotion_candidates(trigger=trigger)
        payload = payload or {}
        recorded = (payload.get("learning") or {}).get("recorded_predictions")
        attempt_status = payload.get("status") or (payload.get("runtime_timing") or {}).get("status")
        if not attempt_status and (_safe_int(recorded) or 0) > 0:
            attempt_status = "completed"
        return finish("attempt_returned", attempt_status=attempt_status or "returned_without_status",
                      recorded_predictions=recorded)

    async def _startup_promotion_snapshot_catchup(self):
        """正式窗口守护；仅在仍合法的实际时点恢复，不追补过期历史。"""
        await asyncio.sleep(1)
        while self.scheduler.running:
            try:
                await self._promotion_recovery_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("正式窗口晋级预测守护失败: {}", type(exc).__name__)
            await asyncio.sleep(15)

    async def _paper_intraday_auto_loop(self):
        """模拟盘盘中心跳兜底；行情轮次事件正常时由 watchdog 自动跳过。"""
        await asyncio.sleep(5)
        while self.scheduler.running:
            try:
                await self._paper_intraday_auto_trade()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"模拟盘盘中心跳执行失败: {exc}")
            await asyncio.sleep(max(10, settings.PAPER_INTRADAY_AUTO_INTERVAL_SEC))

    def _start_quote_round_loop(self, loop: asyncio.AbstractEventLoop):
        if self._quote_round_loop_task and not self._quote_round_loop_task.done():
            return
        self._quote_round_loop_task = loop.create_task(self._quote_round_loop())

    def _publish_quote_round(self, payload: dict) -> None:
        """无await发布：交易只取最新轮次，A2证据按原采集顺序保留。"""
        self._enqueue_momentum_quote_round(payload)
        self._quote_round_payload = payload
        self._quote_round_event.set()

    def _enqueue_momentum_quote_round(self, payload: dict) -> None:
        if not settings.PAPER_MOMENTUM_RETEST_SHADOW_ENABLED:
            return
        observed_at = local_clock(payload.get("committed_at"))
        if observed_at is None:
            return
        if self._momentum_last_published_at is not None and observed_at <= self._momentum_last_published_at:
            return  # 重复/倒序发布不得回退状态机时钟。
        self._momentum_last_published_at = observed_at
        self._momentum_quote_inbox.append({
            "payload": payload, "observed_at": observed_at, "coverage_loss": None,
        })
        maximum = max(1, int(settings.PAPER_MOMENTUM_RETEST_QUOTE_INBOX_MAX_BATCHES))
        while len(self._momentum_quote_inbox) > maximum:
            dropped = self._momentum_quote_inbox.popleft()
            successor = self._momentum_quote_inbox[0]
            if dropped["observed_at"].date() == successor["observed_at"].date():
                prior_loss = dropped["coverage_loss"] or {}
                next_loss = successor["coverage_loss"] or {}
                successor["coverage_loss"] = {
                    "reason": "consumer_inbox_overflow",
                    "first_dropped_round_id": prior_loss.get("first_dropped_round_id") or dropped["payload"].get("round_id"),
                    "last_dropped_round_id": dropped["payload"].get("round_id"),
                    "dropped_rounds": int(prior_loss.get("dropped_rounds", 0)) + int(next_loss.get("dropped_rounds", 0)) + 1,
                }
            logger.warning("A2前向证据inbox溢出，保留显式缺口: round={}", dropped["payload"].get("round_id"))

    async def _drain_momentum_quote_rounds(self, payload: dict) -> None:
        """只推进已发布的A2纯行情证据；不回放板块/资金查询或任何订单。

        一次最多处理调用时已排队的帧，不消费交易payload之后的新轮次。
        待提交失败帧另保留1个；成功提交前不让后续帧越过它。
        """
        from app.paper.momentum_retest_shadow import scan_momentum_retest_shadow

        upper_at = local_clock(payload.get("committed_at"))
        if upper_at is None:
            return
        self._enqueue_momentum_quote_round(payload)  # 兼容直接调用；已发布轮次会去重。
        remaining = len(self._momentum_quote_inbox) + int(self._momentum_quote_inflight is not None)
        for _ in range(remaining):
            if self._momentum_quote_inflight is None:
                if not self._momentum_quote_inbox or self._momentum_quote_inbox[0]["observed_at"] > upper_at:
                    break
                self._momentum_quote_inflight = self._momentum_quote_inbox.popleft()
            entry = self._momentum_quote_inflight
            observed_at = entry["observed_at"]
            if observed_at > upper_at:
                break
            if observed_at.date() < upper_at.date():
                # 不用今天的资格缓存重建昨天路径；日切由状态机独立重置。
                self._momentum_quote_inflight = None
                continue
            frame = entry["payload"]
            loss = entry["coverage_loss"]
            if frame.get("quality_status") != "ok":
                loss = loss or {
                    "reason": "degraded_quote_round", "round_id": frame.get("round_id"),
                    "quality_reason": str(frame.get("quality_reason") or "")[:256],
                }
            try:
                async with async_session() as session:
                    result = await scan_momentum_retest_shadow(
                        session, frame.get("records") or [], observed_at, coverage_loss=loss,
                    )
                self._momentum_quote_inflight = None
                if result.get("events"):
                    logger.info("强势股首次回踩影子扫描: events={} confirmed={} round={}",
                                result.get("events"), result.get("confirmed"), frame.get("round_id"))
            except Exception:
                logger.exception("A2证据提交失败，保留当前帧下轮重试；主交易链路不受影响")
                break

    async def _restore_intraday_quote_state(self) -> None:
        """服务同日重启后恢复最近8分钟轨迹；只预热状态机，不回放交易。"""
        try:
            from app.data.quote_round import quote_round_archive
            from app.signal.anomaly_scanner import anomaly_scanner

            batches = await asyncio.to_thread(
                quote_round_archive.restore_recent_batches,
                date.today(),
                minutes=settings.QUOTE_ROUND_RESTORE_MINUTES,
            )
            anomaly_scanner.restore_quote_batches(batches)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("恢复分时行情状态失败；新轮次将重新形成连续窗口")

    async def _run_quote_round_position_risk(self, payload: dict) -> None:
        """Serialize account writers; retry SQLite busy only in a fresh session.

        A failed account must not make the whole round look healthy or dispatch
        new entries. Already committed fills retain their original identity and
        the normal reconciliation/orphan-fill guards run again on every attempt.
        """
        from app.api.v1 import paper

        account_names = (*paper.PAPER_ALL_ACCOUNTS, *paper.PAPER_CHALLENGER_ACCOUNTS)
        health = {
            "status": "running", "quote_round_id": str(payload.get("round_id") or ""),
            "started_at": datetime.now().isoformat(), "accounts": [],
        }
        self._paper_position_risk_health = health
        failures = []
        for account_name in account_names:
            token = paper._QUOTE_ROUND_CONTEXT.set(payload)
            try:
                # One bounded retry, never an unbounded loop or a new signal.
                for attempt in (1, 2):
                    try:
                        committed = local_clock(payload.get("committed_at"))
                        now = local_clock(datetime.now())
                        if (committed is None or now is None
                                or not 0 <= (now - committed).total_seconds()
                                <= settings.PAPER_EXECUTION_QUOTE_MAX_AGE_SEC):
                            raise RuntimeError("持仓风控轮次已过期或缺少时钟，不以旧轮次补造成交")
                        async with async_session() as session:
                            try:
                                await paper.run_paper_position_risk_monitor(
                                    session, account_name=account_name, trigger="quote-round-risk",
                                )
                            except BaseException:
                                await session.rollback()
                                raise
                        health["accounts"].append({
                            "account_name": account_name, "status": "ok", "attempts": attempt,
                        })
                        break
                    except asyncio.CancelledError:
                        health["status"] = "canceled"
                        raise
                    except Exception as exc:
                        original = exc.orig if isinstance(exc, OperationalError) else None
                        code = getattr(original, "sqlite_errorcode", None)
                        sqlite_busy = isinstance(original, sqlite3.OperationalError) and (
                            (isinstance(code, int) and (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))
                            or str(original) in ("database is locked", "database table is locked")
                        )
                        if sqlite_busy and attempt == 1:
                            logger.warning(
                                "持仓风控SQLite争用，已回滚并将更换会话重试一次: account={}",
                                account_name,
                            )
                            continue
                        failures.append(account_name)
                        health["accounts"].append({
                            "account_name": account_name, "status": "failed", "attempts": attempt,
                            "error_type": type(exc).__name__, "sqlite_busy": sqlite_busy,
                        })
                        logger.exception("行情轮次持仓风控失败: account={}", account_name)
                        break
            finally:
                paper._QUOTE_ROUND_CONTEXT.reset(token)
        health["completed_at"] = datetime.now().isoformat()
        health["status"] = "blocked" if failures else "ok"
        if failures:
            # Both the event consumer and watchdog only mark success after this.
            raise RuntimeError("持仓风控未完成，本轮跳过新增候选开仓: " + ",".join(failures))

    async def _process_quote_round_shadow(self, payload: dict, *, execute_challengers: bool = True) -> None:
        """影子证据与隔离账户均消费同一批 owned records，不读取未来轮次。"""
        records = payload.get("records") or []
        observed_at = payload["committed_at"]
        await self._drain_momentum_quote_rounds(payload)

        try:
            from app.paper.strategy_iteration_shadow import scan_strategy_iteration_shadow

            async with async_session() as session:
                result = await scan_strategy_iteration_shadow(session, records, observed_at)
            if result.get("material_events"):
                logger.info(
                    f"ABC3DF形态挑战者影子扫描: events={result.get('events')} "
                    f"confirmed={result.get('confirmed')} round={payload.get('round_id')}"
                )
        except Exception:
            logger.exception("ABC3DF形态挑战者影子扫描失败；主交易链路不受影响")

        if not execute_challengers:
            return  # 风控故障仍采集影子证据，但不调用任何隔离账户交易入口。
        try:
            from app.api.v1 import paper
            from app.paper.strategy_iteration_challenger import run_strategy_iteration_challenger_accounts

            token = paper._QUOTE_ROUND_CONTEXT.set(payload)
            try:
                async with async_session() as session:
                    result = await run_strategy_iteration_challenger_accounts(
                        session,
                        now=observed_at,
                    )
            finally:
                paper._QUOTE_ROUND_CONTEXT.reset(token)
            if result.get("entries") or result.get("sells") or result.get("blocked"):
                logger.info(f"隔离Challenger模拟账户执行: {result}")
        except Exception:
            logger.exception("隔离Challenger模拟账户执行失败；Champion账户不受影响")

    async def _quote_round_loop(self) -> None:
        """健康轮次先持仓风控再入场；A2顺序采证，重型影子仅处理当前轮次。

        读取payload与clear之间无await；交易去重不等于A2证据已经处理。
        """
        while self.scheduler.running:
            try:
                await self._quote_round_event.wait()
                self._quote_round_event.clear()
                payload = self._quote_round_payload
                if not payload:
                    continue
                if payload.get("quality_status") != "ok":
                    logger.warning(
                        "行情轮次降级，交易失败关闭: "
                        f"round={payload.get('round_id')} reason={payload.get('quality_reason')}"
                    )
                    async with self._quote_dispatch_lock:
                        await self._expire_pending_paper_buys(datetime.now())
                        await self._drain_momentum_quote_rounds(payload)
                    continue
                async with self._quote_dispatch_lock:
                    current_round_id = str(payload.get("round_id") or "")
                    if (
                        current_round_id
                        and current_round_id == self._last_quote_round_processed_id
                    ):
                        logger.debug(
                            f"行情轮次已由watchdog处理，跳过重复交易: round={current_round_id}"
                        )
                        await self._drain_momentum_quote_rounds(payload)
                        continue
                    try:
                        await self._run_quote_round_position_risk(payload)
                    except Exception:
                        # 不新增风险，也不把退出故障扩散成A2/C2的行情采证断档。
                        await self._process_quote_round_shadow(payload, execute_challengers=False)
                        raise
                    await self._run_paper_accounts_isolated(
                        execute=True,
                        trigger="quote-round-entry",
                        execution_mode="intraday",
                        quote_payload=payload,
                        include_position_risk=False,
                    )
                    await self._process_quote_round_shadow(payload)
                    self._last_quote_round_processed_at = datetime.now()
                    self._last_quote_round_processed_id = current_round_id or None
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("行情轮次消费者失败；60秒watchdog将兜底")

    async def _archive_quote_round(self, payload: dict) -> None:
        from app.data.quote_round import quote_round_archive
        from app.models.paper import PaperAutoTradeLog, PaperPosition
        from app.models.trading import TradeOrder

        round_id = str(payload["round_id"])
        try:
            async with async_session() as session:
                focus_codes: set[str] = set(
                    str(code) for code in (
                        await session.scalars(
                            select(PaperPosition.code).where(PaperPosition.is_closed.is_(False))
                        )
                    ).all() if code
                )
                focus_codes.update(
                    str(code) for code in (
                        await session.scalars(
                            select(TradeOrder.code).where(
                                TradeOrder.status.in_(["pending", "submitted", "partial"])
                            )
                        )
                    ).all() if code
                )
                focus_codes.update(
                    str(code) for code in (
                        await session.scalars(
                            select(PaperAutoTradeLog.code).where(
                                PaperAutoTradeLog.trade_date == payload["trade_date"],
                                PaperAutoTradeLog.code.is_not(None),
                            )
                        )
                    ).all() if code
                )
                for model in (LimitUpPool, BrokenLimitPool):
                    focus_codes.update(
                        str(code) for code in (
                            await session.scalars(
                                select(model.code).where(model.trade_date == payload["trade_date"])
                            )
                        ).all() if code
                    )

            result = await asyncio.to_thread(
                quote_round_archive.write_round,
                payload,
                payload.get("records") or [],
                focus_codes=focus_codes,
            )
            async with async_session() as session:
                row = await session.scalar(
                    select(QuoteRound).where(QuoteRound.round_id == round_id)
                )
                if row:
                    row.archive_status = result["archive_status"]
                    row.archive_path = result["archive_path"]
                    row.minute_archive_path = result["minute_archive_path"]
                    row.focus_path = result.get("focus_path")
                    row.focus_count = int(result["focus_count"])
                    await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"行情轮次归档失败: round={round_id}")
            async with async_session() as session:
                row = await session.scalar(
                    select(QuoteRound).where(QuoteRound.round_id == round_id)
                )
                if row:
                    row.archive_status = "failed"
                    row.quality_reason = ";".join(
                        part for part in (row.quality_reason, f"archive:{exc}") if part
                    )
                    await session.commit()

    def _schedule_quote_round_archive(self, payload: dict) -> None:
        if not settings.QUOTE_ROUND_ARCHIVE_ENABLED:
            return
        task = asyncio.create_task(self._archive_quote_round(payload))
        self._quote_archive_tasks.add(task)
        task.add_done_callback(self._quote_archive_tasks.discard)

    def _start_paper_buy_point_push_loop(self, loop: asyncio.AbstractEventLoop):
        if self._paper_buy_point_push_task and not self._paper_buy_point_push_task.done():
            return
        self._paper_buy_point_push_task = loop.create_task(self._paper_buy_point_push_loop())

    async def _paper_buy_point_push_loop(self):
        # 消费本轮已提交买点，独立于交易锁；休市也清理过期/失败通知，但不会生成信号。
        from app.push.paper_buy_points import dispatch_buy_points
        await asyncio.sleep(2)
        while self.scheduler.running:
            try:
                await dispatch_buy_points()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("策略买点推送心跳失败：{}", type(exc).__name__)
            await asyncio.sleep(settings.PAPER_BUY_POINT_PUSH_INTERVAL_SEC)

    def _start_anomaly_push_loop(self, loop: asyncio.AbstractEventLoop):
        """独立启动异动推送心跳，避免依赖行情采集任务的完成时机。"""
        if self._anomaly_push_loop_task and not self._anomaly_push_loop_task.done():
            return
        self._anomaly_push_loop_task = loop.create_task(self._anomaly_push_loop())

    def _request_anomaly_scan(self, trigger: str) -> None:
        """合并短时间内的行情事件，并唤醒一次异动扫描。"""
        if self._anomaly_snapshot_refreshing:
            self._anomaly_snapshot_pending = True
        if not self._anomaly_scan_event.is_set():
            self._anomaly_scan_requested_at = _time.monotonic()
        self._anomaly_scan_trigger = trigger or "market_data"
        self._anomaly_scan_event.set()

    def _take_anomaly_scan_request(self) -> tuple[str, float | None]:
        trigger = self._anomaly_scan_trigger
        requested_at = self._anomaly_scan_requested_at
        self._anomaly_scan_event.clear()
        self._anomaly_scan_trigger = "interval_fallback"
        self._anomaly_scan_requested_at = None
        return trigger, requested_at

    def _anomaly_scan_throttle_delay(self, now: float | None = None) -> float:
        """返回下一次全市场扫描需等待的秒数。

        行情每批提交都可唤醒扫描，但同一轮重扫本身会读取大量快照、
        K线和资金数据。若扫描期间的数据事件立即再启一轮，会长期占满
        事件循环，拖慢页面/API。这里只合并重扫，不丢弃已排队事件。
        """
        if self._last_anomaly_scan_started_at is None:
            return 0.0
        current = _time.monotonic() if now is None else now
        min_gap = max(5.0, float(settings.ANOMALY_PUSH_MIN_FULL_SCAN_GAP_SEC))
        return max(0.0, min_gap - (current - self._last_anomaly_scan_started_at))

    async def _anomaly_push_loop(self):
        """行情提交后立即扫描；固定间隔仅作漏事件兜底。"""
        await asyncio.sleep(2)
        interval = max(10, int(settings.ANOMALY_PUSH_SCAN_INTERVAL_SEC))
        while self.scheduler.running:
            try:
                trigger = "interval_fallback"
                requested_at = None
                try:
                    await asyncio.wait_for(self._anomaly_scan_event.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
                else:
                    trigger, requested_at = self._take_anomaly_scan_request()
                if await trade_calendar.is_trading_hours():
                    throttle_delay = self._anomaly_scan_throttle_delay()
                    if throttle_delay > 0:
                        await asyncio.sleep(throttle_delay)
                    self._last_anomaly_scan_started_at = _time.monotonic()
                    await self._refresh_anomaly_snapshot_background(
                        trigger=trigger,
                        requested_at=requested_at,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"异动推送心跳执行失败: {exc}")

    async def _startup_prewarm(self):
        """服务启动后立即预热一次生命周期快照，避免页面首开冷启动。"""
        await asyncio.sleep(0.5)
        try:
            async with async_session() as session:
                await self._prewarm_lifecycle_snapshot(session, None)
            async with async_session() as session:
                await self._prewarm_anomaly_snapshot(session, None)
            async with async_session() as session:
                await self._prewarm_tenbagger_rank(session)
            async with async_session() as session:
                await self._prewarm_tenbagger_dragon(session)
            async with async_session() as session:
                await self._prewarm_tenbagger_plan(session)
            logger.info("启动常规预热不写晋级预测；正式盘中窗口由独立补跑任务恢复不可变快照")
        except Exception as e:
            logger.warning(f"启动预热快照失败: {e}")

    @staticmethod
    async def _close_snapshot_health(session: AsyncSession, target_date: date) -> dict:
        """检查生产研究宇宙收盘覆盖；来源名不替代有限OHLC与逐股终场核验。"""
        from app.data.price_chain import FORMAL_CLOSE_SOURCES
        valid_bar = and_(
            *(and_(getattr(StockKline, field) > 0,
                   getattr(StockKline, field) < float("inf"))
              for field in ("open", "close", "high", "low")),
            StockKline.high >= StockKline.open,
            StockKline.high >= StockKline.close,
            StockKline.low <= StockKline.open,
            StockKline.low <= StockKline.close,
        )
        scope_filters = (
            or_(
                StockTag.board_tag.in_(["tradeable", "observe_only"]),
                StockTag.is_st.is_(True),
            ),
            StockTag.is_suspended.is_(False),
            or_(StockTag.name.is_(None), ~StockTag.name.like("退%")),
        )
        expected = int(
            await session.scalar(
                select(func.count()).select_from(StockTag).where(*scope_filters)
            )
            or 0
        )
        use_tag_scope = expected > 0
        source_rows = (
            await session.execute(
                select(StockKline.source, func.count(func.distinct(StockKline.code)))
                .join(StockTag, StockTag.code == StockKline.code)
                .where(StockKline.trade_date == target_date, *scope_filters)
                .group_by(StockKline.source)
            )
        ).all()
        source_counts = {str(source or "legacy_unknown"): int(count or 0) for source, count in source_rows}
        close_cutoff = datetime.combine(target_date, time(15, 0))
        next_day = datetime.combine(target_date + timedelta(days=1), time.min)
        fresh_tencent_close_count = int(
            await session.scalar(
                select(func.count(func.distinct(StockKline.code)))
                .select_from(StockKline)
                .join(StockTag, StockTag.code == StockKline.code)
                .join(StockSpot, StockSpot.code == StockKline.code)
                .where(
                    StockKline.trade_date == target_date,
                    StockKline.source == "tencent_close",
                    StockSpot.updated_at >= close_cutoff,
                    StockSpot.updated_at < next_day,
                    *scope_filters,
                )
            )
            or 0
        )
        if expected <= 0:
            expected = int(
                await session.scalar(
                    select(func.count()).select_from(StockSpot).where(
                        StockSpot.updated_at >= datetime.combine(target_date, time.min),
                        StockSpot.updated_at < datetime.combine(target_date + timedelta(days=1), time.min),
                    )
                )
                or 0
            )
            source_rows = (
                await session.execute(
                    select(StockKline.source, func.count(func.distinct(StockKline.code)))
                    .where(StockKline.trade_date == target_date)
                    .group_by(StockKline.source)
                )
            ).all()
            source_counts = {str(source or "legacy_unknown"): int(count or 0) for source, count in source_rows}
            fresh_tencent_close_count = int(
                await session.scalar(
                    select(func.count(func.distinct(StockKline.code)))
                    .select_from(StockKline)
                    .join(StockSpot, StockSpot.code == StockKline.code)
                    .where(
                        StockKline.trade_date == target_date,
                        StockKline.source == "tencent_close",
                        StockSpot.updated_at >= close_cutoff,
                        StockSpot.updated_at < next_day,
                    )
                )
                or 0
            )
        valid_query = select(StockKline.source, func.count(func.distinct(StockKline.code))).where(
            StockKline.trade_date == target_date, valid_bar,
        )
        if use_tag_scope:
            valid_query = valid_query.join(StockTag, StockTag.code == StockKline.code).where(*scope_filters)
        valid_sources = {str(source or "legacy_unknown"): int(count or 0)
                         for source, count in (await session.execute(
                             valid_query.group_by(StockKline.source))).all()}
        qualified_tencent_close_count = int(await session.scalar(
            valid_query.join(StockSpot, StockSpot.code == StockKline.code).where(
                StockKline.source == "tencent_close",
                StockSpot.updated_at >= close_cutoff, StockSpot.updated_at < next_day,
            ).with_only_columns(func.count(func.distinct(StockKline.code)))
        ) or 0)
        invalid_formal_count = sum(
            source_counts.get(source, 0) - valid_sources.get(source, 0) for source in FORMAL_CLOSE_SOURCES)
        unsupported_source_count = sum(count for source, count in source_counts.items()
                                       if source not in FORMAL_CLOSE_SOURCES | {"spot_fallback"})
        fallback_count = int(source_counts.get("spot_fallback", 0))
        stored_tencent_close_count = int(source_counts.get("tencent_close", 0))
        stale_tencent_close_count = max(
            stored_tencent_close_count - fresh_tencent_close_count,
            0,
        )
        # Same-session THS CDN bars can still be an intraday image after 15:10.
        # A source label alone is therefore not enough to call today's close
        # canonical: compare every persisted close with the fresh 15:00+ quote.
        conflicting_close_count = int(
            await session.scalar(
                select(func.count(func.distinct(StockKline.code)))
                .select_from(StockKline)
                .join(StockSpot, StockSpot.code == StockKline.code)
                .where(
                    StockKline.trade_date == target_date,
                    StockSpot.updated_at >= close_cutoff,
                    StockSpot.updated_at < next_day,
                    StockKline.close.is_not(None),
                    StockSpot.price > 0,
                    func.abs(StockKline.close - StockSpot.price) > 0.005,
                )
            )
            or 0
        )
        canonical_count = sum(
            valid_sources.get(source, 0) for source in FORMAL_CLOSE_SOURCES - {"tencent_close"}
        ) + qualified_tencent_close_count
        completeness = min(canonical_count / expected, 1.0) if expected else 0.0
        ready = bool(
            expected > 0
            and completeness >= 0.99
            and fallback_count == 0
            and stale_tencent_close_count == 0
            and conflicting_close_count == 0
            and invalid_formal_count == 0
            and unsupported_source_count == 0
        )
        return {
            "trade_date": target_date.isoformat(),
            "status": "ok" if ready else "blocked",
            "ready": ready,
            "expected_count": expected,
            "canonical_count": canonical_count,
            "fallback_count": fallback_count,
            "fresh_tencent_close_count": fresh_tencent_close_count,
            "qualified_tencent_close_count": qualified_tencent_close_count,
            "stale_tencent_close_count": stale_tencent_close_count,
            "conflicting_close_count": conflicting_close_count,
            "invalid_formal_ohlc_count": invalid_formal_count,
            "unsupported_source_count": unsupported_source_count,
            "completeness": round(completeness, 6),
            "source_counts": source_counts,
        }

    @staticmethod
    async def _reconcile_broken_limit_close_state(
        session: AsyncSession,
        target_date: date,
    ) -> dict:
        """用同日终场报价消除涨停/炸板交集并明确最终状态。

        盘中最后一帧涨停池可能早于15:00。先把终场已开板的陈旧涨停行
        迁入炸板池，再清理真正仍封板的交集，避免把尾盘炸板误记为涨停。
        """
        limit_rows = list(
            (
                await session.scalars(
                    select(LimitUpPool).where(LimitUpPool.trade_date == target_date)
                )
            ).all()
        )
        rows = list(
            (
                await session.scalars(
                    select(BrokenLimitPool).where(BrokenLimitPool.trade_date == target_date)
                )
            ).all()
        )
        all_codes = {
            str(item.code)
            for item in (*limit_rows, *rows)
            if str(item.code or "")
        }
        if not all_codes:
            return {"updated": 0, "removed_overlap": 0, "moved_stale_limit": 0}
        spots = {
            str(row.code): row
            for row in (
                await session.scalars(
                    select(StockSpot).where(StockSpot.code.in_(all_codes))
                )
            ).all()
        }
        close_cutoff = datetime.combine(target_date, time(15, 0))

        def fresh_close_spot(code: str) -> StockSpot | None:
            spot = spots.get(str(code))
            if (
                spot is None
                or not isinstance(spot.updated_at, datetime)
                or spot.updated_at < close_cutoff
                or spot.updated_at.date() != target_date
            ):
                return None
            return spot

        broken_by_code = {str(row.code): row for row in rows}
        moved_codes: set[str] = set()
        for limit_row in limit_rows:
            spot = fresh_close_spot(str(limit_row.code))
            if spot is None:
                continue
            close_price = _safe_float(spot.price)
            limit_price = _safe_float(spot.limit_up) or _safe_float(limit_row.limit_up_price)
            if not close_price or not limit_price:
                continue
            if round(close_price, 2) >= round(limit_price, 2):
                continue
            code = str(limit_row.code)
            broken = broken_by_code.get(code)
            if broken is None:
                broken = BrokenLimitPool(
                    code=code,
                    name=limit_row.name,
                    trade_date=target_date,
                    limit_up_time=limit_row.limit_up_time,
                    seal_amount=limit_row.seal_amount,
                    source="eastmoney+tencent",
                )
                session.add(broken)
                rows.append(broken)
                broken_by_code[code] = broken
            broken.limit_up_price = limit_price
            broken.close_price = close_price
            broken.close_at_limit = False
            broken.final_state = "broken"
            await session.delete(limit_row)
            moved_codes.add(code)

        limit_codes = {
            str(row.code)
            for row in limit_rows
            if str(row.code) not in moved_codes
        }
        updated = 0
        removed_overlap = 0
        for row in rows:
            if str(row.code) in limit_codes:
                await session.delete(row)
                removed_overlap += 1
                continue
            spot = fresh_close_spot(str(row.code))
            if spot is None:
                row.final_state = "unknown"
                row.close_at_limit = None
                continue
            close_price = _safe_float(spot.price)
            limit_price = _safe_float(spot.limit_up) or _safe_float(row.limit_up_price)
            row.close_price = close_price
            row.limit_up_price = limit_price
            if close_price and limit_price:
                row.close_at_limit = round(close_price, 2) >= round(limit_price, 2)
                row.final_state = "reclosed" if row.close_at_limit else "broken"
            else:
                row.close_at_limit = None
                row.final_state = "unknown"
            updated += 1
        await session.commit()
        return {
            "updated": updated,
            "removed_overlap": removed_overlap,
            "moved_stale_limit": len(moved_codes),
        }

    async def _finalize_close_snapshot(self) -> dict:
        """收盘DAG：终场spot → canonical K线 → 炸板终态 → 情绪快照 → 健康验收。"""
        target_date = date.today()
        if not await trade_calendar.is_trade_day(target_date):
            return {"status": "skipped", "reason": "non_trade_day"}
        spot_result = await self._tencent_spot_collect(force=True)
        kline_result = await self._spot_to_kline_fill(finalize_close=True)
        async with async_session() as session:
            broken_result = await self._reconcile_broken_limit_close_state(session, target_date)
        await self._intraday_indices_and_sentiment()
        async with async_session() as session:
            health = await self._close_snapshot_health(session, target_date)
            if health["ready"]:
                await data_quality_guard.record_success(
                    session,
                    "close_snapshot",
                    "finalize",
                    latency_ms=0,
                    record_count=int(health["canonical_count"]),
                    expected_count=int(health["expected_count"]),
                )
            else:
                await data_quality_guard.record_failure(
                    session,
                    "close_snapshot",
                    "finalize",
                    (
                        f"canonical={health['canonical_count']}/{health['expected_count']} "
                        f"fallback={health['fallback_count']} "
                        f"stale_tencent_close={health['stale_tencent_close_count']} "
                        f"conflicting_close={health['conflicting_close_count']} "
                        f"sources={health['source_counts']}"
                    ),
                )
        result = {
            **health,
            "spot": spot_result,
            "kline": kline_result,
            "broken_pool": broken_result,
        }
        logger.info(f"收盘行情固化完成: {result}")
        return result

    async def _ensure_close_snapshot_ready(self, session: AsyncSession) -> dict:
        target_date = date.today()
        health = await self._close_snapshot_health(session, target_date)
        if health.get("ready"):
            return health
        await self._finalize_close_snapshot()
        # 本函数仅在正式快照生成前调用，此时尚无业务写入；结束旧读事务以读取终场提交。
        await session.rollback()
        return await self._close_snapshot_health(session, target_date)

    # =========================================================================
    # 腾讯实时行情采集 (30s/轮)
    # =========================================================================

    async def _tencent_spot_collect(self, *, force: bool = False):
        """腾讯实时行情采集 → stock_spot 表(单行覆盖)

        采集范围: StockTag中可交易主板 + 仅观察板 + ST研究池（交易风控仍独立屏蔽）
        频率: 盘中30秒/轮, 盘后15:01也采集一次(用于盘后展示)
        非交易日跳过
        """
        now = datetime.now()
        if now.weekday() >= 5:
            return {"status": "skipped", "reason": "weekend", "collected": 0}
        is_trade_day = await trade_calendar.is_trade_day(now.date())
        self._pipeline_runtime_health.observe_calendar(now.date(), bool(is_trade_day))
        if not is_trade_day:
            return {"status": "skipped", "reason": "non_trade_day", "collected": 0}

        # 集合竞价/盘中持续采集，收盘后5分钟内补最终快照。
        # 腾讯夜间只会重复返回收盘数据，继续30秒全市场轮询会挤占调度器。
        session_name = trade_calendar.get_trade_session(now)
        closing_snapshot_window = now.hour == 15 and now.minute < 5
        if (
            not force
            and session_name not in {"pre_auction", "morning", "afternoon"}
            and not closing_snapshot_window
        ):
            return {"status": "skipped", "reason": f"session:{session_name}", "collected": 0}

        tencent: TencentSource = self._sources["tencent"]

        if not self._tradeable_codes:
            async with async_session() as session:
                result = await session.execute(
                    select(StockTag.code).where(
                        or_(
                            StockTag.board_tag.in_(["tradeable", "observe_only"]),
                            StockTag.is_st == True,
                        ),
                        StockTag.is_suspended == False,
                        or_(StockTag.name.is_(None), ~StockTag.name.like("退%")),
                    )
                )
                self._tradeable_codes = [r[0] for r in result.all()]
                logger.info(f"[tencent] 可交易+仅观察+ST研究行情: {len(self._tradeable_codes)}只")

        if not self._tradeable_codes:
            return {"status": "missing", "reason": "empty_universe", "collected": 0}

        try:
            spots = await tencent.collect_spot_batch(self._tradeable_codes)
        except Exception as e:
            logger.error(f"[tencent] 采集失败: {e}")
            return {"status": "failed", "reason": str(e), "collected": 0}

        if not spots:
            return {"status": "missing", "reason": "empty_response", "collected": 0}

        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from app.data.quote_round import build_quote_round_record

        # 用全市场抓取完成时刻作为提交时点；源/接收/提交三套时钟独立保留。
        collected_at = datetime.now()
        records = [
            {
                **spot,
                "received_at": spot.get("received_at") or collected_at,
                "updated_at": collected_at,
            }
            for spot in spots
        ]
        insert_stmt = sqlite_insert(StockSpot)
        update_values = {
            column.name: getattr(insert_stmt.excluded, column.name)
            for column in StockSpot.__table__.columns
            if column.name != "code"
        }
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=["code"],
            set_=update_values,
        )
        async with async_session() as session:
            component_watermarks = {
                "fund_flow_observed_at": await session.scalar(select(func.max(FundFlow.observed_at))),
                "sentiment_observed_at": await session.scalar(select(func.max(MarketSentiment.observed_at))),
                # sector_persistence 目前只有交易日粒度，明确记录精度而不伪造盘中时点。
                "sector_trade_date": await session.scalar(select(func.max(SectorPersistence.trade_date))),
                "sector_watermark_precision": "trade_date_only",
            }
            previous_committed_at = await session.scalar(
                select(func.max(QuoteRound.committed_at)).where(
                    QuoteRound.trade_date == collected_at.date(),
                )
            )
            round_record = build_quote_round_record(
                records,
                expected_count=len(self._tradeable_codes),
                committed_at=collected_at,
                component_watermarks=component_watermarks,
                previous_committed_at=previous_committed_at,
            )
            await session.execute(upsert_stmt, records)
            session.add(QuoteRound(**round_record))
            await session.commit()
            logger.debug(
                f"[tencent] 写入 {len(spots)} 条实时行情, round={round_record['round_id']} "
                f"quality={round_record['quality_status']}"
            )

        # 单轮新鲜不能覆盖掉历史断档；仅发出审计告警，不伪造路径或放宽买入门禁。
        continuity = self._pipeline_runtime_health.observe_quote(
            round_record, previous_committed_at=previous_committed_at,
            visible_at=datetime.now(),
        )
        if continuity["status"] in {"gap", "invalid_clock"}:
            logger.warning(
                "[tencent] 已提交行情轮次连续性异常: round={} status={} active_gap={}s threshold={}s",
                round_record["round_id"], continuity["status"],
                continuity["active_gap_sec"], continuity["max_gap_sec"],
            )

        # 单行 upsert 会覆盖上一轮；提交后立即把最小行情叶子字段放入有界 inbox，
        # 扫描繁忙时下一轮仍可按 source_quote_at 回放 A/B/C 连续路径。
        from app.signal.anomaly_scanner import anomaly_scanner

        anomaly_scanner.enqueue_quote_batch(records)

        payload = {
            **round_record,
            "records": records,
            "records_by_code": {
                str(item["code"]): item
                for item in records
                if item.get("code")
            },
        }
        self._schedule_quote_round_archive(payload)
        if session_name in {"morning", "afternoon"}:
            if round_record["quality_status"] == "ok":
                self._last_healthy_quote_round_at = collected_at
            self._publish_quote_round(payload)
        # 行情提交后先无await发布，再做可能访问日历库的通知；避免已采集帧等待日历查询。
        if await trade_calendar.is_trading_hours():
            self._request_anomaly_scan("tencent_spot_commit")
        return {
            "status": "ok" if round_record["quality_status"] == "ok" else "degraded",
            "collected": len(records),
            "updated_at": collected_at.isoformat(),
            "round_id": round_record["round_id"],
            "as_of_at": round_record["as_of_at"].isoformat(),
            "coverage": round_record["coverage"],
            "quality_reason": round_record["quality_reason"],
            "forced": bool(force),
        }

    # =========================================================================
    # 同花顺日K线采集 (盘后1次)
    # =========================================================================

    async def _persist_kline_observations(
        self, session: AsyncSession, records: list[dict], *, trigger: str,
    ) -> dict:
        from app.data.kline_observations import persist_kline_observations

        self._kline_observation_health = {
            "status": "recording", "trigger": trigger, "committed": False,
        }
        try:
            now = datetime.now()
            projection_day = now.date() if await trade_calendar.is_trade_day(now.date()) else None
            result = await persist_kline_observations(session, records, now=now, projection_day=projection_day)
        except Exception as exc:
            self._kline_observation_health = {
                "status": "failed", "trigger": trigger, "committed": False,
                "error_type": type(exc).__name__,
            }
            raise
        result["trigger"] = trigger
        # Caller must commit successfully before publishing committed=True.
        self._kline_observation_health = {**result, "committed": False}
        return result

    async def _ths_kline_daily(self):
        """同花顺日K线盘后增量采集 → stock_kline 表"""
        ths: ThsKlineSource = self._sources["ths_kline"]

        async with async_session() as session:
            result = await session.execute(
                select(StockTag.code).where(
                    or_(
                        StockTag.board_tag.in_(["tradeable", "observe_only"]),
                        StockTag.is_st == True,
                    ),
                    StockTag.is_suspended == False,
                    or_(StockTag.name.is_(None), ~StockTag.name.like("退%")),
                )
            )
            codes = [r[0] for r in result.all()]

        if not codes:
            return

        logger.info(f"[ths_kline] 开始盘后K线采集(可交易+仅观察+ST研究): {len(codes)}只")

        all_klines = []
        batch_size = 5
        async def collect_one(code: str) -> list[dict]:
            try:
                rows = await ths.collect_daily(code)
                return rows or []
            except Exception as exc:
                logger.warning(f"[ths_kline] {code} 采集失败: {exc}")
                return []

        for i in range(0, len(codes), batch_size):
            batch = codes[i:i + batch_size]
            batch_rows = await asyncio.gather(*(collect_one(code) for code in batch))
            for rows in batch_rows:
                all_klines.extend(rows)
            await asyncio.sleep(ths.rate_limit)

        if not all_klines:
            logger.warning("[ths_kline] 无K线数据")
            return

        async with async_session() as session:
            records = []
            for k in all_klines:
                records.append({
                    "code": k["code"],
                    "trade_date": k["trade_date"],
                    "open": k.get("open"),
                    "close": k.get("close"),
                    "high": k.get("high"),
                    "low": k.get("low"),
                    "volume": k.get("volume"),
                    "amount": k.get("amount"),
                    "turnover": k.get("turnover"),
                    "change_pct": k.get("change_pct"),
                    "prev_close": k.get("prev_close"),
                    "source": k.get("source", "ths"),
                })
            result = await self._persist_kline_observations(session, records, trigger="ths_daily")
            await session.commit()
            self._kline_observation_health = {**result, "committed": True}
            logger.info("[ths_kline] 当日投影/隔离采证: {}", result)
            return result

    async def _spot_to_kline_fill(self, *, finalize_close: bool = False):
        """用stock_spot实时行情填充今日stock_kline
        
        同花顺CDN收盘后延迟更新(T+1~T+2)，spot有完整的开高低收量额换手涨跌幅昨收
        当日价格无需前复权，直接填入即可
        盘中/收盘后都适用，确保K线表始终有当日数据
        """
        from sqlalchemy import select, func
        from app.models.stock import StockKline, StockSpot

        current_day = date.today()

        async with async_session() as session:
            meta_result = await session.execute(
                select(func.max(StockSpot.updated_at), func.count()).select_from(StockSpot)
            )
            latest_spot_updated_at, spot_count = meta_result.one()
            spot_count = int(spot_count or 0)

            if spot_count <= 0:
                logger.warning("[spot_to_kline] stock_spot无数据，跳过")
                return {"status": "missing", "written": 0, "reason": "empty_spot"}

            effective_trade_date = _resolve_spot_snapshot_trade_date(
                latest_spot_updated_at,
                today=current_day,
            )
            if effective_trade_date is None:
                logger.warning(
                    f"[spot_to_kline] 最新spot时间非法: {latest_spot_updated_at}, 跳过"
                )
                return {"status": "blocked", "written": 0, "reason": "invalid_spot_time"}
            if not await trade_calendar.is_trade_day(effective_trade_date):
                # Non-trading rows remain evidence. Do not delete history as a
                # side effect of a collector; formal consumers exclude fallback.
                logger.warning(f"[spot_to_kline] {effective_trade_date} 非交易日，保留原记录并跳过")
                return {"status": "blocked", "written": 0, "reason": "non_trade_day",
                        "historical_rows_deleted": 0}

            if finalize_close and (
                not isinstance(latest_spot_updated_at, datetime)
                or latest_spot_updated_at.date() != effective_trade_date
                or latest_spot_updated_at.time() < time(15, 0)
            ):
                logger.warning(
                    f"[spot_to_kline] 收盘固化拒绝非终场spot: {latest_spot_updated_at}"
                )
                return {
                    "status": "blocked",
                    "written": 0,
                    "reason": "spot_before_close",
                    "latest_spot_updated_at": str(latest_spot_updated_at),
                }

            # A stale/future snapshot is not authority to erase other dates.
            # The observation writer isolates every non-current-day candidate.

            day_start = datetime.combine(effective_trade_date, datetime.min.time())
            day_end = day_start + timedelta(days=1)
            close_cutoff = datetime.combine(effective_trade_date, time(15, 0))
            spot_lower_bound = close_cutoff if finalize_close else day_start
            same_day_spot_count = int(
                await session.scalar(
                    select(func.count()).select_from(StockSpot).where(
                        StockSpot.updated_at >= day_start,
                        StockSpot.updated_at < day_end,
                    )
                )
                or 0
            )

            spots = await session.execute(
                select(StockSpot).where(
                    StockSpot.updated_at >= spot_lower_bound,
                    StockSpot.updated_at < day_end,
                )
            )
            fresh_spots = spots.scalars().all()
            stale_same_day_count = max(same_day_spot_count - len(fresh_spots), 0)
            if not fresh_spots:
                logger.warning(
                    f"[spot_to_kline] 未找到 {effective_trade_date} 的spot快照，跳过"
                )
                await session.commit()
                return {
                    "status": "missing",
                    "written": 0,
                    "reason": (
                        "no_fresh_close_spot"
                        if finalize_close
                        else "no_same_day_spot"
                    ),
                    "same_day_spot_count": same_day_spot_count,
                    "stale_spot_count": stale_same_day_count,
                }

            # “本日昨收”与旧日K不一致可能是旧快照、混源或除权。
            # 差异再小也不是改写已观察历史OHLC/涨幅的授权；只记录缺失/冲突。
            # 最新存储日期不冒充经交易日历确认的紧邻交易日，正式收益窗口仍须
            # 经过 outcome_evidence 的日历和逐股价格链门禁。
            from app.data.price_chain import price_chain_evidence
            previous_trade_date = await session.scalar(
                select(func.max(StockKline.trade_date)).where(
                    StockKline.trade_date < effective_trade_date,
                )
            )
            previous_rows = {}
            if previous_trade_date is not None:
                previous_result = await session.execute(
                    select(StockKline.code, StockKline.close).where(
                        StockKline.trade_date == previous_trade_date,
                        StockKline.code.in_([str(spot.code) for spot in fresh_spots]),
                    )
                )
                previous_rows = {str(code): close for code, close in previous_result.all()}
            price_chain_issues = []
            price_chain_conflicts = 0
            for spot in fresh_spots:
                evidence = price_chain_evidence(previous_rows.get(str(spot.code)), spot.prev_close)
                if evidence["status"] != "consistent":
                    price_chain_conflicts += int(evidence["status"] == "conflict")
                    price_chain_issues.append({"code": str(spot.code), **evidence})
            price_chain_health = {
                "status": "degraded" if price_chain_issues else "ok",
                "observed_at": datetime.now().isoformat(),
                "trade_date": effective_trade_date.isoformat(),
                "previous_stored_bar_date": str(previous_trade_date) if previous_trade_date else None,
                "consecutive_trade_session_verified": False,
                "checked_count": len(fresh_spots), "conflict_count": price_chain_conflicts,
                "unknown_count": len(price_chain_issues) - price_chain_conflicts,
                "examples": price_chain_issues[:50], "example_limit": 50,
                "historical_prices_changed": False,
            }
            self._kline_price_chain_health = price_chain_health
            if price_chain_issues:
                logger.warning("[spot_to_kline] 保留原K线，价格链须隔离核查: {}", price_chain_health)

            # Keep previous close rows. _close_snapshot_health independently
            # excludes stale rows from canonical coverage and blocks readiness.
            # Replacing a valid current-day row first archives the original.
            cleared_close_count = 0

            # 盘中 spot_fallback 只负责补缺口；但 finalize_close=True 消费的是
            # 逐股15:00后终场报价，必须覆盖同日THS CDN盘中残影。历史THS日K不受
            # 影响，且后续THS任务会单独保护这批 fresh tencent_close 终值。
            existing_result = await session.execute(
                select(StockKline.code, StockKline.source).where(
                    StockKline.trade_date == effective_trade_date,
                    StockKline.code.in_([str(spot.code) for spot in fresh_spots]),
                )
            )
            existing_sources = {
                str(code): str(source or "")
                for code, source in existing_result.all()
            }

            records = []
            for spot in fresh_spots:
                existing_source = existing_sources.get(str(spot.code), "")
                if not finalize_close and existing_source and existing_source != "spot_fallback":
                    continue
                price = spot.price
                # Structural validation and invalid-value evidence are handled
                # by the shared writer; never let NaN/Inf bypass comparisons.
                records.append({
                    "code": spot.code,
                    "trade_date": effective_trade_date,
                    "open": spot.open,
                    "close": price,
                    "high": spot.high,
                    "low": spot.low,
                    # stock_spot.volume 当前口径是“手”，而 stock_kline.volume 统一要求“股”
                    # 这里转成股，避免 spot_fallback 写入后日K成交量缩小 100 倍。
                    "volume": spot.volume * 100 if spot.volume is not None else None,
                    "amount": spot.amount,
                    "turnover": spot.turnover,
                    "change_pct": spot.change_pct,
                    "prev_close": spot.prev_close,
                    "source": "tencent_close" if finalize_close else "spot_fallback",
                })

            result = await self._persist_kline_observations(
                session, records, trigger="spot_close" if finalize_close else "spot_fallback",
            )
            await session.commit()
            self._kline_observation_health = {**result, "committed": True}
            logger.info("[spot_to_kline] 当日投影/隔离采证: {}", result)
            return {
                "status": result["status"] if records else "unchanged",
                "price_chain_quality": price_chain_health,
                "observation_quality": result,
                "written": result["written"],
                "spot_count": len(fresh_spots),
                "same_day_spot_count": same_day_spot_count,
                "stale_spot_count": stale_same_day_count,
                "cleared_previous_close_count": cleared_close_count,
                "trade_date": effective_trade_date.isoformat(),
                "source": "tencent_close" if finalize_close else "spot_fallback",
            }

    async def _ths_kline_recent_repair(self):
        """夜间定向采集近期可疑K线，历史差异只入隔离版本、不覆盖。

        仅处理 spot_fallback，以及“涨停池记录为涨停但日K涨幅明显不符”的代码，
        避免对全市场反复回拉造成数据源压力。
        """
        repair_end = date.today()
        repair_start = repair_end - timedelta(days=21)
        async with async_session() as session:
            mismatch_result = await session.execute(
                select(StockKline.code)
                .join(
                    LimitUpPool,
                    and_(
                        LimitUpPool.code == StockKline.code,
                        LimitUpPool.trade_date == StockKline.trade_date,
                    ),
                )
                .where(
                    StockKline.trade_date >= repair_start,
                    StockKline.trade_date <= repair_end,
                    func.coalesce(StockKline.change_pct, 0) < 8.8,
                )
                .distinct()
                .limit(120)
            )
            # fallback存量可能很大；每晚从最新缺口分批向历史推进。
            # 与涨停池冲突的代码永远优先，不能被普通fallback挤出限额。
            fallback_result = await session.execute(
                select(StockKline.code)
                .where(
                    StockKline.trade_date >= repair_start,
                    StockKline.trade_date <= repair_end,
                    StockKline.source == "spot_fallback",
                )
                .group_by(StockKline.code)
                .order_by(func.max(StockKline.trade_date).desc(), StockKline.code)
                .limit(220)
            )
            codes = list(dict.fromkeys(
                [str(code) for code in mismatch_result.scalars().all() if code]
                + [str(code) for code in fallback_result.scalars().all() if code]
            ))[:300]

        if not codes:
            logger.debug("[ths_kline_repair] 近期无可疑K线")
            return

        ths: ThsKlineSource = self._sources["ths_kline"]
        repaired: list[dict] = []
        async def repair_one(code: str) -> list[dict]:
            try:
                return await ths.collect_repair(
                    code,
                    lookback_days=21,
                    as_of=repair_end,
                    fetch_attempts=3,
                )
            except Exception as exc:
                logger.warning(f"[ths_kline_repair] {code} 回补失败: {exc}")
                return []

        # 与日常采集一致，限制5路并发；避免300只逐只回补拖过夜间窗口。
        for index in range(0, len(codes), 5):
            batch_rows = await asyncio.gather(
                *(repair_one(code) for code in codes[index:index + 5])
            )
            for rows in batch_rows:
                repaired.extend(rows)

        if not repaired:
            logger.warning(f"[ths_kline_repair] {len(codes)}只可疑标的未拉取到有效日K")
            return

        records = [
            {
                "code": row["code"],
                "trade_date": row["trade_date"],
                "open": row.get("open"),
                "close": row.get("close"),
                "high": row.get("high"),
                "low": row.get("low"),
                "volume": row.get("volume"),
                "amount": row.get("amount"),
                "turnover": row.get("turnover"),
                "change_pct": row.get("change_pct"),
                "prev_close": row.get("prev_close"),
                "source": row.get("source", "ths"),
            }
            for row in repaired
        ]
        async with async_session() as session:
            result = await self._persist_kline_observations(session, records, trigger="ths_recent_repair")
            await session.commit()
            self._kline_observation_health = {**result, "committed": True}
        logger.info("[ths_kline_repair] 定向采证 {} 只，历史候选不覆盖原记录: {}", len(codes), result)
        return result

    # =========================================================================
    # 派生计算: 板块持续性+强弱+生命周期
    # =========================================================================

    async def _sector_derive(self):
        """板块派生计算 — 从原始数据计算持续性/强弱/生命周期

        每2分钟运行一次, 仅在交易时段执行:
        1. 概念资金流 → SectorPersistence (fund_flow/change_pct/strength_score)
        2. 涨停分布 → SectorPersistence (limit_up_count/consecutive_days)
        3. SectorRotationEngine → SectorStrength (强弱排名)
        4. SectorLifecycleAnalyzer → SectorLifecycle (生命周期状态)
        """
        if not await trade_calendar.is_trading_hours():
            return

        today = date.today()
        async with async_session() as session:
            try:
                t0 = _time.monotonic()

                # ---- 1. 从概念资金流更新 SectorPersistence ----
                await self._update_sector_persistence(session, today)

                # ---- 2. 计算板块强弱排名 ----
                from app.risk.rotation import SectorRotationEngine
                engine = SectorRotationEngine()
                items = await engine.calc_sector_strength(session, today)
                if items:
                    await engine.save_strength_ranking(session, items, today)

                # ---- 3. 计算生命周期(只算有涨停的板块, 避免全量计算) ----
                await self._compute_lifecycle(session, today)

                # ---- 4. 预热生命周期页面快照(概念/行业) ----
                await self._prewarm_lifecycle_snapshot(session, today)

                await session.commit()
                total_ms = int((_time.monotonic() - t0) * 1000)
                logger.debug(f"板块派生计算完成: {total_ms}ms")
            except Exception as e:
                logger.error(f"板块派生计算失败: {e}")
                await session.rollback()

    async def _update_sector_persistence(self, session: AsyncSession, trade_date: date):
        """从概念资金流+涨停数据更新 SectorPersistence

        逻辑:
        - 概念资金流(AkShare采集的) → fund_flow / change_pct / strength_score
        - 涨停池(LimitUpPool) → limit_up_count (按板块统计)
        - 连续天数 → 和前一交易日比较
        """
        # 获取概念板块资金流(行业资金流可能报错, 容错)
        ak_src = self._sources["akshare"]
        try:
            df_concept = await ak_src.get_sector_fund_flow("概念")
        except Exception as e:
            logger.warning(f"概念资金流获取失败: {e}")
            df_concept = pd.DataFrame()
        try:
            df_industry = await ak_src.get_sector_fund_flow("行业")
        except Exception as e:
            logger.warning(f"行业资金流获取失败: {e}")
            df_industry = pd.DataFrame()

        # 获取今日涨停按板块分布
        limit_up_result = await session.execute(
            select(LimitUpPool).where(LimitUpPool.trade_date == trade_date)
        )
        limit_ups = limit_up_result.scalars().all()

        # 统计每个板块的“归因涨停数”。不能把映射到华为、锂电、国企改革等
        # 大概念的所有涨停机械累计，否则会把板块整体下跌/流出的泛概念误判
        # 成为主线。这里与生命周期引擎复用同一套涨停归因规则。
        limit_up_entries_by_sector: dict[str, list[LimitUpPool]] = {}
        if limit_ups:
            codes = [lu.code for lu in limit_ups if lu.code]
            if codes:
                mapping_result = await session.execute(
                    select(StockSectorMapping.sector_code, StockSectorMapping.code)
                    .where(StockSectorMapping.code.in_(codes))
                )
                limit_up_by_code = {str(lu.code): lu for lu in limit_ups if lu.code}
                for sector_code, stock_code in mapping_result.all():
                    entry = limit_up_by_code.get(str(stock_code or ""))
                    if entry is not None:
                        limit_up_entries_by_sector.setdefault(str(sector_code), []).append(entry)

        # 获取前一交易日的SectorPersistence(用于计算consecutive_days)
        prev_result = await session.execute(
            select(SectorPersistence)
            .where(SectorPersistence.trade_date < trade_date)
            .order_by(SectorPersistence.trade_date.desc())
            .limit(654)  # 全量板块
        )
        prev_records = prev_result.scalars().all()
        prev_map = {r.sector_code: r for r in prev_records}

        # 预加载SectorInfo建立名称映射(行业一二级名→三级sector_code列表)
        si_result = await session.execute(
            select(SectorInfo.sector_code, SectorInfo.sector_name, SectorInfo.sector_type)
            .where(SectorInfo.source == "pywencai")
        )
        all_si = si_result.all()
        # 精确名称映射: (sector_name, sector_type) → sector_code
        si_name_map = {}
        # 行业子映射: 一级或二级名 → [sector_code, ...]
        industry_sub_map = {}
        for si_row in all_si:
            si_name_map[(si_row.sector_name, si_row.sector_type)] = si_row.sector_code
            # 概念去空格
            clean_key = (si_row.sector_name.replace(" ", "").replace("\u3000", ""), si_row.sector_type)
            if clean_key not in si_name_map:
                si_name_map[clean_key] = si_row.sector_code
            if si_row.sector_type == "industry":
                parts = si_row.sector_name.split("-")
                # 一级名(如"电力设备")
                l1 = parts[0] if parts else si_row.sector_name
                if l1 not in industry_sub_map:
                    industry_sub_map[l1] = []
                industry_sub_map[l1].append(si_row.sector_code)
                # 二级名(如"电池"→"电力设备-电池")
                if len(parts) >= 2:
                    l2 = parts[1]
                    if l2 not in industry_sub_map:
                        industry_sub_map[l2] = []
                    industry_sub_map[l2].append(si_row.sector_code)

        from app.sector.lifecycle import SectorLifecycleEngine
        attribution_engine = SectorLifecycleEngine()

        def attributed_limit_up_count(
            sector_code: str,
            sector_name: str,
            *,
            sector_type: str,
            strength_without_breadth: float,
            fund_flow: float,
        ) -> int:
            entries = list({
                str(entry.code): entry
                for entry in limit_up_entries_by_sector.get(str(sector_code), [])
                if entry.code
            }.values())
            if not entries:
                return 0
            if sector_type == "industry":
                return len(entries)
            attributed, _confidence = attribution_engine._filter_attributed_entries_with_meta(
                entries,
                sector_name,
                lambda entry: entry.limit_up_reason,
                allow_blank_mapped=True,
                sector_strength=strength_without_breadth,
                sector_fund_flow=fund_flow,
            )
            return len({str(entry.code) for entry in attributed if entry.code})

        # 更新概念+行业板块
        records = []
        for df, sector_type in [(df_concept, "concept"), (df_industry, "industry")]:
            if df is None or not isinstance(df, pd.DataFrame) or len(df) == 0:
                continue

            for _, row in df.iterrows():
                sector_name = str(row.get("行业", "") or row.get("名称", ""))
                if not sector_name or sector_name == "nan":
                    continue

                fund_flow = _safe_float(row.get("净额", row.get("主力净流入-净额", row.get("今日主力净流入", row.get("主力净流入", 0)))))
                change_pct = _safe_float(row.get("行业-涨跌幅", row.get("今日涨跌幅", row.get("涨跌幅", row.get("行业涨跌幅", 0)))))
                stock_count = _safe_int(row.get("公司家数", row.get("个股数", 0)))

                # 匹配sector_code: 精确匹配优先，行业模糊匹配(一二级→三级)
                matched_code = si_name_map.get((sector_name, sector_type))
                if not matched_code:
                    clean = sector_name.replace(" ", "").replace("\u3000", "")
                    matched_code = si_name_map.get((clean, sector_type))

                if matched_code:
                    # 精确匹配: 单条写入
                    base_strength = 0.0
                    if fund_flow is not None:
                        if fund_flow > 20: base_strength += 40
                        elif fund_flow > 5: base_strength += 30
                        elif fund_flow > 0: base_strength += 15
                    if change_pct is not None:
                        base_strength += min(max(change_pct * 5, -20), 30)
                    lu_count = attributed_limit_up_count(
                        matched_code,
                        sector_name,
                        sector_type=sector_type,
                        strength_without_breadth=base_strength,
                        fund_flow=fund_flow or 0,
                    )
                    prev = prev_map.get(matched_code)
                    consecutive_days = _next_sector_active_days(
                        prev.consecutive_days if prev else 0,
                        lu_count,
                        fund_flow or 0,
                    )
                    score = base_strength
                    if lu_count > 0:
                        score += min(lu_count * 5, 30)
                    score = max(0, min(100, round(score, 1)))

                    # 获取DB中的正确sector_name
                    db_name = sector_name
                    for si_row in all_si:
                        if si_row.sector_code == matched_code:
                            db_name = si_row.sector_name
                            break

                    records.append({
                        "sector_code": matched_code,
                        "sector_name": db_name,
                        "trade_date": trade_date,
                        "consecutive_days": consecutive_days,
                        "limit_up_count": lu_count,
                        "fund_flow": round(fund_flow or 0, 2),
                        "change_pct": round(change_pct or 0, 2),
                        "strength_score": score,
                    })
                elif sector_type == "industry":
                    # 行业名匹配一级/二级: 将资金流均匀分配到子行业
                    sub_codes = industry_sub_map.get(sector_name, [])
                    if sub_codes:
                        per_code_flow = round(fund_flow / len(sub_codes), 2) if fund_flow else 0
                        for sc in sub_codes:
                            base_strength = 0.0
                            if per_code_flow is not None:
                                if per_code_flow > 20: base_strength += 40
                                elif per_code_flow > 5: base_strength += 30
                                elif per_code_flow > 0: base_strength += 15
                            if change_pct is not None:
                                base_strength += min(max(change_pct * 5, -20), 30)
                            db_name = next(
                                (row.sector_name for row in all_si if row.sector_code == sc),
                                sector_name,
                            )
                            lu_count = attributed_limit_up_count(
                                sc,
                                db_name,
                                sector_type=sector_type,
                                strength_without_breadth=base_strength,
                                fund_flow=per_code_flow or 0,
                            )
                            prev = prev_map.get(sc)
                            consecutive_days = _next_sector_active_days(
                                prev.consecutive_days if prev else 0,
                                lu_count,
                                per_code_flow or 0,
                            )
                            sub_score = base_strength
                            if lu_count > 0:
                                sub_score += min(lu_count * 5, 30)
                            sub_score = max(0, min(100, round(sub_score, 1)))

                            records.append({
                                "sector_code": sc,
                                "sector_name": db_name,
                                "trade_date": trade_date,
                                "consecutive_days": consecutive_days,
                                "limit_up_count": lu_count,
                                "fund_flow": round(per_code_flow or 0, 2),
                                "change_pct": round(change_pct or 0, 2),
                                "strength_score": sub_score,
                            })

        if records:
            await self._batch_upsert(
                session, SectorPersistence, records,
                unique_cols=["sector_code", "trade_date"],
                update_cols=["sector_name", "consecutive_days", "limit_up_count",
                             "fund_flow", "change_pct", "strength_score"],
            )
            logger.info(f"板块持续性更新: {len(records)}条")

    async def _compute_lifecycle(self, session: AsyncSession, trade_date: date):
        """全量批处理板块生命周期，供雷达、晋级和板块营地共用同一快照。"""
        import json

        from app.sector.lifecycle import SectorLifecycleEngine
        from app.models.sector import SectorLifecycle

        analyzer = SectorLifecycleEngine()

        # 旧实现按强度只取前100名并逐板块N+1查询，低位轮动和趋势龙头所在
        # 板块经常根本没有生命周期记录。这里一次性分析全部已治理板块。
        persist_result = await session.execute(
            select(
                SectorPersistence.sector_code,
                SectorInfo.sector_name,
                SectorInfo.sector_type,
            )
            .join(SectorInfo, SectorInfo.sector_code == SectorPersistence.sector_code)
            .where(
                SectorPersistence.trade_date == trade_date,
                SectorInfo.source == "pywencai",
                SectorInfo.sector_type.in_(("concept", "industry")),
                func.coalesce(SectorInfo.is_excluded, 0) == 0,
            )
        )
        persist_sectors = [
            {
                "sector_code": str(row[0]),
                "sector_name": str(row[1]),
                "sector_type": str(row[2]),
            }
            for row in persist_result.all()
            if row[0] and row[1] and row[2]
        ]

        if not persist_sectors:
            return

        analyzed = await analyzer.analyze_sectors_batch(session, persist_sectors, trade_date)
        records = []
        for data in analyzed.values():
            records.append({
                "trade_date": data.trade_date,
                "sector_code": data.sector_code,
                "sector_name": data.sector_name,
                "sector_type": data.sector_type,
                "lifecycle_state": data.lifecycle_state.value,
                "state_score": analyzer.state_scores[data.lifecycle_state],
                "limit_up_count": data.limit_up_count,
                "first_board_count": data.first_board_count,
                "consecutive_board_count": data.consecutive_board_count,
                "max_board_height": data.max_board_height,
                "leader_stocks": json.dumps(data.leader_stocks or [], ensure_ascii=False),
                "ladder_stocks": json.dumps(data.ladders or [], ensure_ascii=False),
                "fund_flow": data.fund_flow,
                "fund_flow_3d": data.fund_flow_3d,
                "active_days": data.active_days,
                "total_active_5d": data.total_active_5d,
                "quality_score": data.quality_score,
                "is_main_line": 1 if data.is_main_line else 0,
                "kline_trend": data.kline_trend,
                "kline_vol_ratio": data.kline_vol_ratio,
                "kline_support": data.kline_support,
                "kline_resistance": data.kline_resistance,
                "kline_ma5": data.kline_ma5,
                "kline_ma20": data.kline_ma20,
                "kline_close": data.kline_close,
            })
        await self._batch_upsert(
            session,
            SectorLifecycle,
            records,
            unique_cols=["trade_date", "sector_code"],
        )
        logger.info(f"板块生命周期批处理完成: {len(records)}/{len(persist_sectors)}个板块")

    async def _prewarm_lifecycle_snapshot(self, session: AsyncSession, trade_date: date | None):
        """预热板块营地生命周期快照，避免页面首开冷启动。"""
        from app.api.v1.sectors import prewarm_lifecycle_snapshot

        warmed = await prewarm_lifecycle_snapshot(
            session,
            trade_date=trade_date,
            sector_types=["concept", "industry"],
            force_refresh=True,
        )
        if warmed:
            logger.debug(
                "生命周期快照预热完成: "
                + ", ".join(f"{sector_type}={count}" for sector_type, count in warmed.items())
            )

    async def _refresh_anomaly_snapshot_background(
        self,
        *,
        trigger: str = "interval_fallback",
        requested_at: float | None = None,
    ):
        if self._anomaly_snapshot_refreshing:
            return
        self._anomaly_snapshot_refreshing = True
        scan_started_at = _time.monotonic()
        queued_ms = (
            max(0, int((scan_started_at - requested_at) * 1000))
            if requested_at is not None
            else 0
        )
        try:
            async with async_session() as session:
                from app.api.v1.tenbagger import refresh_and_push_anomaly_snapshot

                payload = await asyncio.wait_for(
                    refresh_and_push_anomaly_snapshot(session, None),
                    timeout=max(10, int(settings.ANOMALY_PUSH_SCAN_TIMEOUT_SEC)),
                )
                anomalies = payload.get("anomalies") or []
                scan_ms = max(0, int((_time.monotonic() - scan_started_at) * 1000))
                end_to_end_ms = (
                    max(0, int((_time.monotonic() - requested_at) * 1000))
                    if requested_at is not None
                    else scan_ms
                )
                logger.debug(
                    "异动快照后台刷新完成: "
                    f"trigger={trigger} queue={queued_ms}ms scan={scan_ms}ms "
                    f"end_to_end={end_to_end_ms}ms total={len(anomalies)}"
                )
        except asyncio.TimeoutError:
            logger.warning(
                "异动快照后台刷新超时，已释放扫描锁: "
                f"trigger={trigger} queue={queued_ms}ms "
                f"timeout={settings.ANOMALY_PUSH_SCAN_TIMEOUT_SEC}s"
            )
        except Exception as e:
            logger.warning(
                f"异动快照后台刷新失败: trigger={trigger} queue={queued_ms}ms error={e}"
            )
        finally:
            self._anomaly_snapshot_refreshing = False
            if self._anomaly_snapshot_pending:
                self._anomaly_snapshot_pending = False
                self._request_anomaly_scan("pending_market_data")

    async def _prewarm_anomaly_snapshot(self, session: AsyncSession, trade_date: date | None):
        """预热牛股雷达异动快照，避免盘中页面每次现扫。"""
        from app.api.v1.tenbagger import prewarm_anomaly_snapshot

        payload = await prewarm_anomaly_snapshot(session, trade_date, force_refresh=False)
        anomalies = payload.get("anomalies") or []
        logger.debug(f"异动快照预热完成: total={len(anomalies)}")

    async def _prewarm_tenbagger_rank(self, session: AsyncSession):
        """预热牛股雷达强势排行，减少首次切 tab 卡顿。"""
        from app.api.v1.tenbagger import _bull_rank, _tenbagger_rank

        bull_payload = await _bull_rank(session)
        bull_rank = bull_payload.get("rank") or []
        tenbagger_payload = await _tenbagger_rank(session)
        tenbagger_rank = tenbagger_payload.get("rank") or []
        logger.debug(f"强势排行预热完成: bull={len(bull_rank)} tenbagger={len(tenbagger_rank)}")

    async def _prewarm_tenbagger_dragon(self, session: AsyncSession):
        """预热牛股雷达龙头追踪，减少首次切 tab 卡顿。"""
        from app.api.v1.tenbagger import prewarm_dragon_snapshot

        payload = await prewarm_dragon_snapshot(session, None, force_refresh=False)
        dragons = payload.get("dragons") or []
        logger.debug(f"龙头追踪预热完成: total={len(dragons)}")

    async def _prewarm_tenbagger_plan(self, session: AsyncSession):
        """预热牛股雷达明日预案，减少首次切 tab 卡顿。"""
        from app.api.v1.tenbagger import prewarm_next_day_plan_snapshot

        payload = await prewarm_next_day_plan_snapshot(session, None, force_refresh=False)
        plans = payload.get("plans") or []
        logger.debug(f"明日预案预热完成: total={len(plans)}")

    async def _prewarm_promotion_candidates(
        self,
        session: AsyncSession | None = None,
        *,
        trigger: str = "schedule",
    ):
        """Coalesce requests, bound work and keep official snapshots time-causal."""
        from app.api.v1.promotion import _PROMOTION_OFFICIAL_CONTEXT_WINDOWS

        requested_at = datetime.now()
        context = _promotion_prediction_snapshot_context(trigger, requested_at)
        window = _PROMOTION_OFFICIAL_CONTEXT_WINDOWS.get(context)
        clock = (requested_at.hour, requested_at.minute)
        if window is None or not window[0] <= clock <= window[1]:
            return self._record_promotion_runtime_audit(
                "snapshot", "expired_window", trigger=trigger, context=context,
            )
        key = (requested_at.date(), context)
        if key in self._promotion_snapshot_completed_contexts:
            return {"status": "already_completed", "context": context}
        if self._promotion_prediction_refreshing:
            # Same-context requests cannot manufacture a second "official" frame.
            # Newer contexts supersede older pending requests, but never raw quotes.
            pending = self._promotion_prediction_pending
            if (
                context != self._promotion_prediction_active_context
                and (
                    pending is None
                    or context >= pending["context"]
                    or pending["trade_date"] != requested_at.date().isoformat()
                )
            ):
                self._promotion_prediction_pending = {
                    "trigger": trigger, "context": context,
                    "trade_date": requested_at.date().isoformat(),
                    "requested_at": requested_at.isoformat(),
                }
            return self._record_promotion_runtime_audit(
                "snapshot", "coalesced", trigger=trigger, context=context,
            )
        now_tick = _time.monotonic()
        self._promotion_prediction_last_attempt = {
            item_key: value for item_key, value in self._promotion_prediction_last_attempt.items()
            if item_key[0] == requested_at.date()
        }
        last_attempt = self._promotion_prediction_last_attempt.get(key)
        if (
            last_attempt is not None
            and now_tick - last_attempt < max(0.0, float(settings.PROMOTION_SNAPSHOT_RETRY_COOLDOWN_SEC))
        ):
            return {"status": "retry_cooldown", "context": context}
        # Claim before the first await so Cron and the watchdog cannot race.
        self._promotion_prediction_refreshing = True
        self._promotion_prediction_epoch += 1
        self._promotion_prediction_active_context = context
        self._promotion_prediction_task = asyncio.current_task()
        self._promotion_prediction_last_attempt[key] = now_tick
        self._promotion_phase_timings = {}
        self._set_promotion_phase("calendar")
        cancelled = False
        suffix = context.rsplit("_", 1)[-1]
        scheduled_at = requested_at.replace(
            hour=int(suffix[:2]), minute=int(suffix[2:]), second=0, microsecond=0,
        )
        queue_lag_ms = max(0.0, (requested_at - scheduled_at).total_seconds() * 1000)
        try:
            payload = await asyncio.wait_for(
                self._build_promotion_snapshot_once(session, trigger=trigger),
                timeout=max(0.01, float(settings.PROMOTION_SNAPSHOT_TIMEOUT_SEC)),
            )
            self._set_promotion_phase(None)
            recorded = _safe_int(((payload or {}).get("learning") or {}).get("recorded_predictions")) or 0
            if recorded:
                self._promotion_snapshot_completed_contexts.add(key)
            status = "completed" if recorded else str((payload or {}).get("status") or "no_snapshot")
            timing = self._record_promotion_runtime_audit(
                "snapshot", status, trigger=trigger, context=context,
                started_at=requested_at.isoformat(),
                elapsed_ms=round((_time.monotonic() - now_tick) * 1000, 1),
                schedule_lag_ms=round(queue_lag_ms, 1),
                phase_timings_ms=dict(self._promotion_phase_timings),
            )
            if isinstance(payload, dict):
                payload["runtime_timing"] = timing
            return payload
        except asyncio.TimeoutError:
            self._set_promotion_phase(None)
            return self._record_promotion_runtime_audit(
                "snapshot", "timeout", trigger=trigger, context=context,
                started_at=requested_at.isoformat(),
                elapsed_ms=round((_time.monotonic() - now_tick) * 1000, 1),
                schedule_lag_ms=round(queue_lag_ms, 1),
                phase_timings_ms=dict(self._promotion_phase_timings),
            )
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as exc:
            self._set_promotion_phase(None)
            logger.error(f"晋级预测调度失败: {type(exc).__name__}")
            return self._record_promotion_runtime_audit(
                "snapshot", "failed", trigger=trigger, context=context,
                error_type=type(exc).__name__,
                elapsed_ms=round((_time.monotonic() - now_tick) * 1000, 1),
                schedule_lag_ms=round(queue_lag_ms, 1),
                phase_timings_ms=dict(self._promotion_phase_timings),
            )
        finally:
            self._set_promotion_phase(None)
            self._promotion_prediction_refreshing = False
            self._promotion_prediction_active_context = None
            self._promotion_prediction_task = None
            pending = self._promotion_prediction_pending
            self._promotion_prediction_pending = None
            if not cancelled and pending and self.scheduler.running:
                pending_context = pending["context"]
                pending_window = _PROMOTION_OFFICIAL_CONTEXT_WINDOWS.get(pending_context)
                now = datetime.now()
                if (
                    pending["trade_date"] == now.date().isoformat()
                    and pending_window is not None
                    and pending_window[0] <= (now.hour, now.minute) <= pending_window[1]
                    and (now.date(), pending_context) not in self._promotion_snapshot_completed_contexts
                ):
                    # Never retain a caller-owned DB session in a detached task.
                    self._promotion_prediction_task = asyncio.create_task(
                        self._prewarm_promotion_candidates(trigger=pending["trigger"])
                    )
                else:
                    self._record_promotion_runtime_audit(
                        "snapshot", "pending_expired", context=pending_context,
                    )

    async def _begin_promotion_generation(self, snapshot_context: str):
        """Commit a fail-closed start marker before any slow candidate work.

        A separate transaction must not commit/rollback a caller's market writes.
        The outer snapshot timeout owns cancellation; failed marker persistence
        raises and candidate generation must not proceed.
        """
        from app.api.v1.promotion import (
            PROMOTION_MODEL_IDENTITY, _PROMOTION_OFFICIAL_CONTEXT_WINDOWS,
        )
        from app.promotion.ledger import ScheduleBatch, append_blocked_prediction_run

        recorded_at = datetime.now()
        window = _PROMOTION_OFFICIAL_CONTEXT_WINDOWS.get(snapshot_context)
        if window is None or not window[0] <= (recorded_at.hour, recorded_at.minute) <= window[1]:
            return None
        batch = ScheduleBatch(snapshot_context, recorded_at)
        self._set_promotion_phase("generation_barrier")
        async with async_session() as marker_session:
            result = await append_blocked_prediction_run(
                marker_session, {}, identity=PROMOTION_MODEL_IDENTITY, batch=batch,
                persistence={
                    "status": "generation_pending",
                    "reason": "This scheduler attempt has not published a completed prediction run.",
                    "input_count": None, "recordable_count": None, "prepared_count": None,
                },
            )
            await marker_session.commit()
        return result.as_payload()

    async def _build_promotion_snapshot_once(
        self,
        session: AsyncSession | None = None,
        *,
        trigger: str = "schedule",
    ):
        """生成当前时间窗正式批次；质量门禁和模型身份不变。"""
        attempt_started_at = datetime.now()
        attempt_trade_date = attempt_started_at.date()
        if not await trade_calendar.is_trade_day(attempt_trade_date):
            return {"status": "not_trade_day"}
        generation_attempt = None
        try:
            snapshot_context = _promotion_prediction_snapshot_context(trigger, attempt_started_at)
            if datetime.now().date() != attempt_trade_date:
                return {"status": "expired_window", "context": snapshot_context}
            generation_attempt = await self._begin_promotion_generation(snapshot_context)
            if generation_attempt is None:
                return {"status": "expired_window", "context": snapshot_context}
            session_name = trade_calendar.get_trade_session()
            now_clock = datetime.now().time()
            if session_name in {"pre_auction", "morning"} and now_clock.hour == 9 and now_clock.minute <= 26:
                snapshot_time = "09:25:00" if now_clock.minute <= 25 else "09:26:00"
                self._set_promotion_phase("auction_collect")
                await self._auction_collect(force=True, auction_time=snapshot_time)
            if snapshot_context in {"promotion_1510", "promotion_2000"}:
                if session is not None:
                    close_health = await self._ensure_close_snapshot_ready(session)
                else:
                    async with async_session() as close_session:
                        close_health = await self._ensure_close_snapshot_ready(close_session)
                if not close_health.get("ready"):
                    logger.error(
                        "晋级预测收盘快照未冻结，拒绝生成正式批次: "
                        f"context={snapshot_context} health={close_health}"
                    )
                    return {
                        "status": "blocked",
                        "generation_attempt": generation_attempt,
                        "quality_gate": {
                            "gate_passed": False,
                            "status": "blocked",
                            "blocking_count": 1,
                            "blocking_datasets": ["stock_kline"],
                        },
                        "prediction_health": {
                            "status": "blocked",
                            "should_warn": True,
                            "message": "收盘行情尚未完成99%终场固化",
                            "detail": close_health,
                        },
                        "learning": {"recorded_predictions": 0},
                    }
            self._set_promotion_phase("news_cache_check")
            news_refresh_result = await self._ensure_fresh_promotion_news(trigger=trigger)

            from app.api.v1.promotion import PROMOTION_INTERNAL_RECORD_LIMIT, build_promotion_candidates
            from app.strategy.auction import auction_collector

            async def run_with(target_session: AsyncSession):
                self._set_promotion_phase("auction_health")
                auction_health = await auction_collector.get_snapshot_health(
                    target_session,
                    attempt_trade_date,
                )
                if auction_health.get("missing") or auction_health.get("degraded"):
                    ensure_result = await auction_collector.ensure_auction_data_snapshot(
                        target_session,
                        attempt_trade_date,
                        auction_time="09:25:00",
                    )
                    auction_health = {
                        **auction_health,
                        **ensure_result,
                    }
                    logger.info(
                        "晋级预测竞价健康检查: "
                        f"status={ensure_result.get('status')} "
                        f"complete={ensure_result.get('feed_complete_count', 0)}/"
                        f"{ensure_result.get('snapshot_count', 0)}"
                    )
                if auction_health.get("missing"):
                    logger.warning("晋级预测快照缺少当天竞价数据: 首板竞价强攻路线本次为空")
                elif auction_health.get("degraded"):
                    logger.warning(
                        "晋级预测竞价字段不完整: 仅保留价格/集群预测观察，"
                        "不得进入可执行竞价路线"
                    )

                gate_mode = str(settings.PROMOTION_QUALITY_GATE_MODE or "monitor").strip().lower()
                quality_gate = {
                    "gate_passed": False,
                    "status": "disabled",
                    "mode": gate_mode,
                    "blocking_count": None,
                    "run_id": None,
                }
                if gate_mode != "off":
                    from app.core.data_quality import data_quality_guard

                    self._set_promotion_phase("quality_audit")
                    quality_gate = await data_quality_guard.audit_prediction_data(
                        target_session,
                        trade_date_value=attempt_trade_date,
                        snapshot_context=snapshot_context,
                        persist=True,
                        lookback_days=max(20, int(settings.PROMOTION_QUALITY_GATE_LOOKBACK_DAYS)),
                    )
                    if not quality_gate.get("gate_passed"):
                        blocks_all_routes = _promotion_quality_gate_blocks_all_routes(
                            quality_gate
                        )
                        logger.warning(
                            "晋级预测数据质量闸门未通过: "
                            f"mode={gate_mode}, blocking={quality_gate.get('blocking_count', 0)}, "
                            f"blocks_all_routes={blocks_all_routes}"
                        )
                        if gate_mode == "enforce" and blocks_all_routes:
                            return {
                                "status": "blocked",
                                "generation_attempt": generation_attempt,
                                "quality_gate": quality_gate,
                                "prediction_health": {
                                    "status": "blocked",
                                    "should_warn": True,
                                    "message": "预测被数据质量闸门阻止",
                                    "detail": "请先在数据治理页修复阻断项",
                                },
                                "learning": {"recorded_predictions": 0},
                            }
                        if gate_mode == "enforce":
                            logger.warning(
                                "晋级预测质量闸门部分阻断：继续生成不可变批次，"
                                "由 route_gates 分别隔离 B/C/D 消费"
                            )
                self._set_promotion_phase("candidate_build")
                payload = await build_promotion_candidates(
                    limit=12,
                    ranked_limit=PROMOTION_INTERNAL_RECORD_LIMIT,
                    snapshot_source="schedule",
                    snapshot_context=snapshot_context,
                    db=target_session,
                    quality_gate=quality_gate,
                )
                payload["generation_attempt"] = generation_attempt
                if quality_gate is not None:
                    payload["quality_gate"] = quality_gate
                learning = payload.get("learning") or {}
                # The immutable production run is already committed here. A later
                # shadow timeout must not cause the watchdog to publish it twice.
                if _safe_int(learning.get("recorded_predictions")) > 0:
                    self._promotion_snapshot_completed_contexts.add((attempt_trade_date, snapshot_context))
                prediction_run_id = _safe_int(learning.get("prediction_run_id"))
                if (
                    settings.PROMOTION_SHADOW_AUTOMATION_ENABLED
                    and _safe_int(learning.get("recorded_predictions")) > 0
                    and prediction_run_id > 0
                    and bool(quality_gate.get("gate_passed"))
                ):
                    try:
                        from app.promotion.shadow import run_eligible_shadows_for_prediction_run

                        self._set_promotion_phase("model_shadow")
                        shadow_result = await run_eligible_shadows_for_prediction_run(
                            target_session,
                            prediction_run_id=prediction_run_id,
                            quality_gate=quality_gate,
                        )
                        payload["shadow_automation"] = {
                            "status": shadow_result.get("status"),
                            "prediction_run_id": prediction_run_id,
                            "artifact_count": shadow_result.get("artifact_count", 0),
                            "results": [
                                {
                                    "artifact_id": item.get("artifact_id"),
                                    "status": item.get("status"),
                                    "reason": item.get("reason"),
                                    "shadow_run_id": (
                                        (item.get("shadow_run") or {}).get("id")
                                    ),
                                    "evaluation_decision": (
                                        (item.get("evaluation") or {}).get("decision")
                                    ),
                                }
                                for item in shadow_result.get("results", [])
                            ],
                            "automatic_promotion": False,
                        }
                    except Exception as shadow_exc:
                        logger.error(f"晋级挑战者影子自动化失败，生产冠军不受影响: {shadow_exc}")
                        payload["shadow_automation"] = {
                            "status": "failed_closed",
                            "error": str(shadow_exc),
                            "automatic_promotion": False,
                        }
                return payload

            if session is not None:
                payload = await run_with(session)
            else:
                async with async_session() as own_session:
                    payload = await run_with(own_session)

            learning = payload.get("learning") or {}
            health = payload.get("prediction_health") or {}
            logger.info(
                "晋级预测快照完成: "
                f"trigger={trigger}, context={snapshot_context}, first_date={payload.get('first_board_trade_date')}, "
                f"second_date={payload.get('second_board_trade_date')}, "
                f"ranked_first={payload.get('ranked_first_board_count')}, "
                f"ranked_second={payload.get('ranked_second_board_count')}, "
                f"recorded={learning.get('recorded_predictions')}, "
                f"health={health.get('status')}, "
                f"news_refresh={news_refresh_result.get('status')}"
            )
            if _safe_int(learning.get("recorded_predictions")) > 0:
                self._promotion_snapshot_completed_contexts.add((attempt_trade_date, snapshot_context))
            else:
                logger.warning(
                    "晋级预测快照未写入候选，竞价窗口守护将在窗口内重试: "
                    f"trigger={trigger}, context={snapshot_context}"
                )
            return payload
        except Exception as e:
            logger.error(f"晋级预测快照失败: {e}")
            return {
                "status": "failed", "error_type": type(e).__name__,
                "generation_attempt": generation_attempt,
            }

    async def _promotion_daily_learning_review(self):
        """盘后对齐前一日预测与当日真实行情，回填结果并输出精度/召回率审计日志。"""
        if self._promotion_learning_reviewing:
            logger.debug("晋级预测逐日复盘跳过: 上一轮仍在运行")
            return None
        if not await trade_calendar.is_trade_day():
            logger.debug("晋级预测逐日复盘跳过: 当前不是交易日")
            return None

        self._promotion_learning_reviewing = True
        try:
            from app.api.v1.promotion import run_promotion_daily_learning_review

            async with async_session() as session:
                payload = await run_promotion_daily_learning_review(session)
            latest = payload.get("latest") or {}
            aggregate = payload.get("aggregate") or {}
            recommendation = payload.get("recommendation") or {}
            logger.info(
                "晋级预测逐日复盘完成: "
                f"actual_date={latest.get('actual_trade_date')}, "
                f"predicted={latest.get('predicted_count')}, "
                f"limit_up={latest.get('actual_target_limit_up_count')}, "
                f"hits={latest.get('predicted_limit_up_hit_count')}, "
                f"precision={latest.get('limit_up_precision')}, "
                f"recall={latest.get('limit_up_recall')}, "
                f"rolling_brier={aggregate.get('brier_score')}, "
                f"action={recommendation.get('status')}, "
                f"evaluated={payload.get('evaluated_records')}"
            )
            # 单日/近样本直接改生产类变量会造成不可复现的参数漂移，默认永久关闭。
            if settings.PROMOTION_LEGACY_DAILY_WEIGHT_ADJUSTMENT_ENABLED:
                logger.warning("旧版在线权重反馈已显式启用；该模式不具备 Champion/Challenger 审计保障")
                await self._adjust_model_weights_from_learning()
            else:
                logger.info("晋级预测复盘仅追加证据，不自动修改生产权重")
            return payload
        except Exception as exc:
            logger.error(f"晋级预测逐日复盘失败: {exc}")
            return None
        finally:
            self._promotion_learning_reviewing = False

    async def _adjust_model_weights_from_learning(self):
        """[v3.2 P3] 从学习复盘结果自适应调整模型惩罚系数

        读取最近N天的预测精度，动态调整 bull_score / tenbagger 的 pullback_penalty:
        - 精度<30%: 惩罚系数+0.05（更保守）
        - 精度 30-50%: 惩罚系数不变
        - 精度>50%: 惩罚系数-0.03（稍微放松）
        """
        from app.signal.bull_score import BullScoreModel
        from app.signal.tenbagger_model import TenbaggerModel

        async with async_session() as session:
            # 读取最近5个交易日有信号绩效的数据
            result = await session.execute(
                select(SignalPerformance)
                .where(SignalPerformance.signal_type.in_(["ten_bagger", "strong"]))
                .order_by(desc(SignalPerformance.signal_time))
                .limit(200)
            )
            rows = result.scalars().all()
            if len(rows) < 30:
                logger.info("[learning_feedback] 信号绩效数据不足(需>=30), 跳过")
                return

            # 计算整体精度: 1日正收益比例
            hits = sum(1 for r in rows if (r.return_1d or 0) > 0)
            avg_precision = hits / len(rows) if rows else 0
            logger.info(
                f"[learning_feedback] 近{len(rows)}条信号精度: {avg_precision:.1%}, "
                f"命中={hits}/{len(rows)}"
            )

            adjustment = 0.0
            if avg_precision < 0.30:
                adjustment = -0.05
                logger.warning(
                    f"[learning_feedback] 精度{avg_precision:.1%}<30%: 动量惩罚加强→{adjustment:+.2f}"
                )
            elif avg_precision < 0.40:
                adjustment = -0.03
                logger.info(
                    f"[learning_feedback] 精度{avg_precision:.1%}<40%: 动量惩罚微调→{adjustment:+.2f}"
                )
            elif avg_precision > 0.55:
                adjustment = 0.03
                logger.info(
                    f"[learning_feedback] 精度{avg_precision:.1%}>55%: 动量惩罚放宽→{adjustment:+.2f}"
                )

            if adjustment != 0.0:
                BullScoreModel.learning_adjustment = adjustment
                TenbaggerModel.learning_adjustment = adjustment
                logger.info(
                    f"[learning_feedback] 自适应调整已应用: adjustment={adjustment:+.2f}, "
                    f"precision={avg_precision:.1%}"
                )

    async def _review_snapshot_automation(self, phase: str):
        """生成时点化三阶段复盘；失败、降级和重试均写入自动化台账。"""
        lock_key = f"review:{phase}"
        if lock_key in self._review_automation_running:
            logger.debug(f"{phase} 复盘自动化跳过: 上一轮仍在运行")
            return None
        if not settings.REVIEW_AUTOMATION_ENABLED:
            return None
        if not await trade_calendar.is_trade_day():
            logger.debug(f"{phase} 复盘自动化跳过: 当前不是交易日")
            return None
        self._review_automation_running.add(lock_key)
        try:
            from app.review.automation import run_review_automation

            async with async_session() as session:
                result = await run_review_automation(
                    session,
                    review_date=date.today(),
                    phase=phase,
                    trigger="schedule",
                )
            log = logger.info if result.get("status") in {"completed", "already_completed"} else logger.warning
            log(
                f"{phase} 复盘自动化: status={result.get('status')}, "
                f"quality={result.get('quality_status')}, attempt={result.get('attempt')}"
            )
            return result
        except Exception as exc:
            logger.exception(f"{phase} 复盘自动化未捕获异常: {exc}")
            return None
        finally:
            self._review_automation_running.discard(lock_key)

    async def _gpt_review_report(self):
        """盘后生成 GPT 复盘报告；未配置时静默跳过，不阻塞确定性快照。"""
        if not settings.REVIEW_GPT_ENABLED:
            logger.debug("GPT复盘报告跳过: REVIEW_GPT_ENABLED=false")
            return
        if not await trade_calendar.is_trade_day():
            logger.debug("GPT复盘报告跳过: 当前不是交易日")
            return
        try:
            from app.models.review import DailyReviewSnapshot
            async with async_session() as session:
                snapshot = await session.scalar(
                    select(DailyReviewSnapshot)
                    .where(DailyReviewSnapshot.phase == "postmarket")
                    .order_by(desc(DailyReviewSnapshot.id))
                    .limit(1)
                )
                if snapshot is None:
                    logger.warning("GPT复盘报告跳过: 尚无冻结的盘后复盘快照")
                    return None
                from app.review.gpt_report import generate_gpt_report
                payload = await generate_gpt_report(
                    session,
                    review_date=snapshot.review_date,
                    phase="postmarket",
                    snapshot_id=snapshot.id,
                )
                logger.info(
                    f"GPT复盘报告完成: date={payload.get('review_date')} status={payload.get('status')}"
                )
        except Exception as exc:
            logger.error(f"GPT复盘报告失败，确定性复盘不受影响: {exc}")

    async def _market_regime_automation(self):
        """盘后冻结可解释市场风格，风格只作上下文和分层评估。"""
        lock_key = "market_regime"
        if lock_key in self._review_automation_running:
            logger.debug("市场风格自动化跳过: 上一轮仍在运行")
            return None
        if not settings.REVIEW_AUTOMATION_ENABLED:
            return None
        if not await trade_calendar.is_trade_day():
            logger.debug("市场风格自动化跳过: 当前不是交易日")
            return None
        self._review_automation_running.add(lock_key)
        try:
            from app.review.automation import run_regime_automation

            async with async_session() as session:
                result = await run_regime_automation(session, trigger="schedule")
            log = logger.info if result.get("status") in {"completed", "already_completed"} else logger.warning
            log(
                "市场风格自动化: "
                f"status={result.get('status')}, quality={result.get('quality_status')}, "
                f"attempt={result.get('attempt')}"
            )
            return result
        except Exception as exc:
            logger.exception(f"市场风格自动化未捕获异常: {exc}")
            return None
        finally:
            self._review_automation_running.discard(lock_key)

    async def _snapshot_daily_fundamentals(self):
        """截面化每日个股基本面，前向积累供历史复盘；绝不回填历史日期。"""
        from sqlalchemy import select, asc

        current_day = date.today()
        if not await trade_calendar.is_trade_day(current_day):
            logger.debug("基本面截面快照跳过: 当前不是交易日")
            return

        async with async_session() as session:
            latest_kline = await session.scalar(
                select(func.max(StockKline.trade_date))
            )
            if latest_kline != current_day:
                logger.warning(
                    f"基本面截面快照跳过: 当日K线尚未生成 latest_kline={latest_kline} requested={current_day}"
                )
                return

            rows = list(
                (
                    await session.scalars(
                        select(StockSpot)
                        .where(StockSpot.price.is_not(None))
                        .order_by(asc(StockSpot.code))
                    )
                ).all()
            )
            if not rows:
                logger.warning("基本面截面快照跳过: stock_spot 无数据")
                return

            existing_dates = {
                row[0]
                for row in (
                    await session.execute(
                        select(StockFundamentalDaily.trade_date).distinct()
                    )
                ).all()
                if row[0]
            }
            if current_day in existing_dates:
                logger.info(f"基本面截面快照跳过: {current_day} 已采集")
                return

            staged: dict[str, StockFundamentalDaily] = {}
            for spot in rows:
                code = str(spot.code or "").strip()
                if not code or any(
                    value is None
                    for value in (
                        spot.pe_ttm,
                        spot.pb,
                        spot.net_profit_growth,
                        spot.circ_market_cap,
                    )
                ):
                    continue
                staged[code] = StockFundamentalDaily(
                    code=code,
                    name=spot.name or "",
                    trade_date=current_day,
                    pe_ttm=spot.pe_ttm,
                    pb=spot.pb,
                    net_profit_growth=spot.net_profit_growth,
                    circ_market_cap=spot.circ_market_cap,
                    source="tencent_spot",
                    created_at=datetime.now(),
                )
            if not staged:
                logger.warning("基本面截面快照: 无可入库有效字段")
                return
            session.add_all(staged.values())
            await session.commit()
            logger.info(
                f"基本面截面快照完成: date={current_day}, codes={len(staged)}"
            )

    async def _daily_review(self):
        """[v3.2] 每日盘后复盘报告: 市场情绪+板块涨跌+涨停池+资金流向+消息面

        定时执行: 交易日20:25
        """
        if not await trade_calendar.is_trade_day():
            logger.debug("每日复盘跳过: 非交易日")
            return

        try:
            from run_review import daily_review

            async with async_session() as session:
                logger.info("每日盘后复盘开始...")
                await daily_review(session, full=True)
                logger.info("每日盘后复盘完成")
        except Exception as exc:
            logger.error(f"每日盘后复盘失败: {exc}")

    async def _weekly_chase_review(self):
        """每周末复校异动追高过滤阈值 — 日志留完整报告 + 飞书推精简摘要。

        定时执行: 周六 21:00（只读分析，不改库）。
        """
        if self._chase_review_refreshing:
            logger.debug("异动追高过滤周度复校跳过: 上一轮仍在运行")
            return
        self._chase_review_refreshing = True
        try:
            from app.signal.chase_calibration import build_chase_calibration_report
            from app.push.channels.feishu import feishu_channel
            from app.push.channels.base import PushMessage

            logger.info("📊 异动追高过滤周度复校开始...")
            report = await build_chase_calibration_report()
            logger.info("异动追高过滤周度复校报告:\n" + report["detail"])

            if feishu_channel.is_available():
                await feishu_channel.send(PushMessage(
                    title="异动追高过滤周度复校",
                    content=report["summary"],
                    msg_type="info",
                    category="review",
                    priority=5,
                ))
        except Exception as exc:
            logger.error(f"异动追高过滤周度复校失败: {exc}")
        finally:
            self._chase_review_refreshing = False


# =============================================================================
# 通用工具函数
# =============================================================================

def _normalize_code(raw: str) -> str:
    """标准化股票代码

    输入: '1', '000001', '600519', 'sh600519', 'sz000001', '600777.SH'
    输出: '000001', '600519'  (纯6位数字)
    """
    raw = raw.strip()
    # 去掉市场后缀 (.SH/.SZ/.BJ)
    for suffix in (".SH", ".SZ", ".BJ", ".sh", ".sz", ".bj"):
        if raw.upper().endswith(suffix):
            raw = raw[:-3]
            break
    # 去掉市场前缀
    for prefix in ("sh", "sz", "bj", "SH", "SZ", "BJ"):
        if raw.lower().startswith(prefix):
            raw = raw[2:]
            break
    # 补齐6位
    if raw.isdigit():
        return raw.zfill(6)
    return raw


def _safe_float(val) -> Optional[float]:
    """安全转 float, NaN/None/空字符串 → None"""
    if val is None:
        return None
    try:
        f = float(val)
        return f if pd.notna(f) else None
    except (ValueError, TypeError):
        if isinstance(val, str):
            text = val.strip().replace(",", "")
            if not text or text.lower() == "nan":
                return None

            multiplier = 1.0
            if text.endswith("%"):
                text = text[:-1]
            if text.endswith("亿"):
                multiplier = 1e8
                text = text[:-1]
            elif text.endswith("万"):
                multiplier = 1e4
                text = text[:-1]

            try:
                f = float(text) * multiplier
                return f if pd.notna(f) else None
            except (ValueError, TypeError):
                return None
        return None


def _safe_int(val) -> Optional[int]:
    """安全转 int, NaN/None/空字符串 → None"""
    if val is None:
        return None
    try:
        f = float(val)
        return int(f) if pd.notna(f) else None
    except (ValueError, TypeError):
        return None



    # =========================================================================
    # 同花顺日K线采集 (盘后1次) — 注意: 已在上方定义, 此处为旧副本请忽略
    # =========================================================================



data_scheduler = DataScheduler()
